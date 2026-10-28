import copy
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from photo_auditor import audit_checkpoint as checkpoint, runtime
from photo_auditor.images import digest


def picture(color='white'):
    stream = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(stream, format='PNG')
    return stream.getvalue()


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda root: {'api_token': 'checkpoint-test-secret'})
    monkeypatch.setattr(checkpoint.time, 'time', lambda: 10000.0)
    folder = tmp_path / 'audit'
    folder.mkdir()
    (folder / 'generated.bin').write_bytes(picture())
    (folder / 'source-1.bin').write_bytes(picture('blue'))
    (folder / 'source-1.html').write_bytes(b'<html>Exact model page</html>')
    product = {'product_id': 'fotogen-10', 'brand': 'Xiaomi', 'model': 'L55MC-SRU', 'revision': 0}
    brief = {'mode': 'kbt', 'brand': product['brand'], 'model': product['model'], 'specs': 'Original facts'}
    checks = [{'aspect': aspect, 'status': 'match', 'confidence': 0.99,
               'observation': 'Matches the exact model.', 'fix': '', 'source_ids': [1]}
              for aspect in sorted(checkpoint._ASPECTS)]
    report = {'verdict': 'approved', 'reason': 'Product matches.', 'policy_version': checkpoint.POLICY_VERSION,
              'checks': checks, 'comparison_passes': [{'checks': copy.deepcopy(checks)},
                                                      {'checks': copy.deepcopy(checks)}],
              'sources': [{'id': 1, 'url': 'https://example.test/exact-model', 'official': True,
                           'image_sha256': digest((folder / 'source-1.bin').read_bytes()),
                           'page_sha256': digest((folder / 'source-1.html').read_bytes())}]}
    assert checkpoint.save(tmp_path, product, folder, brief, report)
    return tmp_path, product, folder, brief, report


def test_roundtrip_reuses_only_visual_evidence_and_returns_independent_data(saved):
    root, product, folder, brief, report = saved
    loaded = checkpoint.load(root, product, folder, brief)
    assert loaded == report
    loaded['checks'][0]['status'] = 'mismatch'
    assert checkpoint.load(root, product, folder, brief) == report


def test_content_verdict_is_never_inherited(saved):
    root, product, folder, brief, report = saved
    report['content_review'] = {'verdict': 'approved', 'reason': 'Previous content result'}
    assert checkpoint.save(root, product, folder, brief, report)
    loaded = checkpoint.load(root, product, folder, brief)
    assert loaded['verdict'] == 'approved'
    assert 'content_review' not in loaded


@pytest.mark.parametrize('change', [
    {'verdict': 'review'}, {'verdict': 'regenerate'}, {'policy_version': 'old-policy'},
    {'comparison_passes': []}, {'comparison_passes': [{'checks': []}]},
])
def test_unapproved_or_incomplete_visual_report_cannot_be_saved(saved, change):
    root, product, folder, brief, report = saved
    before = (folder / checkpoint.FILENAME).read_bytes()
    assert not checkpoint.save(root, product, folder, brief, {**report, **change})
    assert (folder / checkpoint.FILENAME).read_bytes() == before


def test_each_pass_must_check_every_aspect_without_a_visible_mismatch(saved):
    root, product, folder, brief, report = saved
    report['comparison_passes'][1]['checks'][0]['status'] = 'not_verifiable'
    assert not checkpoint.save(root, product, folder, brief, report)
    report['comparison_passes'][1]['checks'][0]['status'] = 'match'
    report['comparison_passes'][1]['checks'].pop()
    assert not checkpoint.save(root, product, folder, brief, report)


@pytest.mark.parametrize('key,value', [('product_id', 'fotogen-11'), ('brand', 'Different'),
                                      ('model', 'L43MB-APRU'), ('revision', 1)])
def test_exact_product_identity_is_bound(saved, key, value):
    root, product, folder, brief, _ = saved
    assert checkpoint.load(root, {**product, key: value}, folder, brief) is None


def test_original_brief_and_style_rules_are_bound(saved):
    root, product, folder, brief, _ = saved
    assert checkpoint.load(root, product, folder, {**brief, 'specs': 'Changed facts'}) is None
    assert checkpoint.load(root, product, folder, {**brief, 'presentation': {'prompt': 'Another layout'}}) is None


@pytest.mark.parametrize('elapsed,valid', [(3599, True), (3600, True), (3601, False), (-1, False)])
def test_expiry_and_future_timestamps_fail_closed(saved, monkeypatch, elapsed, valid):
    root, product, folder, brief, report = saved
    monkeypatch.setattr(checkpoint.time, 'time', lambda: 10000.0 + elapsed)
    assert (checkpoint.load(root, product, folder, brief) == report) is valid


@pytest.mark.parametrize('filename,replacement', [
    ('generated.bin', picture('red')), ('source-1.bin', picture('red')),
    ('source-1.html', b'<html>A different model</html>'),
])
def test_changed_generated_or_source_bytes_cannot_reuse_visual_approval(saved, filename, replacement):
    root, product, folder, brief, _ = saved
    (folder / filename).write_bytes(replacement)
    assert checkpoint.load(root, product, folder, brief) is None


@pytest.mark.parametrize('filename', ['generated.bin', 'source-1.bin', 'source-1.html'])
def test_missing_evidence_cannot_reuse_visual_approval(saved, filename):
    root, product, folder, brief, _ = saved
    (folder / filename).unlink()
    assert checkpoint.load(root, product, folder, brief) is None


def test_corrupt_or_unsigned_checkpoint_is_ignored_without_mutation(saved):
    root, product, folder, brief, _ = saved
    path = folder / checkpoint.FILENAME
    for raw in (b'{broken', b'\xff\xfe', b'{}', b'[]'):
        path.write_bytes(raw)
        assert checkpoint.load(root, product, folder, brief) is None
        assert path.read_bytes() == raw


def test_tampering_with_signed_report_or_using_another_secret_fails(saved, monkeypatch):
    root, product, folder, brief, report = saved
    path = folder / checkpoint.FILENAME
    signed = json.loads(path.read_text(encoding='utf-8'))
    signed['payload']['report']['reason'] = 'Altered evidence'
    path.write_text(json.dumps(signed), encoding='utf-8')
    assert checkpoint.load(root, product, folder, brief) is None
    assert checkpoint.save(root, product, folder, brief, report)
    monkeypatch.setattr(runtime, 'load', lambda root: {'api_token': 'a-different-secret'})
    assert checkpoint.load(root, product, folder, brief) is None


@pytest.mark.parametrize('change', ['policy', 'content', 'future', 'extra', 'source-declaration'])
def test_even_a_signed_checkpoint_must_satisfy_the_evidence_contract(saved, change):
    root, product, folder, brief, _ = saved
    path = folder / checkpoint.FILENAME
    signed = json.loads(path.read_text(encoding='utf-8'))
    payload = signed['payload']
    if change == 'policy':
        payload['policy_version'] = 'old-policy'
    elif change == 'content':
        payload['report']['content_review'] = {'verdict': 'approved'}
    elif change == 'future':
        payload['created'] = 11000.0
    elif change == 'extra':
        payload['unbound'] = 'unexpected field'
    else:
        payload['report']['sources'][0]['image_sha256'] = '0' * 64
    signed['signature'] = runtime._signature(root, payload)
    path.write_text(json.dumps(signed), encoding='utf-8')
    assert checkpoint.load(root, product, folder, brief) is None


@pytest.mark.parametrize('source_id', [0, 6, True, '../outside', 1.0])
def test_source_identifiers_are_bounded_exact_integers(saved, source_id):
    root, product, folder, brief, report = saved
    report['sources'][0]['id'] = source_id
    assert not checkpoint.save(root, product, folder, brief, report)


def test_source_hash_claims_must_match_actual_evidence_before_save(saved):
    root, product, folder, brief, report = saved
    report['sources'][0]['page_sha256'] = '0' * 64
    assert not checkpoint.save(root, product, folder, brief, report)


def test_load_does_not_write_delete_or_refresh_any_evidence(saved):
    root, product, folder, brief, report = saved
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()}
    assert checkpoint.load(root, product, folder, brief) == report
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()} == before


@pytest.mark.parametrize('name', [checkpoint.FILENAME, 'generated.bin', 'source-1.bin', 'source-1.html'])
def test_evidence_links_are_rejected(saved, monkeypatch, name):
    root, product, folder, brief, _ = saved
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path.name == name or original(path))
    assert checkpoint.load(root, product, folder, brief) is None


def test_missing_checkpoint_does_not_create_a_file_or_contact_a_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, '_signature', lambda *args: pytest.fail('No signature needed without a checkpoint'))
    assert checkpoint.load(tmp_path, {}, tmp_path, None) is None
    assert list(tmp_path.iterdir()) == []
