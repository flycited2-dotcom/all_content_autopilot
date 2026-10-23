"""Photo-agent hook: never uploads an unchecked output; bounded regeneration."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx

from photo_auditor.credentials import load
from photo_auditor.images import digest, image_bytes
from photo_auditor.store import MAX_REGENERATIONS
from listener.pending import write_json


class AuditHold(RuntimeError):
    """The output must remain outside the photo-agent upload outbox."""


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
            return
    except (OSError, KeyError, ValueError, TypeError):
        pass
    raise AuditHold("Нет подтверждения контроля именно этого файла; выгрузка остановлена")


async def checked_output(root, product_id, brand, model, generated_path, supplier_path, regenerate,
                         *, url="http://127.0.0.1:8793", timeout=1200, client=None, interval=3):
    if url != "http://127.0.0.1:8793":
        raise AuditHold("Допустим только локальный адрес службы контроля")
    config = load(root)
    headers = {"X-Audit-Token": config["api_token"]}
    original = image_bytes(Path(supplier_path).read_bytes()) if supplier_path else None
    path = Path(generated_path)
    owns = client is None
    http = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    try:
        for revision in range(MAX_REGENERATIONS + 1):
            data = image_bytes(path.read_bytes())
            fingerprint = digest(data)
            product = {"product_id": product_id, "brand": brand, "model": model, "revision": revision}
            body = {"product": product, "generated": base64.b64encode(data).decode()}
            if original:
                body["supplier"] = base64.b64encode(original).decode()
            response = await http.post(url + "/audits", json=body, headers=headers)
            response.raise_for_status()
            result = response.json()
            deadline = time.monotonic() + timeout
            while result["status"] in ("queued", "checking"):
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
            if result["status"] != "regenerate" or revision >= MAX_REGENERATIONS:
                raise AuditHold("Фото остановлено для ручной проверки: " + result["id"][:12])
            path = Path(await regenerate(result["feedback"]))
        raise AuditHold("Исчерпан лимит исправлений")
    except httpx.HTTPError:
        raise AuditHold("Служба контроля недоступна; фото не выгружено") from None
    finally:
        if owns:
            await http.aclose()
