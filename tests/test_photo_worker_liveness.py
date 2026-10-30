import sqlite3
import threading
from types import SimpleNamespace

import httpx

from photo_auditor import service
from photo_auditor.store import AuditStore


class ImmediateStop:
    def __init__(self, stop_after=2):
        self.stopped = False
        self.waits = []
        self.stop_after = stop_after

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.waits.append(delay)
        self.stopped = len(self.waits) >= self.stop_after
        return self.stopped


def test_transient_claim_error_does_not_remove_worker(monkeypatch, caplog):
    calls, health = [], {}
    stop = ImmediateStop()

    def process(*args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError('private-token-must-not-appear')
        return False

    monkeypatch.setattr(service, 'process_one', process)
    service.worker_loop(object(), object(), stop, health)
    assert len(calls) == 2
    assert stop.waits == [2, 2]
    assert health['state'] == 'stopped' and health['last_error'] is None
    assert health['consecutive_errors'] == 0 and health['last_success'] >= health['last_poll']
    assert 'OperationalError' in caplog.text
    assert 'private-token-must-not-appear' not in caplog.text


def test_repeated_queue_errors_have_bounded_backoff(monkeypatch):
    def fail(*args):
        raise OSError('unavailable')

    monkeypatch.setattr(service, 'process_one', fail)
    stop, health = ImmediateStop(stop_after=8), {}
    service.worker_loop(object(), object(), stop, health)
    assert stop.waits == [2, 4, 8, 16, 30, 30, 30, 30]
    assert health['consecutive_errors'] == 8 and health['last_error'] == 'OSError'


def test_idle_worker_preserves_two_second_poll_interval(monkeypatch):
    monkeypatch.setattr(service, 'process_one', lambda *args: False)
    stop = ImmediateStop(stop_after=1)
    service.worker_loop(object(), object(), stop)
    assert stop.waits == [2]


def test_health_observes_dead_worker_instead_of_http_readiness(tmp_path):
    store = AuditStore(tmp_path)
    alive = SimpleNamespace(is_alive=lambda: True,
                            audit_health={'last_poll': 90, 'state': 'idle', 'last_error': None})
    dead = SimpleNamespace(is_alive=lambda: False,
                           audit_health={'last_poll': 50, 'state': 'stopped', 'last_error': 'OperationalError'})
    value = service.worker_health([alive, dead], store, now=100)
    assert not value['ok']
    assert value['workers']['expected'] == 2 and value['workers']['alive'] == 1
    assert value['workers']['details'][1]['last_poll_age'] == 50
    assert value['queue'] == {}
    assert not service.worker_health([], store)['ok']


def test_health_endpoint_is_authenticated_and_status_schema_unchanged(tmp_path):
    store = AuditStore(tmp_path)
    server = service.make_server(store, 'private-health-token', port=0)
    server.audit_workers = [SimpleNamespace(is_alive=lambda: False,
                                           audit_health={'state': 'stopped', 'last_error': None})]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            assert client.get(base + '/health').status_code == 401
            headers = {'X-Audit-Token': 'private-health-token'}
            response = client.get(base + '/health', headers=headers)
            assert response.status_code == 200 and not response.json()['ok']
            assert response.json()['workers']['alive'] == 0
            assert 'private-health-token' not in response.text
            assert client.get(base + '/status', headers=headers).json() == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
