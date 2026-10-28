import asyncio
import base64
import json

import httpx
import pytest

from photo_auditor import integration, mode
from photo_auditor.images import digest
from tests.test_photo_auditor import picture


@pytest.fixture
def advisory(monkeypatch, tmp_path):
    monkeypatch.setattr(integration, 'load', lambda root: {'api_token': 'test'})
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: advisory\nrepairs: 1\nwait_seconds: 60\n',
                                                          encoding='utf-8')
    path = tmp_path / 'generated.png'
    path.write_bytes(picture())
    return tmp_path, path


def run_hook(root, path, statuses, regenerate=None, **kwargs):
    calls = []
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, json=calls[-1])
        value = json.loads(request.content)
        result = {'id': 'a' * 32, 'product': value['product'], 'sha256': digest(base64.b64decode(value['generated'])),
                  'status': statuses[min(len(calls), len(statuses) - 1)], 'feedback': 'Исправить ручку', 'report': None}
        calls.append(result)
        return httpx.Response(200, json=result)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await integration.checked_output(root, 'fotogen-7', 'Midea', 'ABC-12-RU', path, None,
                                                    regenerate, client=client, interval=0, **kwargs)
    return asyncio.run(run()), calls


def test_missing_or_broken_switch_keeps_historical_gate(tmp_path):
    assert mode.settings(tmp_path)['mode'] == 'gate'
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: [', encoding='utf-8')
    assert mode.settings(tmp_path)['mode'] == 'gate'
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: advisory\nrepairs: 9\nwait_seconds: 5\n',
                                                          encoding='utf-8')
    assert mode.settings(tmp_path) == {'mode': 'advisory', 'repairs': 1, 'wait_seconds': 900}


def test_project_switch_enables_advisory_mode():
    from pathlib import Path
    assert mode.advisory(Path(__file__).resolve().parents[1])


def test_doubtful_audit_passes_exact_file_on_with_notes(advisory):
    root, path = advisory
    result, calls = run_hook(root, path, ['review'])
    assert result == path and len(calls) == 1
    integration.require_permit(root, 'fotogen-7', path)
    path.write_bytes(picture('blue'))
    with pytest.raises(integration.AuditHold):
        integration.require_permit(root, 'fotogen-7', path)


def test_provider_wait_does_not_stall_the_photo_agent(advisory):
    root, path = advisory
    result, calls = run_hook(root, path, ['waiting_provider'])
    assert result == path and len(calls) == 1
    assert integration.released(root, 'fotogen-7', digest(path.read_bytes()))


def test_confirmed_discrepancy_gets_one_repair_then_passes_on(advisory):
    root, path = advisory
    notes = []
    async def regenerate(feedback):
        notes.append(feedback)
        path.write_bytes(picture('black'))
        return path
    result, calls = run_hook(root, path, ['regenerate', 'regenerate'], regenerate)
    assert result == path and notes == ['Исправить ручку'] and len(calls) == 2
    assert calls[1]['product']['revision'] == 1
    integration.require_permit(root, 'fotogen-7', path)


def test_unavailable_service_releases_instead_of_failing_the_job(advisory):
    root, path = advisory
    def handle(request):
        raise httpx.ConnectError('down')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await integration.checked_output(root, 'fotogen-7', 'Midea', 'ABC-12-RU', path, None, None,
                                                    client=client)
    assert asyncio.run(run()) == path
    integration.require_permit(root, 'fotogen-7', path)


def test_failed_source_only_repair_releases_unchanged_saved_file(advisory):
    root, path = advisory
    original = path.read_bytes()
    async def unavailable(_):
        raise integration.AuditHold('Для исправления нет проверенного фото точной модели')
    result, _ = run_hook(root, path, ['regenerate'], unavailable, repair_reference=True)
    assert result == path and path.read_bytes() == original
    integration.require_permit(root, 'fotogen-7', path)
    from photo_auditor import progress
    saved = json.loads((progress.folder(root, 'fotogen-7') / 'state.json').read_text())
    assert saved['phase'] == 'generated' and saved['revision'] == 0


def test_uncertain_paid_repair_is_still_held(advisory):
    root, path = advisory
    async def unknown(_):
        raise integration.AuditHold('Не подтверждён результат генерации')
    with pytest.raises(integration.AuditHold):
        run_hook(root, path, ['regenerate'], unknown, repair_reference=False)
    assert not integration.released(root, 'fotogen-7', digest(path.read_bytes()))


def test_wrong_snapshot_is_still_held_in_advisory_mode(advisory):
    root, path = advisory
    def handle(request):
        value = json.loads(request.content)
        return httpx.Response(200, json={'id': 'a' * 32, 'product': value['product'], 'sha256': 'wrong',
                                         'status': 'review'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await integration.checked_output(root, 'fotogen-7', 'Midea', 'ABC-12-RU', path, None, None, client=client)
    with pytest.raises(integration.AuditHold):
        asyncio.run(run())


def test_advisory_permit_is_ignored_when_gate_is_restored(advisory):
    root, path = advisory
    run_hook(root, path, ['review'])
    (root / 'rules/photo-auditor-mode.yaml').write_text('mode: gate\n', encoding='utf-8')
    with pytest.raises(integration.AuditHold):
        integration.require_permit(root, 'fotogen-7', path)


def test_held_review_jobs_are_offered_for_resumption(tmp_path):
    from photo_auditor.store import AuditStore
    from tests.test_photo_auditor import product
    store = AuditStore(tmp_path)
    job = store.submit(product(), picture())
    store.finish(store.claim(audit_id=job['id']), {'verdict': 'review', 'reason': 'Нет эталона', 'checks': [],
                                                   'sources': [], 'dimensions_note': 'Не измерены'})
    assert not store.recovery_results()
    assert [r['id'] for r in store.recovery_results(include_review=True)] == [job['id']]


def test_server_resumes_review_only_on_recovery_route():
    from orchestrator_bot.photo_recovery import validate
    value = {'job_id': 7, 'audit_id': 'a' * 32, 'attempt': 0, 'revision': 0, 'status': 'review',
             'sha256': 'b' * 64, 'brand': 'Midea', 'model': 'ABC-12-RU'}
    assert validate(value, allow_review=True) == value
    with pytest.raises(ValueError):
        validate(value)


def test_owner_sees_notes_of_a_passed_on_regenerate_instead_of_silence(tmp_path, monkeypatch):
    from photo_auditor import service
    from tests.test_photo_notification_images import Telegram, finished
    monkeypatch.setattr(integration, 'load', lambda root: {'api_token': 'test'})
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: advisory\n', encoding='utf-8')
    store, row = finished(tmp_path, 'regenerate', product_id='fotogen-71')
    integration._release(tmp_path, 'fotogen-71', row['sha256'], 'Лимит исправлений')
    tg = Telegram()
    service.notify(store, tg, 123)
    text = '\n'.join(event[2] for event in tg.events if event[0] == 'text')
    assert 'Аудитор нашёл расхождение' in text and 'Фото не задержано' in text


def test_old_server_rejecting_review_does_not_block_other_decisions(tmp_path, monkeypatch):
    from photo_auditor import progress, runtime, service
    from photo_auditor.store import AuditStore
    monkeypatch.setattr(runtime, 'load', lambda root: {'api_token': 'test'})
    monkeypatch.setattr(service, '_review_resume_paused_until', 0.0)
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-mode.yaml').write_text('mode: advisory\n', encoding='utf-8')
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    for job_id, verdict in ((5, 'review'), (6, 'regenerate')):
        runtime.register_job(tmp_path, job_id, 'research', 'Midea', 'ABC-12-RU', 'кондиционеры')
        row = store.submit({'product_id': f'fotogen-{job_id}', 'revision': 0, 'brand': 'Midea', 'model': 'ABC-12-RU'},
                           picture())
        store.finish(store.claim(audit_id=row['id']), {'verdict': verdict, 'reason': 'Отличие', 'checks': [],
                                                       'sources': [], 'dimensions_note': 'Не измерены'})
    monkeypatch.setattr(progress, 'restore_research', lambda *args: True)
    delivered = []
    def deliver(payload):
        delivered.append(payload['status'])
        if payload['status'] == 'review':
            raise ValueError('Решение не разрешает продолжение')
        return {'handled': True}
    service.recover_pipeline(store, deliver)
    assert delivered == ['regenerate', 'review']
    service.recover_pipeline(store, deliver)
    assert delivered == ['regenerate', 'review']  # paused, no request storm every 3 seconds
