import json

import pytest

from photo_auditor import progress, runtime
from photo_auditor.integration import AuditHold
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, product


def register(root, monkeypatch, mode='research', model='ABC-12-RU'):
    monkeypatch.setattr(runtime, 'load', lambda root: {'api_token': 'test-research-scope'})
    runtime.register_job(root, 123, mode, 'Midea', model, '')


def test_interrupted_source_lookup_can_retry_without_a_paid_render(tmp_path, monkeypatch):
    register(tmp_path, monkeypatch)
    progress.start_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    assert progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU') is None
    progress.start_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    assert progress.restore_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU') is None


def test_real_unknown_image_generation_remains_blocked(tmp_path, monkeypatch):
    register(tmp_path, monkeypatch, mode='kbt')
    progress.starting(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU', 0, paid=True)
    with pytest.raises(AuditHold, match='Не подтверждён результат начатой генерации'):
        progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    with pytest.raises(AuditHold, match='области задания'):
        progress.start_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


def test_legacy_generating_research_checkpoint_is_not_blindly_cleared(tmp_path, monkeypatch):
    register(tmp_path, monkeypatch)
    progress.starting(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU', 0, paid=True)
    with pytest.raises(AuditHold, match='Не подтверждён результат начатой генерации'):
        progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


def test_forged_search_phase_cannot_bypass_card_scope(tmp_path, monkeypatch):
    register(tmp_path, monkeypatch, mode='kbt')
    path = progress.folder(tmp_path, 'fotogen-123') / 'state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'product_id': 'fotogen-123', 'brand': 'Midea', 'model': 'ABC-12-RU',
        'revision': 0, 'phase': 'researching', 'operation': progress.RESEARCH_OPERATION}))
    with pytest.raises(AuditHold, match='области задания'):
        progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


def test_search_checkpoint_cannot_substitute_identity(tmp_path, monkeypatch):
    register(tmp_path, monkeypatch, model='ANOTHER-1')
    with pytest.raises(AuditHold, match='области задания'):
        progress.start_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


def test_completed_search_restores_exact_photo_and_facts(tmp_path, monkeypatch):
    register(tmp_path, monkeypatch)
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    progress.start_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    progress.save_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU', 'verified facts')
    restored = progress.restore_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    assert restored['path'].read_bytes() == picture()
    assert restored['specs'] == 'verified facts'
    assert restored['revision'] == row['revision']


def test_search_phase_without_operation_marker_is_held(tmp_path, monkeypatch):
    register(tmp_path, monkeypatch)
    progress.start_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    path = progress.folder(tmp_path, 'fotogen-123') / 'state.json'
    value = json.loads(path.read_text())
    del value['operation']
    path.write_text(json.dumps(value))
    with pytest.raises(AuditHold, match='операция поиска'):
        progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
