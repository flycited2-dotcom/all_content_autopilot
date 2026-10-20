"""Тонкий клиент Telegram Bot API (только то, что нужно оркестратору)."""
from __future__ import annotations

import httpx

_MAX_TEXT = 4000            # лимит Telegram 4096, с запасом


class TelegramApi:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self.token = token
        self.http = http or httpx.Client(timeout=60)
        self.base = f"https://api.telegram.org/bot{token}/"

    def _call(self, method: str, **params):
        resp = self.http.post(self.base + method, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', resp.status_code)}")
        return data["result"]

    @staticmethod
    def _markup(keyboard):
        return {"inline_keyboard": keyboard} if keyboard else None

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", **params)

    def send(self, chat_id, text, keyboard=None, reply_keyboard=None) -> int:
        params = {"chat_id": chat_id, "text": text[:_MAX_TEXT]}
        if keyboard:
            params["reply_markup"] = self._markup(keyboard)
        elif reply_keyboard:                       # постоянные кнопки внизу чата (не уезжают с лентой)
            params["reply_markup"] = {"keyboard": [[{"text": b} for b in row] for row in reply_keyboard],
                                      "resize_keyboard": True, "is_persistent": True}
        return self._call("sendMessage", **params)["message_id"]

    def set_commands(self, commands: list[tuple[str, str]]):
        self._call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    def edit(self, chat_id, message_id, text, keyboard=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text[:_MAX_TEXT]}
        if keyboard:
            params["reply_markup"] = self._markup(keyboard)
        self._call("editMessageText", **params)

    def download_voice(self, file_id) -> bytes:
        path = self._call("getFile", file_id=file_id)["file_path"]
        resp = self.http.get(f"https://api.telegram.org/file/bot{self.token}/{path}")
        resp.raise_for_status()
        return resp.content

    def answer_callback(self, callback_id, text=""):
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
