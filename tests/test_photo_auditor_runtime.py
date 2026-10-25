import importlib.util
import json
from pathlib import Path

import pytest

from photo_auditor import runtime
from photo_auditor.integration import AuditHold


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'load', lambda root: {'api_token': 'runtime-test-secret'})
    return tmp_path


def test_product_upload_requires_exact_approved_image(config, monkeypatch):
    assert runtime.register_job(config, 12, 'kbt', 'Midea', 'EXACT-1', 'specs')
    called = []
    monkeypatch.setattr(runtime, 'require_permit', lambda *args: called.append(args))
    runtime.guard_upload(config, 12, Path('image.png'))
    assert called == [(config, 'fotogen-12', Path('image.png'))]
    with pytest.raises(AuditHold):
        runtime.guard_upload(config, 12, None)


def test_unknown_legacy_outbox_is_not_silently_released(config):
    with pytest.raises(AuditHold, match='старый outbox'):
        runtime.guard_upload(config, 999, Path('old.png'))


def test_editorial_scene_is_not_claimed_as_audited_product(config):
    assert not runtime.register_job(config, 13, 'research', '', 'Дом зимой', 'editorial')
    runtime.guard_upload(config, 13, None)
    with pytest.raises(AuditHold):
        runtime.register_job(config, 14, 'research', '', 'ABC-12', 'кондиционеры')


def test_job_scope_cannot_be_changed_to_bypass_audit(config):
    runtime.register_job(config, 15, 'kbt', 'Midea', 'EXACT-1', '')
    with pytest.raises(AuditHold):
        runtime.register_job(config, 15, 'ritual', '', '', '')
    path = config / 'runs/photo-auditor/job-scope/15.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['payload']['required'] = False
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(AuditHold):
        runtime.guard_upload(config, 15, Path('image.png'))


def test_human_calibration_never_changes_automatic_verdict_or_upload_permission(tmp_path):
    from photo_auditor.store import AuditStore
    from tests.test_photo_auditor import picture, product
    store = AuditStore(tmp_path)
    job = store.submit(product(), picture())
    claimed = store.claim(audit_id=job['id'])
    store.finish(claimed, {'verdict': 'review', 'reason': 'Нет официального эталона',
                          'checks': [], 'sources': [], 'dimensions_note': 'Не измерены'})
    assert store.feedback(job['id'], 'match')
    assert store.get(job['id'])['status'] == 'review'
    assert not store.feedback('missing-id', 'match')
    with pytest.raises(ValueError):
        store.feedback(job['id'], 'approved')


def test_generator_patch_gates_research_card_and_both_replay_routes():
    root = Path(__file__).resolve().parents[1]
    agent = root.parent / 'agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2/remote_agent.py'
    if not agent.exists():
        pytest.skip('External photo-agent checkout is not part of this repository')
    spec = importlib.util.spec_from_file_location('audit_patch', root / 'scripts/prepare-photo-auditor-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    live = agent.read_text(encoding='utf-8')
    source = live
    if 'PHOTO_AUDIT_BRIDGE_V' in live:
        with pytest.raises(ValueError, match='уже присутствует'):
            module.patched(live)
        backup = root / 'runs/2026-10-23/autopilot/photo-agent-backup/remote_agent.py'
        if not backup.exists():
            pytest.skip('Installed external bridge has no original source in this checkout')
        source = backup.read_text(encoding='utf-8')
    patched = module.patched(source)
    if 'PHOTO_AUDIT_BRIDGE_V6' in live:
        import ast
        assert ast.dump(ast.parse(live)) == ast.dump(ast.parse(patched))
    compile(patched, 'remote_agent.py', 'exec')
    assert patched.count('await checked_output(') == 2
    assert patched.count('guard_upload(AUDIT_ROOT, job_id, output_path)') == 2
    assert patched.count('register_job(AUDIT_ROOT, job_id, job_mode') == 3
    assert patched.count('except PhotoAuditHold as hold:') == 3
    assert 'restore_audit_output(' in patched and 'render_exact_product_card(' in patched
    assert 'Исходник %d восстановлен из снимка контроля' in patched
    with pytest.raises(ValueError, match='уже присутствует'):
        module.patched(patched)
