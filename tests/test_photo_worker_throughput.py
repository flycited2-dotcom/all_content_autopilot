import threading

import pytest

from photo_auditor.service import start_workers
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, product, report


def test_live_photoagent_waiter_is_prioritized_over_historical_rechecks(tmp_path):
    store = AuditStore(tmp_path)
    old = store.submit({**product(), 'product_id': 'historical'}, picture())
    live = store.submit({**product(), 'product_id': 'live-agent'}, picture())
    store.consumer_waiting(live['id'], now=100)
    assert store.claim(now=101)['id'] == live['id']
    assert store.claim(now=101)['id'] == old['id']


def test_expired_waiter_does_not_starve_an_older_card(tmp_path):
    store = AuditStore(tmp_path)
    first = store.submit({**product(), 'product_id': 'first'}, picture())
    last = store.submit({**product(), 'product_id': 'last'}, picture())
    store.consumer_waiting(last['id'], now=1)
    assert store.claim(now=100)['id'] == first['id']


def test_live_inspection_lease_is_renewed_but_stale_worker_cannot_renew_new_lease(tmp_path):
    store = AuditStore(tmp_path)
    store.submit(product(), picture())
    original = store.claim(now=100)
    assert store.renew_lease(original, now=900)
    assert store.claim(now=1001) is None
    newer = store.claim(now=1900)
    assert newer['lease_token'] != original['lease_token']
    assert not store.renew_lease(original, now=2000)
    assert store.renew_lease(newer, now=2000)


def test_two_workers_process_different_snapshots_concurrently_once(tmp_path):
    store = AuditStore(tmp_path)
    for key in ('first', 'second'):
        store.submit({**product(), 'product_id': key}, picture())
    both_started = threading.Barrier(2)
    completed = threading.Event()
    events, providers = [], []
    stop = threading.Event()

    class Auditor:
        def run(self, item, folder):
            both_started.wait(timeout=3)
            events.append(item['product_id'])
            if len(events) == 2:
                completed.set()
            return report('approved')

    def factory():
        value = Auditor()
        providers.append(value)
        return value

    workers = start_workers(store, factory, stop)
    try:
        assert completed.wait(5)
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=3)
    assert len(providers) == 2 and providers[0] is not providers[1]
    assert sorted(events) == ['first', 'second']
    assert store.status() == {'approved': 2}


@pytest.mark.parametrize('count', [0, 3, True, '2'])
def test_unbounded_parallelism_is_not_started(tmp_path, count):
    with pytest.raises(ValueError):
        start_workers(AuditStore(tmp_path), lambda: object(), threading.Event(), count=count)
