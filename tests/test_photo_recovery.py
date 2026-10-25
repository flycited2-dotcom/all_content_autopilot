import asyncio
import json
import sqlite3

import httpx
import pytest

from orchestrator_bot.photo_recovery import reconcile
from photo_auditor import integration, progress, service
from photo_auditor.research import SourceUnavailable, search_product
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, product, report


def test_quota_wait_survives_many_attempts_and_notifies_only_once(tmp_path):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())
    for now in range(100, 2500, 400):
        row = store.claim(now=now)
        store.defer(row, 'usage_limit', now=now, provider_wait=True)
        assert store.get(row['id'])['status'] == 'waiting_provider'
    assert len(store.waiting_notifications()) == 1
    store.notified(row['id'])
    assert not store.waiting_notifications()
    row = store.claim(now=3000)
    store.finish(row, report('approved'))
    assert len(store.pending_notifications()) == 1


def test_recheck_keeps_snapshot_history_revision_and_latest_only(tmp_path):
    store = AuditStore(tmp_path)
    store.submit(product(), picture())
    store.finish(store.claim(), report('regenerate'))
    row = store.submit(product(1), picture('black'))
    store.finish(store.claim(), report('review'))
    folder = tmp_path / row['id']
    (folder / 'source-1.html').write_text('previous evidence')
    assert store.recheck(row['id'])
    assert not store.recheck(row['id'])
    assert store.latest(product()['product_id'])['revision'] == 1
    assert (folder / 'generated.bin').read_bytes() == picture('black')
    assert list((folder / 'history').glob('*/source-1.html'))
    store.finish(store.claim(), report('approved'))
    results = store.recovery_results()
    assert len(results) == 1 and results[0]['attempt'] == 1
    assert results[0]['id'] == row['id']


def test_restart_restores_last_snapshot_and_never_resets_revision(tmp_path):
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    store.submit(product(), picture())
    store.finish(store.claim(), report())
    row = store.submit(product(1), picture('black'))
    restored = progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    assert restored['revision'] == 1 and restored['path'].read_bytes() == picture('black')
    assert store.get(row['id'])['sha256']
    restored['path'].write_bytes(picture('red'))
    with pytest.raises(integration.AuditHold, match='изменилась'):
        progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


def test_restart_retains_original_when_server_has_removed_failed_job_input(tmp_path):
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture('black'), picture('blue'))
    restored = progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    assert restored['supplier'].read_bytes() == picture('blue')
    (store.root / row['id'] / 'supplier.bin').unlink()
    assert progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')['supplier'].read_bytes() == picture('blue')
    restored['supplier'].write_bytes(picture('red'))
    with pytest.raises(integration.AuditHold, match='исходник изменился'):
        progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


@pytest.mark.parametrize('state', [None, {'phase': 'broken', 'revision': 0}, 'invalid'])
def test_unknown_or_corrupt_generation_outcome_cannot_start_paid_render(tmp_path, state):
    progress.starting(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU', 0)
    if state is not None:
        (progress.folder(tmp_path, 'fotogen-123') / 'state.json').write_text(json.dumps(state))
    with pytest.raises(integration.AuditHold):
        progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


def test_confirmed_quota_rejection_allows_first_generation_after_cooldown(tmp_path):
    progress.starting(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU', 0)
    progress.rejected(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    assert progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU') is None


def test_interrupted_native_layout_can_be_composed_again_without_paid_call(tmp_path):
    progress.starting(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU', 0, paid=False)
    assert progress.restore(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU') is None


def test_checked_output_resumes_revision_without_initial_regeneration(tmp_path, monkeypatch):
    monkeypatch.setattr(integration, 'load', lambda root: {'api_token': 'test'})
    photo = tmp_path / 'photo.png'
    photo.write_bytes(picture())
    requests = []
    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload['product']['revision'])
        return httpx.Response(200, json={'id': 'a' * 32, 'status': 'approved',
            'sha256': integration.digest(picture()), 'product': payload['product']})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            async def forbidden(_):
                pytest.fail('Another paid render must not start')
            return await integration.checked_output(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU',
                photo, None, forbidden, client=client, start_revision=1)
    assert asyncio.run(run()) == photo
    assert requests == [1]


def databases(tmp_path, status='failed', error='PHOTO_AUDIT_HOLD: quota'):
    state, queue = tmp_path / 'state.db', tmp_path / 'queue.db'
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE excel_items(key TEXT,status TEXT,card_job INTEGER,error TEXT)')
        db.execute("INSERT INTO excel_items VALUES('original-item','failed',123,'audit')")
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER,status TEXT,mode TEXT,brand TEXT,model TEXT,error_text TEXT,'
                   'claimed_by TEXT,claimed_at TEXT,updated_at TEXT)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL,NULL,NULL)',
                   (123, status, 'kbt', 'Midea', 'ABC-12-RU', error))
    return state, queue


def event():
    return {'job_id': 123, 'audit_id': 'a' * 32, 'attempt': 1, 'revision': 1,
            'status': 'approved', 'sha256': 'b' * 64, 'brand': 'Midea', 'model': 'ABC-12-RU'}


def test_server_resumes_same_job_atomically_and_deduplicates_lost_response(tmp_path):
    state, queue = databases(tmp_path)
    assert reconcile(state, queue, event())['resumed']
    assert reconcile(state, queue, event())['duplicate']
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT id,status FROM jobs').fetchall() == [(123, 'pending')]
    with sqlite3.connect(state) as db:
        assert db.execute('SELECT key,status,card_job FROM excel_items').fetchall() == [('original-item', 'card', 123)]


@pytest.mark.parametrize('status', ['cancelled', 'done', 'processing', 'pending'])
def test_server_does_not_revive_cancelled_finished_or_active_jobs(tmp_path, status):
    state, queue = databases(tmp_path, status=status)
    result = reconcile(state, queue, event())
    assert not result.get('resumed')
    assert result['handled'] == (status in ('cancelled', 'done'))
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0] == status


def test_server_rejects_identity_substitution_and_unrelated_failures(tmp_path):
    state, queue = databases(tmp_path, error='HTTP 503')
    assert reconcile(state, queue, event())['reason'] == 'unrelated_failure'
    with pytest.raises(ValueError, match='Номенклатура'):
        reconcile(state, queue, {**event(), 'model': 'DIFFERENT'})
    with pytest.raises(sqlite3.OperationalError):
        reconcile(tmp_path / 'missing.db', queue, event())
    assert not (tmp_path / 'missing.db').exists()


def test_delivery_retries_after_network_loss_then_saves_receipt(tmp_path):
    store = AuditStore(tmp_path)
    store.submit(product(), picture())
    store.finish(store.claim(), report('approved'))
    calls = []
    def deliver(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise ConnectionError('lost ack')
        return {'handled': True, 'resumed': True}
    service.recover_pipeline(store, deliver)
    service.recover_pipeline(store, deliver)
    service.recover_pipeline(store, deliver)
    assert len(calls) == 2 and calls[0] == calls[1]


def test_source_fallback_uses_download_failure_and_keeps_exact_identity():
    calls = []
    async def original(brand, model, category, **kwargs):
        calls.append((brand, model, category))
        if len(calls) == 1:
            response = httpx.Response(503, request=httpx.Request('GET', 'https://manufacturer.example/broken'))
            response.raise_for_status()
        return 'photo', 'facts'
    assert asyncio.run(search_product(original, 'Midea', 'EXACT-RU', 'холодильники')) == ('photo', 'facts')
    assert calls[1][:2] == ('Midea', 'EXACT-RU')
    assert '503' in calls[1][2] and 'https://manufacturer.example/broken' in calls[1][2]


def test_source_fallback_is_bounded_and_quota_is_not_retried():
    calls = []
    async def broken(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('research: source too small')
    with pytest.raises(SourceUnavailable):
        asyncio.run(search_product(broken, 'Midea', 'EXACT', 'холодильники'))
    assert len(calls) == 3
    calls.clear()
    async def limited(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('usage_limit')
    with pytest.raises(RuntimeError, match='usage_limit'):
        asyncio.run(search_product(limited, 'Midea', 'EXACT', 'холодильники'))
    assert len(calls) == 1
