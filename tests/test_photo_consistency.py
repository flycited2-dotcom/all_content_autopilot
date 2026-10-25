from photo_auditor.consistency import preserve_defects
from tests.test_photo_auditor import comparison


def evidence(verdict='approved', status='match', reference='exact-reference'):
    return {'verdict': verdict, 'reason': 'test', **comparison(status), 'sources': [
        {'official': True, 'model_image_verified': True, 'image_sha256': reference}]}


def test_same_official_reference_defect_cannot_be_erased_by_repeated_optimistic_response():
    old = evidence('regenerate', 'mismatch')
    fresh = evidence()
    result = preserve_defects(fresh, [old])
    assert result['verdict'] == 'regenerate'
    assert all(c['status'] == 'mismatch' for c in result['checks'])
    assert fresh['verdict'] == 'approved'


def test_unverified_or_different_reference_does_not_supply_a_defect():
    fresh = evidence()
    assert preserve_defects(fresh, [evidence('regenerate', 'mismatch', 'another-image')]) == fresh
    old = evidence('regenerate', 'mismatch')
    old['sources'][0]['official'] = False
    assert preserve_defects(fresh, [old]) == fresh


def test_second_visual_pass_can_block_first_optimistic_response(tmp_path):
    import json
    from pathlib import Path
    from photo_auditor.engine import Auditor
    from photo_auditor.store import AuditStore
    from tests.test_photo_auditor import product, picture
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())
    class Provider:
        checks = 0
        def inspect(self, instruction, content, search=False, domains=()):
            if search:
                return {'text': json.dumps({'sources': [{'url': 'https://midea.com/p'}]}), 'sources': ['https://midea.com/p']}
            self.checks += 1
            return {'text': json.dumps(comparison('match' if self.checks == 1 else 'mismatch'))}
    class Web:
        def reference(self, candidate, model, cited, domains):
            return {**candidate, 'official': True, 'model_image_verified': True}, picture(), b'page'
    provider = Provider()
    result = Auditor(Path(__file__).resolve().parents[1], provider, Web()).run(product(), store.root / row['id'])
    assert provider.checks == 2 and result['verdict'] == 'regenerate'
    assert len(result['comparison_passes']) == 2


def test_old_signed_permit_is_blocked_when_audit_is_no_longer_approved(tmp_path, monkeypatch):
    from photo_auditor import integration
    from photo_auditor.store import AuditStore
    from tests.test_photo_auditor import picture, product, report
    monkeypatch.setattr(integration, 'load', lambda _: {'api_token': 'test'})
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    store.submit(product(), picture())
    store.finish(store.claim(), report('approved'))
    sha = integration.digest(picture())
    assert integration._current_approval(tmp_path, 'fotogen-123', sha)
    with store.connect() as db:
        db.execute("UPDATE audits SET status='review'")
    assert not integration._current_approval(tmp_path, 'fotogen-123', sha)
    assert not integration._current_approval(tmp_path / 'missing', 'fotogen-123', sha)


def test_verified_asset_can_request_native_repair_without_approving_photo(tmp_path, monkeypatch):
    from photo_auditor.store import AuditStore
    from photo_auditor import runtime
    from photo_auditor.images import digest
    from photo_auditor.reference_asset import from_audit
    from tests.test_photo_auditor import product, picture, report
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    source = {'id': 1, 'official': True, 'model_image_verified': True,
              'model': product()['model'], 'image_sha256': digest(picture('blue'))}
    store.finish(store.claim(), {**report('review'), 'sources': [source]})
    path = store.root / row['id'] / 'source-1.bin'
    path.write_bytes(picture('blue'))
    monkeypatch.setattr(runtime, '_read', lambda *a: {'mode': 'kbt', 'required': True,
        'brand': product()['brand'], 'model': product()['model']})
    assert store.request_reference_repair(row['id'])
    repaired = store.get(row['id'])
    assert repaired['status'] == 'regenerate' and repaired['report']['repair_mode'] == 'reference_asset'
    assert from_audit(tmp_path, repaired) == path
    assert (store.root / row['id'] / 'before-reference-repair.json').exists()
    assert not store.request_reference_repair(row['id'])
    path.write_bytes(picture('red'))
    assert from_audit(tmp_path, repaired) is None


def test_manual_or_unverified_photo_cannot_start_reference_repair(tmp_path, monkeypatch):
    from photo_auditor.store import AuditStore
    from photo_auditor import runtime
    from tests.test_photo_auditor import product, picture, report
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    store.finish(store.claim(), report('review'))
    monkeypatch.setattr(runtime, '_read', lambda *a: {'mode': 'research', 'required': True,
        'brand': product()['brand'], 'model': product()['model']})
    assert not store.request_reference_repair(row['id'])


def test_late_repeated_completion_cannot_overwrite_report_file(tmp_path):
    from photo_auditor.store import AuditStore
    from tests.test_photo_auditor import product, picture, report
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())
    claimed = store.claim()
    assert store.finish(claimed, report('review'))
    saved = (store.root / row['id'] / 'report.json').read_bytes()
    assert not store.finish(claimed, report('approved'))
    assert (store.root / row['id'] / 'report.json').read_bytes() == saved
