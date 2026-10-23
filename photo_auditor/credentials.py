"""Masked token entry, Telegram identity validation and user-bound storage."""
import getpass
import json
import re
import secrets
import sys
import unicodedata
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
        from photo_auditor.setup_ui import run_setup
        return run_setup(root)
    return save_token(root, getpass.getpass(f"Токен @{BOT_USERNAME} (скрытый ввод): "))


def normalize_token(text):
    """Extract one credential from a BotFather copy, without echoing any input."""
    if not isinstance(text, str) or not text.strip():
        raise SetupError("Поле пустое. Скопируйте токен в BotFather и нажмите «Вставить из буфера».")
    if len(text) > 32000:
        raise SetupError("Скопировано слишком много текста. Скопируйте сообщение BotFather с токеном.")
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    matches = re.findall(r"(?<![A-Za-z0-9_-])([0-9]{5,20})\s*:\s*([A-Za-z0-9_-]{15,256})(?![A-Za-z0-9_-])", text)
    tokens = {bot_id + ":" + key for bot_id, key in matches}
    if len(tokens) > 1:
        raise SetupError("Скопированы токены нескольких ботов. Скопируйте только сообщение для @foto_auditor_bot.")
    if not tokens:
        raise SetupError("Токен в тексте не найден. В BotFather нажмите кнопку копирования токена, затем «Вставить из буфера» здесь.")
    return tokens.pop()


def verify_token(token):
    """getMe is the authority for token validity and bot identity."""
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


def store_verified_token(root, token):
    path = credentials_path(root)
    api_token = load(root)["api_token"] if path.exists() else secrets.token_urlsafe(32)
    blob = json.dumps({"bot_token": token, "api_token": api_token}).encode()
    if sys.platform == "win32":
        blob = b"dpapi:v1\n" + _dpapi(blob)
    _atomic_write(path, blob)
    return {"connected": True, "username": BOT_USERNAME}


def save_token(root, text):
    token = normalize_token(text)
    verify_token(token)
    return store_verified_token(root, token)
