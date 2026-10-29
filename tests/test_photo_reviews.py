import base64
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from orchestrator_bot import photo_reviews
from orchestrator_bot.store import Store
from photo_auditor import review_sync, runtime
from photo_auditor.images import digest
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, report


def payload(job_id=123, revision=0, color='white'):
    data = picture(color)
    return dict(audit_id=(f'{job_id:031x}' + str(revision)), job_id=job_id, revision=revision,
                sha256=digest(data), brand='CARVER', model='PPG-11000 DVR', status='review',
                summary='Неверная подпись топлива', preview=base64.b64encode(data).decode())


def test_registered_snapshot_used_for_exact_correction_and_stale_card_rejected(tmp_path):
    store = Store(tmp_path / 'orch.db')
    original = payload()
    photo_reviews.register(store, original)
    card = photo_reviews.list_cards(store)[0]
    sent = []
    factory = SimpleNamespace(correct_photo=lambda value: sent.append(value) or {'job_id': 999})
    request = photo_reviews.create_feedback(card, 'Дизельный генератор, убрать бензин', 42, 'a' * 32)
    assert photo_reviews.correct(store, factory, request)['job_id'] == 999
    assert sent[0]['notes'] == request['notes']
    modified = {**request, 'sha256': 'f' * 64}
    with pytest.raises(ValueError, match='другому снимку'):
        photo_reviews.correct(store, factory, modified)
    photo_reviews.register(store, payload(revision=1, color='blue'))
    assert photo_reviews.correct(store, factory, request)['reason'] == 'superseded'
    assert len(sent) == 1


def test_changed_image_cannot_be_registered_and_revision_cannot_move_backwards(tmp_path):
    store = Store(tmp_path / 'orch.db')
    value = payload()
    value['sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='снимку'):
        photo_reviews.register(store, value)
    photo_reviews.register(store, payload(revision=1, color='blue'))
    assert photo_reviews.register(store, payload())['reason'] == 'superseded'
    assert photo_reviews.list_cards(store)[0]['revision'] == 1
    with pytest.raises(ValueError, match='ревизии изменилось'):
        photo_reviews.register(store, payload(revision=1, color='black'))


def test_sync_mirrors_latest_card_once_without_making_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'test'})
    runtime.register_job(tmp_path, 123, 'kbt', 'CARVER', 'PPG-11000 DVR', 'Исходное задание')
    audits = AuditStore(tmp_path / 'runs/photo-auditor')
    product = dict(product_id='fotogen-123', revision=0, brand='CARVER', model='PPG-11000 DVR')
    row = audits.submit(product, picture())
    audits.finish(audits.claim(), report('review'))
    remote = Store(tmp_path / 'orch.db')
    calls = []
    def deliver(value):
        calls.append(value)
        return photo_reviews.register(remote, value)
    assert review_sync.sync(audits, deliver) == 1
    assert review_sync.sync(audits, deliver) == 0
    assert len(calls) == 1
    assert photo_reviews.get_card(remote, row['id'])['sha256'] == row['sha256']


def test_batch_selection_does_not_show_other_models(tmp_path):
    store = Store(tmp_path / 'orch.db')
    photo_reviews.register(store, payload())
    assert len(photo_reviews.list_cards(store, ['excel|carver|ppg-11000 dvr'])) == 1
    assert not photo_reviews.list_cards(store, ['excel|carver|ppg-11000 dr'])


def test_batch_filter_applies_before_limit_and_uses_authoritative_supplier_key(tmp_path):
    store = Store(tmp_path / 'orch.db')
    photo_reviews.register(store, payload(), item_key='supplier|original|full-product-name')
    for job_id in range(124, 140):
        photo_reviews.register(store, payload(job_id))
    cards = photo_reviews.list_cards(store, ['supplier|original|full-product-name'], limit=2)
    assert [card['job_id'] for card in cards] == [123]


@pytest.fixture
def sync_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'test'})
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    def add(job_id, *, broken=False, supplier=None):
        runtime.register_job(tmp_path, job_id, 'kbt', 'CARVER', 'PPG-11000 DVR', 'Исходное задание')
        row = store.submit(dict(product_id=f'fotogen-{job_id}', revision=0,
                                brand='CARVER', model='PPG-11000 DVR'), picture(), supplier=supplier)
        store.finish(store.claim(audit_id=row['id']), report('review'))
        with store.connect() as db:
            db.execute('UPDATE audits SET created=? WHERE id=?', (job_id, row['id']))
        if broken:
            (store.root / row['id'] / 'generated.bin').unlink()
        return row
    return store, add


def test_invalid_newest_snapshot_is_backed_off_durably_and_does_not_block_older_card(sync_cards, caplog):
    store, add = sync_cards
    old, broken = add(123), add(124, broken=True)
    calls = []
    def deliver(value):
        calls.append(value['job_id'])
        return {'registered': True}
    with caplog.at_level(logging.WARNING, logger='photo_auditor'):
        assert review_sync.sync(store, deliver, now=100) == 1
        assert calls == [123]
        restarted = AuditStore(store.root)
        assert review_sync.sync(restarted, deliver, now=101) == 0
    assert sum('отложена' in item.message for item in caplog.records) == 1
    (store.root / broken['id'] / 'generated.bin').write_bytes(picture())
    assert review_sync.sync(restarted, deliver, now=401) == 1
    assert calls[-1] == 124


def test_per_card_http400_has_backoff_and_other_cards_still_mirror(sync_cards):
    store, add = sync_cards
    add(123)
    add(124)
    calls = []
    def deliver(value):
        calls.append(value['job_id'])
        if value['job_id'] == 124:
            response = httpx.Response(400, request=httpx.Request('POST', 'http://localhost/photo-reviews'))
            raise httpx.HTTPStatusError('invalid snapshot', request=response.request, response=response)
        return {'registered': True}
    assert review_sync.sync(store, deliver, now=100) == 1
    assert calls == [124, 123]
    assert review_sync.sync(AuditStore(store.root), deliver, now=101) == 0
    assert calls == [124, 123]


def test_global_delivery_failure_does_not_starve_older_cards_on_next_pass(sync_cards):
    store, add = sync_cards
    add(123)
    add(124)
    calls = []
    def deliver(value):
        calls.append(value['job_id'])
        if value['job_id'] == 124:
            raise TimeoutError('network unavailable for this attempt')
        return {'registered': True}
    assert review_sync.sync(store, deliver, now=100) == 0
    assert calls == [124]
    assert review_sync.sync(store, deliver, now=101) == 1
    assert calls == [124, 123]


def test_many_invalid_newest_rows_have_bounded_work_and_eventual_older_progress(sync_cards):
    store, add = sync_cards
    add(100)
    for job_id in range(101, 125):
        add(job_id, broken=True)
    delivered = []
    def deliver(value):
        delivered.append(value['job_id'])
        return {'registered': True}
    assert review_sync.sync(store, deliver, limit=1, now=100) == 0
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM meta WHERE key LIKE 'review-sync-retry:%'").fetchone()[0] == 20
    assert review_sync.sync(store, deliver, limit=1, now=101) == 1
    assert delivered == [100]


def test_supplier_arriving_after_preview_is_retried_after_restart_without_repeated_preview_upload(sync_cards):
    store, add = sync_cards
    row = add(123)
    calls = []
    def deliver(value):
        calls.append(value)
        return {'registered': True}
    assert review_sync.sync(store, deliver, now=100) == 1
    assert 'supplier' not in calls[0]
    restarted = AuditStore(store.root)
    assert review_sync.sync(restarted, deliver, now=101) == 0
    original = picture('blue')
    (store.root / row['id'] / 'supplier.bin').write_bytes(original)
    assert review_sync.sync(restarted, deliver, now=399) == 0
    assert review_sync.sync(restarted, deliver, now=401) == 1
    assert len(calls) == 2
    assert base64.b64decode(calls[-1]['supplier']) == original
    assert calls[-1]['supplier_sha256'] == digest(original)
    assert review_sync.sync(AuditStore(store.root), deliver, now=900) == 0


def test_delivery_attempt_limit_counts_rejections_not_only_successes(sync_cards):
    store, add = sync_cards
    for job_id in range(100, 108):
        add(job_id)
    calls = []
    def reject(value):
        calls.append(value['job_id'])
        return {'registered': False}
    assert review_sync.sync(store, reject, limit=2, now=100) == 0
    assert calls == [107, 106]
    assert review_sync.sync(store, reject, limit=2, now=101) == 0
    assert calls == [107, 106, 105, 104]


def test_previous_source_sync_version_is_upgraded_even_when_report_did_not_change(sync_cards):
    store, add = sync_cards
    row = add(123, supplier=picture('blue'))
    row = store.get(row['id'])
    stamp = digest(json.dumps({'sha256': row['sha256'], 'report': row['report'], 'status': row['status'], 'source_sync': 1},
                             sort_keys=True, ensure_ascii=False).encode())
    with store.connect() as db:
        db.execute('INSERT INTO meta VALUES(?,?)', ('review-sync:' + row['id'], stamp))
    calls = []
    assert review_sync.sync(store, lambda value: calls.append(value) or {'registered': True}, now=100) == 1
    assert calls[0]['supplier_sha256'] == digest(picture('blue'))
