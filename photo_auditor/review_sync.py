"""Mirror immutable previews to Orchestrator; no generation or publication."""
import base64
import json
import logging
import os
import re

from photo_auditor.engine import feedback
from photo_auditor.images import digest, image_bytes
from photo_auditor.integration import AuditHold

log = logging.getLogger('photo_auditor')


def sync(store, deliver=None, limit=4):
    if deliver is None:
        if not os.environ.get('ORCH_API_TOKEN'):
            return
        import httpx
        from listener.client import ApiClient
        with httpx.Client(timeout=30, trust_env=False) as http:
            api = ApiClient(os.environ.get('ORCH_API_URL', 'http://127.0.0.1:8790'),
                            os.environ['ORCH_API_TOKEN'], http=http)
            return sync(store, lambda p: api._post('/photo-reviews', p), limit)
    from photo_auditor import runtime
    with store.connect() as db:
        rows = db.execute("SELECT id FROM audits a WHERE status IN ('approved','review','regenerate') "
                          "AND NOT EXISTS (SELECT 1 FROM audits n WHERE n.product_id=a.product_id AND n.revision>a.revision) "
                          "ORDER BY created DESC").fetchall()
        sent = {r[0]: r[1] for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'review-sync:%'")}
    count = 0
    for item in rows:
        row = store.get(item['id'])
        match = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
        if not match or not row.get('report'):
            continue
        marker = 'review-sync:' + row['id']
        stamp = digest(json.dumps({'sha256': row['sha256'], 'report': row['report'], 'status': row['status'], 'source_sync': 1},
                                  sort_keys=True, ensure_ascii=False).encode())
        if sent.get(marker) == stamp:
            continue
        try:
            if runtime.content_brief(store.root.parent.parent, row['product']) is None:
                continue
            data = image_bytes((store.root / row['id'] / 'generated.bin').read_bytes())
            if digest(data) != row['sha256']:
                continue
            payload = {**{k: row[k] for k in ('revision', 'sha256', 'status')},
                       'audit_id': row['id'], 'job_id': int(match[1]),
                       'brand': row['product']['brand'], 'model': row['product']['model'],
                       'summary': feedback(row['report'])[:6000],
                       'preview': base64.b64encode(data).decode()}
            # Uploaded checkpoints may have moved their rendered output away;
            # the separately frozen supplier remains valid and must be reused.
            from photo_auditor.progress import _supplier
            try:
                source = _supplier(store.root.parent.parent, row['product_id'], row['product']['brand'],
                                   row['product']['model'], row)
                if source:
                    original = image_bytes(source.read_bytes())
                    payload.update(supplier=base64.b64encode(original).decode(), supplier_sha256=digest(original))
            except (AuditHold, OSError, ValueError):
                pass
        except (AuditHold, OSError, ValueError, KeyError, TypeError) as exc:
            log.warning('Непригодный снимок карточки пропущен: %s', type(exc).__name__)
            continue
        try:
            result = deliver(payload)
            if not (result.get('registered') or result.get('reason') == 'superseded'):
                continue
            with store.connect() as db:
                db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (marker, stamp))
            count += 1
            if count >= limit:
                break
        except Exception as exc:
            log.warning('Синхронизация карточки отложена: %s', type(exc).__name__)
            response = getattr(exc, 'response', None)
            if response is not None and getattr(response, 'status_code', 0) in (400, 409, 413, 422):
                continue
            break
    return count
