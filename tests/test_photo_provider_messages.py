import json

import pytest

from listener.provider_errors import ProviderError
from photo_auditor import service
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, product


def test_wait_notification_identifies_adapter_failure_and_retry_preserves_photo(tmp_path):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())

    class Auditor:
        def run(self, *args):
            raise ProviderError('chatgpt', 'usage_limit', 'raw credential must not leak',
                                code='subscription_sharing_usage_limit_exceeded')

    assert service.process_one(store, Auditor())
    waiting = store.get(row['id'])
    public = service.public_result(waiting)
    assert public['provider_error']['code'] == 'subscription_sharing_usage_limit_exceeded'
    assert public['retry_at'] > 0
    sent = []

    class Tg:
        def send(self, *args):
            sent.append(args)

    service.notify(store, Tg(), 1)
    assert len(sent) == 1
    assert 'ограничению использования' in sent[0][1]
    assert 'Это не проверка доступности' in sent[0][1]
    assert 'raw credential' not in json.dumps(public) + sent[0][1]
    assert 'Проверка ожидает доступности ChatGPT' not in sent[0][1]
    # Status remains actionable even after the once-only notice was delivered.
    assert row['id'] in service.status_text(store)
    service.handle_update({'message': {'from': {'id': 1}, 'chat': {'id': 1},
                                      'text': '/retry ' + row['id']}}, store, Tg(), 1)
    assert store.get(row['id'])['status'] == 'queued'
    assert (tmp_path / row['id'] / 'generated.bin').read_bytes() == picture()
    assert store.get(row['id'])['revision'] == 0


@pytest.mark.parametrize('kind', ['auth', 'configuration'])
def test_connection_action_does_not_wait_forever(tmp_path, kind):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())

    class Auditor:
        def run(self, *args):
            raise ProviderError('chatgpt', kind, 'unsafe raw text')

    service.process_one(store, Auditor())
    result = store.get(row['id'])
    assert result['status'] == 'review'
    assert 'программного подключения аудитора' in result['report']['reason']
    assert 'unsafe raw text' not in json.dumps(result)


def test_invalid_response_has_bounded_retries(tmp_path):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())

    class Auditor:
        def run(self, *args):
            raise ProviderError('chatgpt', 'invalid_response', 'unsafe raw text')

    for _ in range(3):
        with store.connect() as db:
            db.execute('UPDATE audits SET retry_at=0 WHERE id=?', (row['id'],))
        assert service.process_one(store, Auditor())
    assert store.get(row['id'])['status'] == 'review'
    assert not service.process_one(store, Auditor())


def test_stranger_cannot_retry_waiting_snapshot(tmp_path):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())
    store.defer(store.claim(), 'quota', provider_wait=True)

    class Tg:
        def send(self, *args):
            pytest.fail('A stranger cannot interact with the queue')

    service.handle_update({'message': {'from': {'id': 2}, 'chat': {'id': 1},
                                      'text': '/retry ' + row['id']}}, store, Tg(), 1)
    assert store.get(row['id'])['status'] == 'waiting_provider'
