"""Mirror immutable previews to Orchestrator; no generation or publication."""
import base64
import json
import logging
import os
import re
import time

from photo_auditor.engine import feedback
from photo_auditor.images import digest, image_bytes
from photo_auditor.integration import AuditHold

log = logging.getLogger('photo_auditor')
SOURCE_RETRY_SECONDS = 300
LOCAL_RETRY_SECONDS = 300
MAX_RETRY_SECONDS = 1800


def _state(values, key, stamp):
    try:
        value = json.loads(values.get(key, '{}'))
        if (isinstance(value, dict) and value.get('stamp') == stamp and
                isinstance(value.get('retry_at', 0), (int, float))):
            return value
    except (TypeError, ValueError):
        pass
    return {'stamp': stamp, 'retry_at': 0, 'failures': 0, 'source_pending': False}


def _save(store, key, state):
    with store.connect() as db:
        db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(state, sort_keys=True)))


def _defer(store, key, state, now, initial, kind):
    failures = state.get('failures', 0) + 1
    state.update(failures=failures, retry_at=now + min(MAX_RETRY_SECONDS, initial * 2 ** min(failures - 1, 6)),
                 reason=kind)
    _save(store, key, state)


def sync(store, deliver=None, limit=4, *, now=None):
    """Bound work and preserve retries, including late-arriving supplier files."""
    now = time.time() if now is None else now
    limit = max(1, min(50, int(limit)))
    if deliver is None:
        if not os.environ.get('ORCH_API_TOKEN'):
            return
        import httpx
        from listener.client import ApiClient
        with httpx.Client(timeout=30, trust_env=False) as http:
            api = ApiClient(os.environ.get('ORCH_API_URL', 'http://127.0.0.1:8790'),
                            os.environ['ORCH_API_TOKEN'], http=http)
            return sync(store, lambda p: api._post('/photo-reviews', p), limit, now=now)
    from photo_auditor import runtime
    with store.connect() as db:
        rows = db.execute("SELECT id FROM audits a WHERE status IN ('approved','review','regenerate') "
                          "AND NOT EXISTS (SELECT 1 FROM audits n WHERE n.product_id=a.product_id AND n.revision>a.revision) "
                          "ORDER BY created DESC").fetchall()
        saved = {r[0]: r[1] for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'review-sync:%' "
                                              "OR key LIKE 'review-sync-retry:%'")}
    count = attempts = inspected = 0
    for item in rows:
        row = store.get(item['id'])
        match = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
        if not match or not row.get('report'):
            continue
        marker = 'review-sync:' + row['id']
        retry_key = 'review-sync-retry:' + row['id']
        from photo_auditor import correction_reference
        reference_stamp = correction_reference.stamp(store.root.parent.parent, row['id'])
        stamp = digest(json.dumps({'sha256': row['sha256'], 'report': row['report'], 'status': row['status'], 'source_sync': 2,
                                   'correction_reference': reference_stamp},
                                  sort_keys=True, ensure_ascii=False).encode())
        state = _state(saved, retry_key, stamp)
        mirrored = saved.get(marker) == stamp
        if (mirrored and not state.get('source_pending')) or state['retry_at'] > now:
            continue
        inspected += 1
        if inspected > max(20, limit * 5):
            break
        try:
            if runtime.content_brief(store.root.parent.parent, row['product']) is None:
                _defer(store, retry_key, state, now, LOCAL_RETRY_SECONDS, 'brief_missing')
                continue
            data = image_bytes((store.root / row['id'] / 'generated.bin').read_bytes())
            if digest(data) != row['sha256']:
                raise ValueError('Changed immutable card snapshot')
            payload = {**{k: row[k] for k in ('revision', 'sha256', 'status')},
                       'audit_id': row['id'], 'job_id': int(match[1]),
                       'brand': row['product']['brand'], 'model': row['product']['model'],
                       'summary': feedback(row['report'])[:6000],
                       'preview': base64.b64encode(data).decode()}
            # Uploaded checkpoints may have moved their rendered output away;
            # the separately frozen supplier remains valid and must be reused.
            from photo_auditor.progress import _supplier
            has_source = False
            try:
                source = _supplier(store.root.parent.parent, row['product_id'], row['product']['brand'],
                                   row['product']['model'], row)
                if source:
                    original = image_bytes(source.read_bytes())
                    payload.update(supplier=base64.b64encode(original).decode(), supplier_sha256=digest(original))
                    has_source = True
            except (AuditHold, OSError, ValueError):
                pass
            reference = correction_reference.read(store.root.parent.parent, row)
            if reference:
                payload.update(correction_reference=base64.b64encode(reference['data']).decode(),
                               correction_reference_sha256=reference['reference_sha256'],
                               correction_reference_url=reference['page_url'], correction_reference_note=reference['note'])
                has_source = True
            if mirrored and not has_source:
                state.update(source_pending=True, retry_at=now + SOURCE_RETRY_SECONDS)
                _save(store, retry_key, state)
                continue
        except (AuditHold, OSError, ValueError, KeyError, TypeError) as exc:
            _defer(store, retry_key, state, now, LOCAL_RETRY_SECONDS, 'invalid_snapshot')
            log.warning('Синхронизация карточки %s отложена: %s', row['id'][:12], type(exc).__name__)
            continue
        attempts += 1
        try:
            result = deliver(payload)
            if not (result.get('registered') or result.get('reason') == 'superseded'):
                _defer(store, retry_key, state, now, 30, 'not_registered')
            else:
                source_pending = not has_source and result.get('reason') != 'superseded'
                state.update(failures=0, source_pending=source_pending,
                             retry_at=now + SOURCE_RETRY_SECONDS if source_pending else 0,
                             reason='source_pending' if source_pending else None)
                with store.connect() as db:
                    if type(result.get('supports_publication')) is bool:
                        db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                   ('photo-review-route:' + row['id'], json.dumps({'supports_publication': result['supports_publication']})))
                    db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                               (marker, stamp))
                    db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                               (retry_key, json.dumps(state, sort_keys=True)))
                count += 1
        except Exception as exc:
            _defer(store, retry_key, state, now, 30, 'delivery_error')
            log.warning('Синхронизация карточки %s отложена: %s', row['id'][:12], type(exc).__name__)
            response = getattr(exc, 'response', None)
            if response is None or getattr(response, 'status_code', 0) not in (400, 409, 413, 422):
                break
        if attempts >= limit:
            break
    return count
