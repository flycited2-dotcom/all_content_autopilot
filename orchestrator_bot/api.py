"""Локальный HTTP API для слушателя на ПК (только 127.0.0.1, доступ через SSH-туннель)."""
from __future__ import annotations

import json
import base64
import binascii
import logging
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestrator_bot import actions, intents, workers
from orchestrator_bot.intents import Outcome, Result

log = logging.getLogger("orchestrator_bot")
EDITORIAL = ("run_editorial", "revise_editorial")
CAPABILITIES = ["list_products", "product_selection", "full_catalog", "supplier_photo_import", "supplier_photo_inventory", "document_import"]


def text_chunks(text: str, maximum=3800):
    """Не обрезать длинные списки; лимит учитывает пары UTF-16 для эмодзи."""
    chunk, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > maximum:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield "".join(chunk)


def start_of_day(now: float) -> float:
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def handle_intent(store, factory, send, msg_id, raw_intent, threshold=40) -> dict:
    intent = intents.parse_intent(raw_intent) if isinstance(raw_intent, str) else raw_intent
    reply, acts = intents.normalize(intent)          # разбор до отметки: битое намерение не «сжигает» сообщение
    if not store.mark_intent(msg_id):
        response = store.get_response(msg_id)
        # Inspections are read-only; supplier dispatch has its own durable
        # receipt. Recover both after a crash instead of consuming the photo.
        replayable = {"enqueue_supplier_products", "inspect_supplier_photo"}
        if any(a["type"] == "inspect_supplier_photo" for a in acts):
            replayable.add("ask")
        recoverable_photo = bool(acts) and all(a["type"] in replayable for a in acts)
        if response is not None or not recoverable_photo:
            store.mark_done(msg_id)
            return {**(response or {"ok": True, "editorial": []}), "duplicate": True}
    results, editorial = [], []
    inventory = next((a["inventory"] for a in acts if a["type"] == "inspect_supplier_photo"), None)
    if inventory is not None:
        from orchestrator_bot.existing_cards import pricing
        supplier_pricing = pricing(store.get_message(msg_id), acts)
    for action in acts:
        try:
            outcomes = intents.plan([action], factory.resolve, threshold)
        except Exception as exc:
            log.exception("разбор действия %s", action.get("type"))
            results.append(Result(Outcome("error", action, ""), _failed(action.get("category") or action["type"], exc)))
            continue
        for outcome in outcomes:
            try:
                options = {"supplier_inventory": inventory, 'supplier_pricing': supplier_pricing} if inventory is not None else {}
                results.append(actions.execute(outcome, factory, store, msg_id, **options))
                if outcome.kind in EDITORIAL:
                    editorial.append(outcome.action)
            except Exception as exc:
                log.exception("исполнение %s", outcome.kind)
                results.append(Result(outcome, _failed(outcome.text or outcome.kind, exc)))
    if any(r.log_id is not None for r in results):        # что-то поставлено — не будет ли ждать?
        warning = workers.note(factory)
        if warning:
            results.append(warning)
    text, keyboard = intents.render(reply, results)
    notices = [notice for result in results for notice in result.notices]
    try:
        for notice in notices:
            for part in text_chunks(notice):
                send(part, [])
        parts = list(text_chunks(text or "Принял."))
        for n, part in enumerate(parts):
            send(part, keyboard if n == len(parts) - 1 else [])
    except Exception:
        log.exception("ответ владельцу не отправлен")
    response = {"ok": True, "editorial": editorial, "reply": text or "Принял."}
    if notices:
        response['notices'] = notices
    store.save_response(msg_id, response)
    store.mark_done(msg_id)
    return response


def _failed(what, exc) -> str:
    return f"❌ Не удалось: {what} — {exc.__class__.__name__}: {str(exc)[:200]}"


def make_server(store, factory, send, token, port, threshold=40, clock=time.time,
                on_draft=None, on_transcribe=None) -> ThreadingHTTPServer:
    intent_lock = threading.RLock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # без журнала запросов в stderr
            pass

        def _json(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if self.headers.get("X-Orch-Token") != token:
                self._json(401, {"error": "unauthorized"})
                return False
            return True

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if not self._authorized():
                return
            if self.path == '/health':
                heartbeat = store.last_heartbeat()
                return self._json(200, {'ok': True, 'queued': store.count_new(),
                    'heartbeat_age': max(0, clock() - heartbeat) if heartbeat else None,
                    'capabilities': CAPABILITIES})
            if self.path != "/next":
                return self._json(404, {"error": "not found"})
            now = clock()
            message = store.claim_next(now)
            if message:
                from orchestrator_bot.supplier_import import source_message
                message = source_message(store, message["id"])
            hint, agent = {}, ""
            if message:                                 # каталог и агент нужны только для настоящего сообщения
                try:
                    hint = factory.hint()
                except Exception:
                    log.exception("подсказка каталога")
                try:
                    agent = workers.readiness(factory.workers())[1]
                except Exception:
                    agent = "состояние контент-агента неизвестно"
            self._json(200, {"message": message, "context": {"hint": hint, "workers": agent,
                                                              "capabilities": CAPABILITIES,
                                                              "history": store.dialogue_before(message["id"]) if message else [],
                                                              "today": store.actions_since(start_of_day(now))}})

        def do_POST(self):
            try:
                body = self._body()                # дочитать тело до ответа, иначе 401 рвёт соединение (Windows)
            except ValueError as exc:
                body, bad = None, exc
            else:
                bad = None
            if not self._authorized():
                return
            if bad is not None:
                return self._json(400, {"error": str(bad)})
            try:
                if self.path == '/photo-feedback-transcribe' and on_transcribe is not None:
                    audio, hint = body.get('audio'), body.get('hint', '')
                    if (not isinstance(audio, str) or not 0 < len(audio) <= 14 * 1024 * 1024 or
                            not isinstance(hint, str) or len(hint) > 1000):
                        raise ValueError('Некорректное голосовое замечание')
                    try:
                        data = base64.b64decode(audio, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise ValueError('Повреждено голосовое замечание') from exc
                    if not 0 < len(data) <= 10 * 1024 * 1024:
                        raise ValueError('Голосовое замечание слишком большое')
                    return self._json(200, {'text': on_transcribe(data, hint)})
                if self.path == '/photo-reviews':
                    from orchestrator_bot.photo_reviews import register, _identity
                    _identity(body)
                    item_key = factory.photo_review_key(body['job_id']) if hasattr(factory, 'photo_review_key') else None
                    return self._json(200, register(store, body, item_key=item_key))
                if self.path == '/photo-feedback':
                    from orchestrator_bot.photo_reviews import correct
                    with intent_lock:
                        return self._json(200, correct(store, factory, body))
                if self.path == '/photo-audit-publish':
                    from orchestrator_bot.photo_recovery import validate
                    return self._json(200,factory.publish_audited(validate(body)))
                if self.path == '/photo-audit-result':
                    from orchestrator_bot.photo_recovery import validate
                    result = factory.reconcile_photo(validate(body, allow_review=True))
                    if result.get('resumed') and result.get('item_key'):
                        store.reopen_recovered_batches(result['item_key'], now=clock(),
                            recovery_id=body['audit_id'] + ':' + str(body['attempt']))
                    return self._json(200, result)
                if self.path == "/heartbeat":
                    store.heartbeat(clock())
                    return self._json(200, {"ok": True})
                if self.path == "/reply":
                    send(body["text"], body.get("keyboard"))
                    return self._json(200, {"ok": True})
                if self.path == "/draft" and on_draft is not None:   # этап 3: принятая версия → картинка → ✅
                    return self._json(200, on_draft(body["draft"]))
                if self.path == "/intent":
                    with intent_lock:
                        result = handle_intent(store, factory, send, body["message_id"], body["intent"], threshold)
                    return self._json(200, result)
            except (KeyError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                log.exception("API %s", self.path)
                return self._json(500, {"error": exc.__class__.__name__})
            self._json(404, {"error": "not found"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
