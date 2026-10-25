"""Loopback API, audit worker and owner-only Telegram interface."""
import base64
import hmac
import json
import logging
import os
import re
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
        delay = 3600 if exc.kind == 'usage_limit' else 300
        store.defer(row, "Проверка не завершена: провайдер недоступен (" + exc.kind + ").",
                    retry_at=exc.retry_at or time.time() + delay, provider_wait=True)
    except Exception as exc:
        log.warning("Проверка не завершена: %s", type(exc).__name__)
        store.defer(row, "Проверка источников/изображений не завершена: " + type(exc).__name__)
    return True


def worker_loop(store, auditor, stop):
    while not stop.is_set():
        if not process_one(store, auditor):
            stop.wait(2)


def watch_parent(parent, stop):
    """Task Scheduler stops conhost, so its Python child must stop itself."""
    from listener.lifecycle import parent_alive
    while not stop.wait(2):
        if not parent_alive(parent):
            log.info('Родительский процесс завершён — фотоаудитор останавливается')
            stop.set()


def notify(store, tg, owner):
    for row in store.waiting_notifications():
        tg.send(owner, f"⏳ {row['product']['brand']} {row['product']['model']}: фото сохранено. "
                       "Проверка ожидает доступности ChatGPT и продолжится автоматически. Новая генерация не запускается.")
        store.notified(row['id'])
    for row in store.pending_notifications():
        generated_job = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
        intermediate = bool(generated_job and row['status'] == 'regenerate')
        if generated_job and row['status'] == 'approved':
            from photo_auditor import runtime
            root = store.root.parent.parent
            job_id = int(generated_job[1])
            scope_path = root / 'runs/photo-auditor/job-scope' / (str(job_id) + '.json')
            if scope_path.exists():
                intermediate = runtime._read(root, job_id).get('mode') == 'research'
        if intermediate:
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
        parts = list(text_chunks(text))
        keyboard = [[{'text': title, 'callback_data': f"qa:{code}:{row['id']}"}]
                    for title, code in [('Соответствует товару', 'match'),
                                        ('Есть расхождение', 'mismatch'), ('Не могу определить', 'unclear')]]
        parts[-1] += '\n\nОцените результат для калибровки аудитора. Оценка не запускает публикацию.'
        # Keep the extra footer within Telegram's limit even for long source lists.
        chunks = list(text_chunks('\n'.join(parts)))
        for n, part in enumerate(chunks):
            tg.send(owner, part, keyboard if n == len(chunks) - 1 else None)
        store.notified(row["id"])


def recover_pipeline(store, deliver=None):
    """Only the latest decision can resume a failed card; the server deduplicates it."""
    if deliver is None:
        token = os.environ.get('ORCH_API_TOKEN')
        if not token:
            return
        import httpx
        from listener.client import ApiClient
        with httpx.Client(timeout=20, trust_env=False) as http:
            api = ApiClient(os.environ.get('ORCH_API_URL', 'http://127.0.0.1:8790'), token, http=http)
            return recover_pipeline(store, lambda payload: api._post('/photo-audit-result', payload))
    for row in store.recovery_results():
        match = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
        if not match:
            continue
        # Research can resume only together with its frozen, verified facts.
        from photo_auditor import runtime, progress
        root = store.root.parent.parent
        scope_path = root / 'runs/photo-auditor/job-scope' / (match[1] + '.json')
        if scope_path.exists():
            scope = runtime._read(root, int(match[1]))
            if scope.get('mode') == 'research' and not progress.restore_research(
                    root, row['product_id'], row['product']['brand'], row['product']['model']):
                continue
        payload = {'job_id': int(match[1]), 'audit_id': row['id'], 'attempt': row['attempt'],
                   'revision': row['revision'], 'status': row['status'], 'sha256': row['sha256'],
                   'brand': row['product']['brand'], 'model': row['product']['model']}
        try:
            result = deliver(payload)
            if result.get('handled'):
                store.recovery_delivered(row['id'], row['attempt'], result)
        except Exception as exc:
            log.warning('Доставка решения в конвейер отложена: %s', type(exc).__name__)
            break


def handle_update(update, store, tg, owner):
    callback = update.get('callback_query')
    if callback:
        if callback.get('from', {}).get('id') != owner or callback.get('message', {}).get('chat', {}).get('id') != owner:
            return
        data = callback.get('data', '').split(':')
        import re
        if len(data) == 3 and data[0] == 'qa' and re.fullmatch('[a-f0-9]{32}', data[2]):
            try:
                saved = store.feedback(data[2], data[1])
            except ValueError:
                saved = False
            tg.answer_callback(callback['id'], 'Оценка сохранена' if saved else 'Проверка не найдена')
        return
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
            recover_pipeline(store)
            notify(store, tg, owner)
            for update in tg.get_updates(store.offset(), timeout=20):
                handle_update(update, store, tg, owner)
                store.offset(update["update_id"] + 1)
        except Exception as exc:
            # Telegram HTTP errors contain credential-bearing URLs; never log them.
            log.warning("Telegram временно недоступен: %s", type(exc).__name__)
            stop.wait(10)
