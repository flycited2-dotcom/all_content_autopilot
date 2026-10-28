import io
import json
import sqlite3

import pytest
from PIL import Image

from photo_auditor.store import AuditStore, sanitize_provider_error


QUOTA = {'provider': 'chatgpt', 'kind': 'usage_limit',
         'code': 'subscription_sharing_usage_limit_exceeded'}


def picture(color='white'):
    output = io.BytesIO()
    Image.new('RGB', (120, 80), color).save(output, 'PNG')
    return output.getvalue()


def product(revision=0):
    return {'product_id': 'fotogen-123', 'revision': revision, 'brand': 'Midea', 'model': 'ABC-12-RU'}


def report(verdict):
    return {'verdict': verdict, 'reason': 'Не завершено', 'checks': [], 'sources': [],
            'dimensions_note': 'Не измерены'}


def waiting_job(store, *, now=100):
    row = store.submit(product(), picture())
    claimed = store.claim(now=now)
    store.defer(claimed, 'untrusted response with secret bearer value', now=now,
                retry_at=900, provider_wait=True, provider_error=QUOTA)
    return store.get(row['id'])


def test_legacy_database_migration_preserves_existing_audit_and_is_repeatable(tmp_path):
    with sqlite3.connect(tmp_path / 'audit.sqlite3') as db:
        db.execute('CREATE TABLE audits (id TEXT PRIMARY KEY, product_id TEXT, revision INTEGER, '
                   'product TEXT, sha256 TEXT, status TEXT, report TEXT, created REAL, lease REAL, '
                   'tries INTEGER DEFAULT 0, retry_at REAL DEFAULT 0, notified INTEGER DEFAULT 0, '
                   'UNIQUE(product_id, revision))')
        db.execute('INSERT INTO audits(id,product_id,revision,product,sha256,status,created) '
                   'VALUES(?,?,?,?,?,?,?)',
                   ('legacy', product()['product_id'], 0, json.dumps(product()), 'a' * 64,
                    'waiting_provider', 10))
    for _ in range(2):
        store = AuditStore(tmp_path)
        assert store.get('legacy')['provider_error'] is None
        assert store.status() == {'waiting_provider': 1}
        assert store.get('legacy')['sha256'] == 'a' * 64
    with store.connect() as db:
        assert [row[1] for row in db.execute('PRAGMA table_info(audits)')].count('provider_error') == 1


def test_provider_wait_persists_safe_diagnostic_without_raw_reason_or_report(tmp_path):
    store = AuditStore(tmp_path)
    row = waiting_job(store)
    assert row['provider_error'] == QUOTA
    assert row['retry_at'] == 900
    assert row['report'] is None
    with store.connect() as db:
        saved = dict(db.execute('SELECT * FROM audits').fetchone())
    assert json.loads(saved['provider_error']) == QUOTA
    assert 'untrusted response' not in str(saved)
    assert 'bearer value' not in str(saved)
    assert AuditStore(tmp_path).get(row['id'])['provider_error'] == QUOTA


@pytest.mark.parametrize('value,expected', [
    ({**QUOTA, 'message': 'secret', 'token': 'secret'}, QUOTA),
    ({**QUOTA, 'code': 'secret bearer value'}, {**QUOTA, 'code': None}),
    ({**QUOTA, 'code': {'token': 'secret'}}, {**QUOTA, 'code': None}),
    ({**QUOTA, 'kind': 'secret bearer value'}, None),
    ({**QUOTA, 'provider': 'secret bearer value'}, None),
    ('secret bearer value', None),
])
def test_provider_diagnostic_uses_fixed_allowlists(value, expected):
    assert sanitize_provider_error(value) == expected


@pytest.mark.parametrize('retry_at', [float('nan'), float('inf'), 'secret', True])
def test_provider_wait_does_not_store_invalid_retry_timestamp(tmp_path, retry_at):
    store = AuditStore(tmp_path)
    store.submit(product(), picture())
    store.defer(store.claim(now=100), 'untrusted', now=100, retry_at=retry_at,
                provider_wait=True, provider_error=QUOTA)
    assert store.waiting()[0]['retry_at'] == 400


def test_retry_waiting_reuses_identical_snapshot_and_does_not_create_generation(tmp_path):
    store = AuditStore(tmp_path)
    row = waiting_job(store)
    folder = tmp_path / row['id']
    before = {file.name: file.read_bytes() for file in folder.iterdir()}
    store.notified(row['id'])
    assert store.waiting()[0]['id'] == row['id']
    assert not store.recheck(row['id'])
    assert store.retry_waiting(row['id'])
    after = store.get(row['id'])
    assert after['status'] == 'queued'
    assert after['id'] == row['id'] and after['revision'] == row['revision']
    assert after['sha256'] == row['sha256'] and after['tries'] == row['tries']
    assert after['retry_at'] == 0 and after['provider_error'] is None
    assert {file.name: file.read_bytes() for file in folder.iterdir()} == before
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM audits').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM audit_history').fetchone()[0] == 0
    assert not store.retry_waiting(row['id'])
    assert not store.retry_waiting('missing')
    assert store.claim(now=101)['id'] == row['id']


def test_retry_waiting_rejects_changed_snapshot_without_changing_queue(tmp_path):
    store = AuditStore(tmp_path)
    row = waiting_job(store)
    (tmp_path / row['id'] / 'generated.bin').write_bytes(picture('black'))
    with pytest.raises(ValueError, match='Сохранённый снимок изменился'):
        store.retry_waiting(row['id'])
    assert store.get(row['id'])['status'] == 'waiting_provider'
    assert store.get(row['id'])['provider_error'] == QUOTA


def test_retry_and_status_waiting_exclude_superseded_revision(tmp_path):
    store = AuditStore(tmp_path)
    older = store.submit(product(), picture())
    store.finish(store.claim(now=100), report('regenerate'))
    newer = store.submit(product(1), picture('black'))
    store.defer(store.claim(now=101), 'quota', now=101, provider_wait=True, provider_error=QUOTA)
    # A stale historical wait must never reopen an older revision.
    with store.connect() as db:
        db.execute("UPDATE audits SET status='waiting_provider' WHERE id=?", (older['id'],))
    assert not store.retry_waiting(older['id'])
    assert [row['id'] for row in store.waiting()] == [newer['id']]
    assert store.retry_waiting(newer['id'])


@pytest.mark.parametrize('limit', [0, 51, True, '10'])
def test_status_waiting_requires_bounded_integer_limit(tmp_path, limit):
    with pytest.raises(ValueError):
        AuditStore(tmp_path).waiting(limit)


def test_terminal_decision_and_recheck_clear_previous_provider_diagnostic(tmp_path):
    store = AuditStore(tmp_path)
    row = waiting_job(store)
    store.finish(store.claim(now=901), report('review'))
    assert store.get(row['id'])['provider_error'] is None
    with store.connect() as db:
        db.execute('UPDATE audits SET provider_error=? WHERE id=?', (json.dumps(QUOTA), row['id']))
    assert store.recheck(row['id'])
    assert store.get(row['id'])['provider_error'] is None
