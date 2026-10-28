"""A saved photo-only approval cannot release a card with a new content brief."""
import base64
import hashlib
import hmac
import io
import json
import threading

import httpx
import pytest
from PIL import Image

from listener.pending import write_json
from photo_auditor import integration, runtime, service
from photo_auditor.images import digest
from photo_auditor.store import AuditStore


@pytest.fixture
def root(tmp_path, monkeypatch):
    load = lambda _: {'api_token': 'test-content-brief-secret'}
    monkeypatch.setattr(runtime, 'load', load)
    monkeypatch.setattr(integration, 'load', load)
    return tmp_path


def product(job_id=51):
    return {'product_id': f'fotogen-{job_id}', 'revision': 0,
            'brand': 'Xiaomi', 'model': 'L65MB-APRU'}


def register(root, job_id=51, specs='Сохранить тёмный фон; только подтверждённые характеристики.'):
    runtime.register_job(root, job_id, 'ready_tv', 'Xiaomi', 'L65MB-APRU', specs)
    return runtime.content_brief(root, product(job_id))


def picture():
    data = io.BytesIO()
    Image.new('RGB', (80, 80), 'white').save(data, 'PNG')
    return data.getvalue()


def report(brief=None):
    value = {'verdict': 'approved', 'reason': 'Товар совпадает',
             'checks': [], 'sources': [], 'dimensions_note': 'Не измерены'}
    if brief is not None:
        value['content_review'] = {
            'policy_version': runtime.CONTENT_POLICY, 'verdict': 'approved',
            'brief_sha256': runtime.brief_digest(brief), 'reason': 'Текст и оформление проверены.'}
    return value


def approved_store(root, approved_report):
    store = AuditStore(root / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    assert store.finish(store.claim(audit_id=row['id']), approved_report)
    return store, store.get(row['id'])


def permit(root, row):
    payload = {'product_id': row['product_id'], 'sha256': row['sha256']}
    signature = hmac.new(integration.load(root)['api_token'].encode(),
                         json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    write_json(integration._permit(root, row['product_id'], row['sha256']),
               {'payload': payload, 'signature': signature})
    return root / 'runs/photo-auditor' / row['id'] / 'generated.bin'


@pytest.mark.parametrize('mode', sorted(runtime.CARD_MODES))
def test_card_registration_freezes_separate_signed_brief_and_leaves_scope_unchanged(root, mode):
    assert runtime.register_job(root, 51, mode, 'Xiaomi', 'L65MB-APRU', 'Заданный стиль')
    expected = {'mode': mode, 'brand': 'Xiaomi', 'model': 'L65MB-APRU', 'specs': 'Заданный стиль'}
    assert runtime.content_brief(root, product()) == expected
    assert 'specs' not in runtime._read(root, 51)
    path = root / 'runs/photo-auditor/job-scope/51.brief.json'
    before = path.read_bytes()
    runtime.register_job(root, 51, mode, 'Xiaomi', 'L65MB-APRU', 'Заданный стиль')
    assert path.read_bytes() == before
    with pytest.raises(integration.AuditHold, match='Изменилось задание'):
        runtime.register_job(root, 51, mode, 'Xiaomi', 'L65MB-APRU', 'Другой стиль')
    assert path.read_bytes() == before


def test_research_and_unregistered_photos_have_no_card_brief(root):
    assert runtime.content_brief(root, product()) is None
    assert runtime.content_brief(root, {**product(), 'product_id': 'telegram-51'}) is None
    runtime.register_job(root, 51, 'research', 'Xiaomi', 'L65MB-APRU', 'Телевизоры')
    assert runtime.content_brief(root, product()) is None
    assert not (root / 'runs/photo-auditor/job-scope/51.brief.json').exists()


@pytest.mark.parametrize('change', ['missing', 'specs', 'model', 'signature', 'extra'])
def test_card_brief_must_be_present_exact_and_untampered(root, change):
    register(root)
    path = root / 'runs/photo-auditor/job-scope/51.brief.json'
    if change == 'missing':
        path.unlink()
    else:
        signed = json.loads(path.read_text(encoding='utf-8'))
        if change in ('specs', 'model'):
            signed['payload'][change] = 'Подменённое значение'
        elif change == 'signature':
            signed['signature'] = '0' * 64
        else:
            signed['payload']['channel'] = 'vk'
        path.write_text(json.dumps(signed), encoding='utf-8')
    with pytest.raises(integration.AuditHold, match='Нет подтверждённого задания'):
        runtime.content_brief(root, product())


def test_signature_cannot_be_reused_for_identical_brief_from_another_job(root):
    register(root, 51)
    register(root, 52)
    folder = root / 'runs/photo-auditor/job-scope'
    (folder / '52.brief.json').write_bytes((folder / '51.brief.json').read_bytes())
    with pytest.raises(integration.AuditHold):
        runtime.content_brief(root, product(52))


@pytest.mark.parametrize('field,value', [('brand', 'Other'), ('model', 'L43MB-APRU')])
def test_card_brief_cannot_bind_to_different_product(root, field, value):
    register(root)
    with pytest.raises(integration.AuditHold, match='другому товару'):
        runtime.content_brief(root, {**product(), field: value})


@pytest.mark.parametrize('field,value', [
    ('policy_version', 'visible-product-2'), ('verdict', 'review'),
    ('verdict', 'regenerate'), ('brief_sha256', '0' * 64),
])
def test_content_approval_requires_current_policy_verdict_and_exact_brief(root, field, value):
    brief = register(root)
    approved = report(brief)
    assert runtime.approval_covers_brief(root, product(), approved)
    approved['content_review'][field] = value
    assert not runtime.approval_covers_brief(root, product(), approved)
    assert not runtime.approval_covers_brief(root, product(), report())


def test_legacy_photo_only_permit_and_recovery_are_blocked_for_card(root):
    register(root)
    store, row = approved_store(root, report())
    image = permit(root, row)
    with pytest.raises(integration.AuditHold):
        integration.require_permit(root, row['product_id'], image)
    delivered = []
    service.recover_pipeline(store, lambda payload: delivered.append(payload) or {'handled': True})
    assert delivered == []
    assert store.get(row['id'])['status'] == 'approved'
    assert image.read_bytes() == picture()


def test_current_content_approval_releases_exact_file_and_recovers_once(root):
    brief = register(root)
    store, row = approved_store(root, report(brief))
    image = permit(root, row)
    integration.require_permit(root, row['product_id'], image)
    delivered = []
    deliver = lambda payload: delivered.append(payload) or {'handled': True}
    service.recover_pipeline(store, deliver)
    service.recover_pipeline(store, deliver)
    assert len(delivered) == 1
    assert delivered[0]['job_id'] == 51 and delivered[0]['sha256'] == digest(picture())
    assert delivered[0]['status'] == 'approved'


def test_changing_even_validly_signed_brief_revokes_previous_permit(root):
    brief = register(root)
    _, row = approved_store(root, report(brief))
    image = permit(root, row)
    integration.require_permit(root, row['product_id'], image)
    new_brief = {**brief, 'specs': 'Новое подтверждённое задание'}
    write_json(root / 'runs/photo-auditor/job-scope/51.brief.json', {
        'payload': new_brief,
        'signature': runtime._signature(root, {'job_id': 51, 'brief': new_brief}),
    })
    assert runtime.content_brief(root, product()) == new_brief
    with pytest.raises(integration.AuditHold):
        integration.require_permit(root, row['product_id'], image)


def test_rechecking_same_snapshot_immediately_revokes_previous_permit(root):
    brief = register(root)
    store, row = approved_store(root, report(brief))
    image = permit(root, row)
    integration.require_permit(root, row['product_id'], image)
    assert store.recheck(row['id'])
    with pytest.raises(integration.AuditHold):
        integration.require_permit(root, row['product_id'], image)


@pytest.mark.parametrize('change', ['missing', 'tampered'])
def test_missing_or_tampered_job_scope_cannot_bypass_brief_on_upload_or_recovery(root, change):
    brief = register(root)
    store, row = approved_store(root, report(brief))
    image = permit(root, row)
    scope = root / 'runs/photo-auditor/job-scope/51.json'
    if change == 'missing':
        scope.unlink()
    else:
        signed = json.loads(scope.read_text(encoding='utf-8'))
        signed['payload']['mode'] = 'research'
        scope.write_text(json.dumps(signed), encoding='utf-8')
    assert not runtime.approval_covers_brief(root, product(), row['report'])
    with pytest.raises(integration.AuditHold):
        integration.require_permit(root, row['product_id'], image)
    delivered = []
    service.recover_pipeline(store, lambda payload: delivered.append(payload) or {'handled': True})
    assert delivered == []


@pytest.mark.parametrize('content_approved', [False, True])
def test_api_rechecks_legacy_approval_on_same_snapshot_without_new_generation(root, content_approved):
    brief = register(root)
    store, old = approved_store(root, report(brief if content_approved else None))
    server = service.make_server(store, 'test-token', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(f'http://127.0.0.1:{server.server_address[1]}/audits',
                headers={'X-Audit-Token': 'test-token'},
                json={'product': product(), 'generated': base64.b64encode(picture()).decode()})
        assert response.status_code == 200
        current = response.json()
        assert current['id'] == old['id'] and current['sha256'] == old['sha256']
        assert current['product']['revision'] == 0
        assert current['status'] == ('approved' if content_approved else 'queued')
        assert (store.root / old['id'] / 'generated.bin').read_bytes() == picture()
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM audits').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM audit_history').fetchone()[0] == (0 if content_approved else 1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
