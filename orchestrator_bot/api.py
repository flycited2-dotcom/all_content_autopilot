"""Локальный HTTP API для слушателя на ПК (только 127.0.0.1, доступ через SSH-туннель)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestrator_bot import actions, intents, workers
from orchestrator_bot.intents import Outcome, Result

log = logging.getLogger("orchestrator_bot")
EDITORIAL = ("run_editorial", "revise_editorial")


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
        store.mark_done(msg_id)                      # повтор после потерянного ответа — не зацикливать
        return {**(store.get_response(msg_id) or {"ok": True, "editorial": []}), "duplicate": True}
    results, editorial = [], []
    for action in acts:
        try:
            outcomes = intents.plan([action], factory.resolve, threshold)
        except Exception as exc:
            log.exception("разбор действия %s", action.get("type"))
            results.append(Result(Outcome("error", action, ""), _failed(action.get("category") or action["type"], exc)))
            continue
        for outcome in outcomes:
            try:
                results.append(actions.execute(outcome, factory, store, msg_id))
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
    try:
        parts = list(text_chunks(text or "Принял."))
        for n, part in enumerate(parts):
            send(part, keyboard if n == len(parts) - 1 else [])
    except Exception:
        log.exception("ответ владельцу не отправлен")
    response = {"ok": True, "editorial": editorial, "reply": text or "Принял."}
    store.save_response(msg_id, response)
    store.mark_done(msg_id)
    return response


def _failed(what, exc) -> str:
    return f"❌ Не удалось: {what} — {exc.__class__.__name__}: {str(exc)[:200]}"


def make_server(store, factory, send, token, port, threshold=40, clock=time.time,
                on_draft=None) -> ThreadingHTTPServer:
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
                    'capabilities': ['list_products', 'product_selection', 'full_catalog']})
            if self.path != "/next":
                return self._json(404, {"error": "not found"})
            now = clock()
            message = store.claim_next(now)
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
                                                              "capabilities": ["list_products", "product_selection", "full_catalog"],
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
                if self.path == "/heartbeat":
                    store.heartbeat(clock())
                    return self._json(200, {"ok": True})
                if self.path == "/reply":
                    send(body["text"], body.get("keyboard"))
                    return self._json(200, {"ok": True})
                if self.path == "/draft" and on_draft is not None:   # этап 3: принятая версия → картинка → ✅
                    return self._json(200, on_draft(body["draft"]))
                if self.path == "/intent":
                    return self._json(200, handle_intent(store, factory, send, body["message_id"],
                                                         body["intent"], threshold))
            except (KeyError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                log.exception("API %s", self.path)
                return self._json(500, {"error": exc.__class__.__name__})
            self._json(404, {"error": "not found"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
