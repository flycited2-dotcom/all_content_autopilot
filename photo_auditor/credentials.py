"""Masked token entry, Telegram identity validation and user-bound storage."""
import getpass
import json
import re
import secrets
import sys
from pathlib import Path

import httpx

from listener.chatgpt_auth import _atomic_write, _dpapi

BOT_USERNAME = "foto_auditor_bot"


def credentials_path(root):
    return Path(root) / "runs/private/photo-auditor.dat"


def load(root):
    path = credentials_path(root)
    data = path.read_bytes()
    if sys.platform == "win32":
        if not data.startswith(b"dpapi:v1\n"):
            raise ValueError("Неверный формат защищённого подключения")
        data = _dpapi(data[9:], decrypt=True)
    return json.loads(data)


def configure(root, console=False):
    if sys.platform == "win32" and not console:
        import tkinter as tk
        from tkinter.simpledialog import askstring
        window = tk.Tk()
        window.withdraw()
        token = askstring("Фотоаудитор", f"Токен @{BOT_USERNAME} из BotFather (скрытый ввод):", show="*", parent=window)
        window.destroy()
    else:
        token = getpass.getpass(f"Токен @{BOT_USERNAME} (скрытый ввод): ")
    token = (token or "").strip()
    if not re.fullmatch(r"\d{6,16}:[A-Za-z0-9_-]{25,80}", token):
        raise ValueError("Ввод отменён или формат токена неверен")
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/getMe")
            data = response.json()
        if not data.get("ok") or data.get("result", {}).get("username", "").casefold() != BOT_USERNAME:
            raise ValueError("Токен не принадлежит @foto_auditor_bot")
    except (httpx.HTTPError, json.JSONDecodeError):
        raise ValueError("Не удалось проверить подключение к Telegram") from None
    path = credentials_path(root)
    api_token = load(root)["api_token"] if path.exists() else secrets.token_urlsafe(32)
    blob = json.dumps({"bot_token": token, "api_token": api_token}).encode()
    if sys.platform == "win32":
        blob = b"dpapi:v1\n" + _dpapi(blob)
    _atomic_write(path, blob)
    return {"connected": True, "username": BOT_USERNAME}
