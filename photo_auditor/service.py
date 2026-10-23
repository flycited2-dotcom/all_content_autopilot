"""Loopback API, audit worker and owner-only Telegram interface."""
import base64
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from listener.provider_errors import ProviderError
from orchestrator_bot.api import text_chunks
from photo_auditor.engine import feedback

log = logging.getLogger("photo_auditor")
MAX_BODY = 29 * 1024 * 1024
LABELS = {"approved": "✅ Проверка пройдена", "regenerate": "🔄 Вернуть на генерацию", "review": "🔎 Нужна ручная проверка"}


def public_result(audit):
    report = audit["report"]
    return {"id": audit["id"], "status": audit["status"], "sha256": audit["sha256"],
            "product": audit["product"], "report": report, "feedback": feedback(report) if report else None}


def make_server(store, token, port=8793):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if not hmac.compare_digest(self.headers.get("X-Audit-Token", ""), token):
                self.send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/status":
                return self.send_json(200, store.status())
            if self.path.startswith("/audits/"):
                row = store.get(self.path.removeprefix("/audits/"))
                return self.send_json(200 if row else 404, public_result(row) if row else {"error": "not found"})
            self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/audits":
                return self.send_json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self.send_json(413, {"error": "body too large"})
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict) or set(value) - {"product", "generated", "supplier"}:
                    raise ValueError()
                generated = base64.b64decode(value["generated"], validate=True)
                supplier = base64.b64decode(value["supplier"], validate=True) if value.get("supplier") else None
                audit = store.submit(value["product"], generated, supplier)
            except (ValueError, TypeError, KeyError):
                return self.send_json(400, {"error": "Invalid product, revision or image; snapshot conflict"})
            self.send_json(200, public_result(audit))
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def process_one(store, auditor, audit_id=None):
    row = store.claim(audit_id=audit_id)
    if row is None:
        return False
    try:
        path = store.root / row["id"]
        from photo_auditor.images import digest
        if digest((path / "generated.bin").read_bytes()) != row["sha256"]:
            store.finish(row, {"verdict": "review", "reason": "Снимок изменился после постановки проверки.",
                               "checks": [], "sources": [], "dimensions_note": "Проверка остановлена."})
        else:
            store.finish(row, auditor.run(row["product"], path))
    except ProviderError as exc:
        log.warning("Проверка ожидает провайдера: %s", exc.kind)
        store.defer(row, "Проверка не завершена: провайдер недоступен (" + exc.kind + ").", retry_at=exc.retry_at)
    except Exception as exc:
        log.warning("Проверка не завершена: %s", type(exc).__name__)
        store.defer(row, "Проверка источников/изображений не завершена: " + type(exc).__name__)
    return True


def worker_loop(store, auditor, stop):
    while not stop.is_set():
        if not process_one(store, auditor):
            stop.wait(2)


def notify(store, tg, owner):
    for row in store.pending_notifications():
        if row['product_id'].startswith('fotogen-') and row['status'] == 'regenerate':
            # The generator receives this feedback over the API. Do not flood the
            # owner with intermediate revisions that are already being corrected.
            store.notified(row['id'])
            continue
        report = row["report"]
        folder = store.root / row["id"]
        sheet = folder / "comparison.jpg"
        if sheet.exists():
            tg.send_photo(owner, sheet, caption=f"{row['product']['brand']} {row['product']['model']}: GENERATED / SOURCE")
        text = (f"{LABELS[row['status']]}\n{row['product']['brand']} {row['product']['model']}\n"
                f"Ревизия: {row['revision']}. Проверка: {row['id'][:12]}\n\n{feedback(report)}\n\n"
                f"Размеры: {report['dimensions_note']}\n\nРешение относится к фотографии; публикацию подтверждает владелец.")
        for part in text_chunks(text):
            tg.send(owner, part)
        store.notified(row["id"])


def handle_update(update, store, tg, owner):
    message = update.get("message") or {}
    if message.get("from", {}).get("id") != owner or message.get("chat", {}).get("id") != owner:
        return
    text = (message.get("text") or "").strip()
    if text in ("/start", "/help"):
        tg.send(owner, "Я проверяю фотографии по точной номенклатуре. Отправьте фото с подписью «Бренд | Модель» "
                       "или подключите фотоагент. /status — очередь. При замечаниях — до двух повторных генераций; "
                       "при нехватке эталонов — ручная проверка.")
    elif text == "/status":
        counts = store.status()
        tg.send(owner, "Контроль фотографий: " + (", ".join(f"{k}: {v}" for k, v in counts.items()) or "очередь пуста"))
    elif message.get("photo") or message.get("document"):
        caption = (message.get("caption") or "").strip().split("|", 1)
        if len(caption) != 2 or not all(s.strip() for s in caption):
            tg.send(owner, "Нужна точная номенклатура: отправьте фото с подписью «Бренд | Модель».")
            return
        media = message["photo"][-1] if message.get("photo") else message["document"]
        if media.get("file_size", 0) > 10 * 1024 * 1024:
            tg.send(owner, "Фото должно быть не больше 10 МБ.")
            return
        photo = tg.download_voice(media["file_id"])  # same Telegram getFile transport, any media
        product = {"product_id": f"telegram-{message['message_id']}", "revision": 0,
                   "brand": caption[0].strip(), "model": caption[1].strip()}
        try:
            row = store.submit(product, photo)
        except ValueError:
            tg.send(owner, "Не удалось принять фото: проверьте формат PNG/JPEG/WEBP и номенклатуру.")
            return
        tg.send(owner, f"Принял на проверку: {product['brand']} {product['model']}. № {row['id'][:12]}")
    else:
        tg.send(owner, "Отправьте фото с подписью «Бренд | Модель». /help — порядок проверки.")


def run_bot(store, tg, owner, stop):
    while not stop.is_set():
        try:
            notify(store, tg, owner)
            for update in tg.get_updates(store.offset(), timeout=20):
                handle_update(update, store, tg, owner)
                store.offset(update["update_id"] + 1)
        except Exception as exc:
            # Telegram HTTP errors contain credential-bearing URLs; never log them.
            log.warning("Telegram временно недоступен: %s", type(exc).__name__)
            stop.wait(10)
