"""Photo-agent hook: never uploads an unchecked output; bounded regeneration."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import sqlite3
from pathlib import Path

import httpx

from photo_auditor.credentials import load
from photo_auditor.images import digest, image_bytes
from photo_auditor.store import MAX_REGENERATIONS
from listener.pending import write_json


class AuditHold(RuntimeError):
    """The output must remain outside the photo-agent upload outbox."""

    def __str__(self):
        return 'PHOTO_AUDIT_HOLD: ' + super().__str__()


def _permit(root, product_id, fingerprint):
    # Short filename for Windows MAX_PATH; full identities are checked in signed content.
    key = hashlib.sha256((product_id + ":" + fingerprint).encode()).hexdigest()[:32]
    return Path(root) / "runs/photo-auditor/permits" / (key + ".json")


def require_permit(root, product_id, path):
    """Also gates retries from the photo agent's durable upload outbox."""
    fingerprint = digest(Path(path).read_bytes())
    payload = {"product_id": product_id, "sha256": fingerprint}
    signature = hmac.new(load(root)["api_token"].encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    try:
        value = json.loads(_permit(root, product_id, fingerprint).read_text(encoding="utf-8"))
        if value["payload"] == payload and hmac.compare_digest(value["signature"], signature):
            if _current_approval(root, product_id, fingerprint):
                return
    except (OSError, KeyError, ValueError, TypeError):
        pass
    raise AuditHold("Нет подтверждения контроля именно этого файла; выгрузка остановлена")


def _current_approval(root, product_id, fingerprint):
    database = Path(root) / 'runs/photo-auditor/audit.sqlite3'
    try:
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as db:
            row = db.execute('SELECT status,sha256 FROM audits WHERE product_id=? ORDER BY revision DESC LIMIT 1',
                             (product_id,)).fetchone()
        return bool(row and row[0] == 'approved' and row[1] == fingerprint)
    except sqlite3.Error:
        return False


async def checked_output(root, product_id, brand, model, generated_path, supplier_path, regenerate,
                         *, url="http://127.0.0.1:8793", timeout=1200, client=None, interval=3,
                         start_revision=0, repair_reference=False):
    if url != "http://127.0.0.1:8793":
        raise AuditHold("Допустим только локальный адрес службы контроля")
    config = load(root)
    if type(start_revision) is not int or not 0 <= start_revision <= MAX_REGENERATIONS:
        raise AuditHold('Неверная ревизия восстановления')
    from photo_auditor import progress
    headers = {"X-Audit-Token": config["api_token"]}
    original = image_bytes(Path(supplier_path).read_bytes()) if supplier_path else None
    path = Path(generated_path)
    owns = client is None
    http = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    deadline = time.monotonic() + timeout
    try:
        for revision in range(start_revision, MAX_REGENERATIONS + 1):
            if time.monotonic() >= deadline:
                raise AuditHold("Контроль ещё не завершён; общий срок проверки и исправлений исчерпан")
            data = image_bytes(path.read_bytes())
            fingerprint = digest(data)
            progress.generated(root, product_id, brand, model, revision, path, supplier=supplier_path)
            product = {"product_id": product_id, "brand": brand, "model": model, "revision": revision}
            body = {"product": product, "generated": base64.b64encode(data).decode()}
            if original:
                body["supplier"] = base64.b64encode(original).decode()
            response = await http.post(url + "/audits", json=body, headers=headers)
            response.raise_for_status()
            result = response.json()
            while result["status"] in ("queued", "checking", "waiting_provider"):
                if time.monotonic() >= deadline:
                    raise AuditHold("Контроль ещё не завершён; результат не выгружен")
                await asyncio.sleep(interval)
                response = await http.get(url + "/audits/" + result["id"], headers=headers)
                response.raise_for_status()
                result = response.json()
            if result["sha256"] != fingerprint or result["product"] != product or digest(path.read_bytes()) != fingerprint:
                raise AuditHold("Результат проверки относится к другому снимку/товару")
            if result["status"] == "approved":
                payload = {"product_id": product_id, "sha256": fingerprint}
                signature = hmac.new(config["api_token"].encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()
                write_json(_permit(root, product_id, fingerprint), {"payload": payload, "signature": signature})
                return path
            if result['status'] == 'review' and repair_reference and revision < MAX_REGENERATIONS:
                from photo_auditor.store import AuditStore
                store = AuditStore(Path(root) / 'runs/photo-auditor')
                if store.request_reference_repair(result['id']):
                    response = await http.get(url + '/audits/' + result['id'], headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    if result['sha256'] != fingerprint or result['product'] != product:
                        raise AuditHold('Изменился снимок при подготовке официального исходника')
            if result["status"] != "regenerate" or revision >= MAX_REGENERATIONS:
                raise AuditHold("Фото остановлено для ручной проверки: " + result["id"][:12])
            if time.monotonic() >= deadline:
                raise AuditHold("Контроль ещё не завершён; повторная генерация не запускалась")
            progress.starting(root, product_id, brand, model, revision + 1, paid=not repair_reference)
            path = Path(await regenerate(result["feedback"]))
        raise AuditHold("Исчерпан лимит исправлений")
    except httpx.HTTPError:
        raise AuditHold("Служба контроля недоступна; фото не выгружено") from None
    finally:
        if owns:
            await http.aclose()
