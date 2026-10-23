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


class SetupError(ValueError):
    """Only application-authored messages; never include a token or HTTP URL."""


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
        from tkinter import messagebox
        from tkinter.simpledialog import askstring
        window = tk.Tk()
        window.withdraw()
        try:
            while True:
                token = askstring("Фотоаудитор", f"Токен @{BOT_USERNAME} из BotFather (скрытый ввод):", show="*", parent=window)
                if token is None:
                    return {"connected": False, "cancelled": True}
                try:
                    return save_token(root, token)
                except SetupError as error:
                    messagebox.showerror("Фотоаудитор: подключение", str(error), parent=window)
        finally:
            window.destroy()
    return save_token(root, getpass.getpass(f"Токен @{BOT_USERNAME} (скрытый ввод): "))


def save_token(root, token):
    token = (token or "").strip()
    if not re.fullmatch(r"\d{6,16}:[A-Za-z0-9_-]{25,80}", token):
        raise SetupError("Неверный формат. Вставьте только сам токен из BotFather: цифры, двоеточие и ключ, без поясняющего текста.")
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/getMe")
            data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise SetupError("Telegram не принял токен. Скопируйте актуальный токен @foto_auditor_bot из BotFather.")
        result = data.get("result") or {}
        if not isinstance(result, dict) or result.get("username", "").casefold() != BOT_USERNAME:
            raise SetupError("Это токен другого бота. Нужен токен именно @foto_auditor_bot.")
    except (httpx.HTTPError, json.JSONDecodeError):
        raise SetupError("Не удалось связаться с Telegram. Проверьте интернет и повторите ввод; токен не сохранён.") from None
    path = credentials_path(root)
    api_token = load(root)["api_token"] if path.exists() else secrets.token_urlsafe(32)
    blob = json.dumps({"bot_token": token, "api_token": api_token}).encode()
    if sys.platform == "win32":
        blob = b"dpapi:v1\n" + _dpapi(blob)
    _atomic_write(path, blob)
    return {"connected": True, "username": BOT_USERNAME}
