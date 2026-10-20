"""Клиент локального API сервиса оркестратора (через SSH-туннель на 127.0.0.1:8790)."""
from __future__ import annotations

import httpx


class ApiClient:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=60)
        self.headers = {"X-Orch-Token": token}

    def _post(self, path, body):
        resp = self.http.post(self.base + path, json=body, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def next(self) -> dict:
        resp = self.http.get(self.base + "/next", headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def intent(self, message_id, intent) -> dict:
        return self._post("/intent", {"message_id": message_id, "intent": intent})

    def reply(self, text, keyboard=None) -> dict:
        return self._post("/reply", {"text": text, "keyboard": keyboard})

    def heartbeat(self) -> dict:
        return self._post("/heartbeat", {})
