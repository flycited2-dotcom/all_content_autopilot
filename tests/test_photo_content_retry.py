import copy
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from listener.pending import write_json
from photo_auditor import audit_checkpoint, integration, runtime
from photo_auditor.engine import Auditor
from photo_auditor.images import digest
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import comparison, picture
from tests.test_photo_content_review import result as content_result


@pytest.fixture
def ready(tmp_path, monkeypatch):
    secret = {'api_token': 'content-retry-test-secret'}
    monkeypatch.setattr(runtime, 'load', lambda root: secret)
    monkeypatch.setattr(integration, 'load', lambda root: secret)
    monkeypatch.setattr(audit_checkpoint.time, 'time', lambda: 10000.0)
    product = {'product_id': 'fotogen-70', 'brand': 'JVC', 'model': 'JK-MG123', 'revision': 0}
    runtime.register_job(tmp_path, 70, 'kbt', product['brand'], product['model'], 'Мощность 2000 Вт')
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product, picture())
    folder = store.root / row['id']
    (folder / 'source-1.bin').write_bytes(picture())
    (folder / 'source-1.html').write_bytes(b'<html>Official exact product</html>')
    source = {'id': 1, 'url': 'https://jvc.example/JK-MG123', 'image_url': 'https://jvc.example/exact.png',
              'official': True, 'model_image_verified': True, 'model': product['model'],
              'text': 'Мясорубка JVC JK-MG123. Максимальная мощность 2000 Вт. Реверс: есть.',
              'image_sha256': digest((folder / 'source-1.bin').read_bytes()),
              'page_sha256': digest((folder / 'source-1.html').read_bytes())}
    scoped = {**comparison(), 'limitations': [], 'source_checks': [
        {'id': 1, 'main_product_visible': True, 'suitable_for_repair': True, 'reason': 'Whole exact product.'}]}
    visual = {**copy.deepcopy(scoped), 'verdict': 'approved', 'reason': 'Visible product verified.',
              'sources': [source], 'comparison_passes': [copy.deepcopy(scoped), copy.deepcopy(scoped)],
              'policy_version': audit_checkpoint.POLICY_VERSION}
    brief = runtime.content_brief(tmp_path, product)
    assert audit_checkpoint.save(tmp_path, product, folder, brief, visual)
    checked = store.claim(audit_id=row['id'])
    report = {**visual, 'verdict': 'review', 'reason': 'Content parser needs a retry.',
              'content_review': {'verdict': 'review', 'reason': 'Incomplete content response'}}
    assert store.finish(checked, report)
    (folder / 'content-validation.json').write_text('{"error":"old parser error"}', encoding='utf-8')
    return tmp_path, store, store.get(row['id']), folder, visual


def assert_no_retry(store, row):
    assert store.get(row['id'])['status'] == row['status']
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM audit_history').fetchone()[0] == 0
    assert not (store.root / row['id'] / 'history').exists()


def test_content_retry_archives_prior_evidence_and_keeps_same_signed_visual_snapshot(ready):
    root, store, row, folder, visual = ready
    checkpoint_path = folder / audit_checkpoint.FILENAME
    before = checkpoint_path.read_bytes(), checkpoint_path.stat().st_mtime_ns
    image_before = (folder / 'generated.bin').read_bytes()
    assert store.recheck(row['id'], content_only=True)
    current = store.get(row['id'])
    assert current['id'] == row['id'] and current['product'] == row['product']
    assert current['revision'] == 0 and current['sha256'] == row['sha256']
    assert current['status'] == 'queued' and current['report'] is None
    assert current['tries'] == 0 and current['retry_at'] == 0 and current['lease'] is None
    assert (checkpoint_path.read_bytes(), checkpoint_path.stat().st_mtime_ns) == before
    assert (folder / 'generated.bin').read_bytes() == image_before
    with store.connect() as db:
        history = db.execute('SELECT * FROM audit_history WHERE audit_id=?', (row['id'],)).fetchall()
        assert len(history) == 1 and history[0]['attempt'] == 0
        assert json.loads(history[0]['report']) == row['report']
        assert db.execute('SELECT count(*) FROM audits').fetchone()[0] == 1
    archived = Path(history[0]['folder'])
    assert json.loads((archived / 'report.json').read_text(encoding='utf-8')) == row['report']
    assert (archived / audit_checkpoint.FILENAME).read_bytes() == before[0]
    assert (archived / 'source-1.bin').read_bytes() == (folder / 'source-1.bin').read_bytes()
    assert (archived / 'content-validation.json').is_file()
    assert audit_checkpoint.load(root, row['product'], folder, runtime.content_brief(root, row['product'])) == visual
    assert not store.recovery_results()
    assert not store.recheck(row['id'], content_only=True)


@pytest.mark.parametrize('status', ['approved', 'regenerate', 'queued', 'checking', 'waiting_provider'])
def test_content_retry_is_only_available_for_latest_review_state(ready, status):
    _, store, row, _, _ = ready
    with store.connect() as db:
        db.execute('UPDATE audits SET status=? WHERE id=?', (status, row['id']))
    row = store.get(row['id'])
    assert not store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


def test_content_retry_rejects_an_older_revision(ready):
    _, store, row, _, _ = ready
    newer = {**row['product'], 'revision': 1}
    with store.connect() as db:
        db.execute('INSERT INTO audits(id,product_id,revision,product,sha256,status,created) VALUES(?,?,?,?,?,?,?)',
                   ('a' * 32, row['product_id'], 1, json.dumps(newer), row['sha256'], 'queued', 10001.0))
    assert not store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


@pytest.mark.parametrize('missing', ['scope', 'brief', 'checkpoint'])
def test_content_retry_requires_signed_current_scope_brief_and_visual_approval(ready, missing):
    root, store, row, folder, _ = ready
    path = {'scope': runtime._path(root, 70),
            'brief': runtime._path(root, 70).with_name('70.brief.json'),
            'checkpoint': folder / audit_checkpoint.FILENAME}[missing]
    path.unlink()
    assert not store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


def test_content_retry_rejects_a_validly_signed_changed_brief(ready):
    root, store, row, _, _ = ready
    path = runtime._path(root, 70).with_name('70.brief.json')
    signed = json.loads(path.read_text(encoding='utf-8'))
    signed['payload']['specs'] = 'Мощность 3000 Вт'
    signed['signature'] = runtime._signature(root, {'job_id': 70, 'brief': signed['payload']})
    write_json(path, signed)
    assert runtime.content_brief(root, row['product'])['specs'] == 'Мощность 3000 Вт'
    assert not store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


def test_content_retry_rejects_changed_scope_even_when_signature_is_valid(ready):
    root, store, row, _, _ = ready
    path = runtime._path(root, 70)
    signed = json.loads(path.read_text(encoding='utf-8'))
    signed['payload']['mode'] = 'research'
    signed['signature'] = runtime._signature(root, signed['payload'])
    write_json(path, signed)
    assert not store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


def test_content_retry_rejects_expired_visual_evidence(ready, monkeypatch):
    _, store, row, _, _ = ready
    monkeypatch.setattr(audit_checkpoint.time, 'time', lambda: 13601.0)
    assert not store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


def test_content_retry_rejects_changed_source_evidence(ready):
    _, store, row, folder, _ = ready
    (folder / 'source-1.html').write_text('Changed source page', encoding='utf-8')
    assert not store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


def test_content_retry_rejects_changed_generated_image(ready):
    _, store, row, folder, _ = ready
    (folder / 'generated.bin').write_bytes(picture('red'))
    with pytest.raises(ValueError, match='снимок изменился'):
        store.recheck(row['id'], content_only=True)
    assert_no_retry(store, row)


def test_full_recheck_still_removes_cached_visual_approval(ready):
    _, store, row, folder, _ = ready
    with store.connect() as db:
        db.execute("UPDATE audits SET status='approved' WHERE id=?", (row['id'],))
    assert store.recheck(row['id'])
    assert not (folder / audit_checkpoint.FILENAME).exists()
    assert list((folder / 'history').glob('*/visual-checkpoint.json'))


def test_stale_upload_permit_cannot_authorize_a_queued_content_retry(ready):
    root, store, row, folder, _ = ready
    payload = {'product_id': row['product_id'], 'sha256': row['sha256']}
    signature = hmac.new(b'content-retry-test-secret', json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    write_json(integration._permit(root, row['product_id'], row['sha256']), {'payload': payload, 'signature': signature})
    assert store.recheck(row['id'], content_only=True)
    with pytest.raises(integration.AuditHold):
        integration.require_permit(root, row['product_id'], folder / 'generated.bin')


def test_content_retry_runs_only_new_content_passes_without_search_or_visual_requests(ready, monkeypatch):
    root, store, row, folder, _ = ready
    assert store.recheck(row['id'], content_only=True)

    class Provider:
        calls = 0

        def inspect(self, instruction, content, **kwargs):
            assert not kwargs.get('search')
            self.calls += 1
            return {'text': json.dumps(content_result(), ensure_ascii=False)}

    provider = Provider()
    (root / 'rules').mkdir()
    (root / 'rules/photo-auditor-manufacturers.yaml').write_text('{}', encoding='utf-8')
    auditor = Auditor(root, provider, object())
    monkeypatch.setattr(auditor, '_visual', lambda *args: pytest.fail('No repeated source search or visual inspection'))
    claimed = store.claim(audit_id=row['id'])
    report = auditor.run(claimed['product'], folder)
    assert report['verdict'] == 'approved' and report['content_review']['verdict'] == 'approved'
    assert provider.calls == 2
    assert store.finish(claimed, report)
    assert store.get(row['id'])['status'] == 'approved'
    assert store.get(row['id'])['revision'] == 0
