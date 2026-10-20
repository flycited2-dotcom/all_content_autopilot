"""Локальный HTTP API для слушателя на ПК (только 127.0.0.1, доступ через SSH-туннель)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestrator_bot import actions, intents


def start_of_day(now: float) -> float:
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def handle_intent(store, factory, send, msg_id, raw_intent, threshold=40) -> dict:
    intent = intents.parse_intent(raw_intent) if isinstance(raw_intent, str) else raw_intent
    reply, acts = intents.normalize(intent)          # разбор до отметки: битое намерение не «сжигает» сообщение
    if not store.mark_intent(msg_id):
        return {"ok": True, "duplicate": True}
    outcomes = intents.plan(acts, factory.resolve, threshold)
    results = [actions.execute(o, factory, store, msg_id) for o in outcomes]
    text, keyboard = intents.render(reply, results)
    send(text or "Принял.", keyboard)
    store.mark_done(msg_id)
    return {"ok": True, "editorial": [o.action for o in outcomes if o.kind in ("run_editorial", "revise_editorial")]}


def make_server(store, factory, send, token, port, threshold=40, clock=time.time) -> ThreadingHTTPServer:
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
            if self.path != "/next":
                return self._json(404, {"error": "not found"})
            now = clock()
            message = store.claim_next(now)
            try:
                hint = factory.hint()
            except Exception:
                hint = {}
            self._json(200, {"message": message, "context": {"hint": hint, "today": store.actions_since(start_of_day(now))}})

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
                if self.path == "/intent":
                    return self._json(200, handle_intent(store, factory, send, body["message_id"],
                                                         body["intent"], threshold))
            except (KeyError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(404, {"error": "not found"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
