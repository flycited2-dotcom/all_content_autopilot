import time

from photo_auditor.service import public_result
from photo_auditor.store import AuditStore
from photo_auditor import integration
from photo_auditor.images import digest
from tests.test_photo_auditor import picture, product
from tests.test_photo_advisory_mode import advisory, run_hook


def test_advisory_deferred_check_keeps_background_queue_and_exact_identity(tmp_path):
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: advisory\n', encoding='utf-8')
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    claimed = store.claim()
    store.defer(claimed, 'Read-only check delayed', retry_at=time.time() + 300)
    value = public_result(store.get(row['id']), tmp_path)
    assert value['status'] == 'deferred' and value['report'] is None
    assert value['product'] == row['product'] and value['sha256'] == row['sha256']
    assert store.get(row['id'])['status'] == 'queued'
    assert public_result(store.get(row['id']))['status'] == 'queued'
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: gate\n', encoding='utf-8')
    assert public_result(store.get(row['id']), tmp_path)['status'] == 'queued'


def test_initial_queue_is_still_waited_for_even_in_advisory_mode(tmp_path):
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: advisory\n', encoding='utf-8')
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    assert public_result(row, tmp_path)['status'] == 'queued'


def test_deferred_advisory_check_releases_exact_file_without_render_or_approval(advisory):
    root, path = advisory
    async def forbidden(_):
        raise AssertionError('A delayed check cannot authorize another paid image')
    result, calls = run_hook(root, path, ['deferred'], forbidden)
    assert result == path and len(calls) == 1
    assert integration.released(root, 'fotogen-7', digest(path.read_bytes()))
    integration.require_permit(root, 'fotogen-7', path)
