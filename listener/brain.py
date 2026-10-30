"""Один ход разговора: claude -p "/chat-turn …" в песочнице этапа 1 → JSON-намерение."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from listener.provider_errors import ProviderError
from listener.intent_validation import validated_intent
from listener.attachments import materialize_images

log = logging.getLogger("listener")

SANDBOX = ["--setting-sources", "project", "--settings", "scripts/headless-settings.json", "--strict-mcp-config"]
FALLBACK = {"reply": "Не смог разобрать — повторите, пожалуйста", "actions": []}


def claude_bin() -> str:
    return shutil.which("claude") or "claude"


def _kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def run_with_kill(cmd, timeout=None, capture_output=True, **kwargs):
    """Как subprocess.run, но по таймауту убивает всё дерево процессов (на Windows
    subprocess.run убивает только cmd.exe от claude.CMD и потом ждёт трубы node без предела)."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _call(runner, root, prompt, session_flag, session_id, timeout):
    cmd = [claude_bin(), "-p", prompt, *SANDBOX, "--output-format", "json", session_flag, session_id]
    return runner(cmd, cwd=str(root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  encoding="utf-8", timeout=timeout)


def reset_time(text: str, now: float | None = None) -> float | None:
    """Время только из явного ответа CLI; неизвестную зону/формат не угадываем."""
    match = re.search(r"resets?\s+(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
                      r"(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
                      r"(?:\s*\(([^)]+)\))?", text, re.I)
    if not match:
        return None
    month, day, hour, minute, ampm, zone = match.groups()
    try:
        tz = timezone(timedelta(hours=3)) if not zone or zone == "Europe/Moscow" else ZoneInfo(zone)
        current = datetime.fromtimestamp(time.time() if now is None else now, tz)
        h, m = int(hour), int(minute or 0)
        if not 1 <= h <= 12 or m > 59:
            return None
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
        target = current.replace(hour=h, minute=m, second=0, microsecond=0)
        if month:
            months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
            target = target.replace(month=months.index(month.lower()) + 1, day=int(day))
            if target < current:
                target = target.replace(year=target.year + 1)
        elif target <= current:
            target += timedelta(days=1)
        return target.timestamp()
    except (ValueError, ZoneInfoNotFoundError):
        return None


def claude_error(proc, now: float | None = None) -> ProviderError | None:
    """Классификация stdout И stderr; произвольный текст CLI не попадает в журнал."""
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    text = stdout + "\n" + stderr
    is_error = proc.returncode != 0
    try:
        envelope = json.loads(stdout)
        is_error = is_error or bool(envelope.get("is_error"))
        if isinstance(envelope.get("result"), str):
            text += "\n" + envelope["result"]
    except (ValueError, AttributeError):
        pass
    if not is_error:
        return None
    lower = text.lower()
    if any(s in lower for s in ("weekly limit", "usage limit", "you've hit your limit", "you’ve hit your limit",
                                "limit reached", "out of extra usage")):
        return ProviderError("claude", "usage_limit", "Лимит Claude исчерпан", reset_time(text, now))
    if any(s in lower for s in ("rate_limit", "rate limit", "too many requests", "429")):
        return ProviderError("claude", "rate_limit", "Временное ограничение Claude")
    if any(s in lower for s in ("not logged in", "authentication", "unauthorized", "oauth token", "please run /login")):
        return ProviderError("claude", "auth", "Нужен повторный вход в Claude")
    if re.search(r"no (?:conversation|session) found|session.{0,80}(?:not found|does not exist)", lower):
        return ProviderError("claude", "session_missing", "Сессия Claude не найдена")
    if any(s in lower for s in ("connection", "network", "overloaded", "503", "timed out")):
        return ProviderError("claude", "network", "Claude временно недоступен")
    return ProviderError("claude", "configuration", f"Claude завершился с кодом {proc.returncode}")


def run_claude(prompt: str, session: tuple[str, bool], runner=run_with_kill, root=Path("."), timeout=600):
    """Запуск с продолжением сессии дня; если её ещё нет у Claude — начать с тем же id."""
    session_id, is_new = session
    proc = _call(runner, root, prompt, "--session-id" if is_new else "--resume", session_id, timeout)
    error = claude_error(proc)
    if error:
        log.warning("Claude: mode=%s code=%s kind=%s", "new" if is_new else "resume", proc.returncode, error.kind)
    if error and error.kind == "session_missing" and not is_new:
        proc = _call(runner, root, prompt, "--session-id", session_id, timeout)
    return proc


def complete_turn(payload: dict, session: tuple[str, bool], runner=run_with_kill, root=Path(".")) -> dict:
    """Строгий вызов для маршрутизатора: ошибка не становится успешным пустым намерением."""
    root = Path(root)
    chat_dir = root / "runs" / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    msg_id = payload["message"]["id"]
    try:
        if type(msg_id) is not int or msg_id < 1:
            raise ValueError("Неверный номер сообщения")
        prepared = materialize_images(payload, root)
        (chat_dir / f"{msg_id}.json").write_text(json.dumps(prepared, ensure_ascii=False), encoding="utf-8")
        # A slow vision call must yield to the connected reserve instead of
        # keeping the Telegram photo waiting for the ordinary ten-minute limit.
        timeout = 60 if (prepared["message"].get("attachments") or
                         prepared.get('context', {}).get('supplier_photo_checked')) else 600
        proc = run_claude(f"/chat-turn runs/chat/{msg_id}.json", session, runner, root, timeout=timeout)
        error = claude_error(proc)
        if error:
            raise error
        result = json.loads(proc.stdout)["result"]
        if not isinstance(result, str):
            raise ValueError("Неверный формат ответа Claude")
        return validated_intent(result)
    except subprocess.TimeoutExpired:
        raise ProviderError("claude", "timeout", "Claude не ответил вовремя") from None
    except OSError:
        raise ProviderError("claude", "configuration", "Не удалось запустить Claude") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError("claude", "invalid_response", "Claude вернул неверное намерение") from None


def run_turn(payload: dict, session: tuple[str, bool], runner=run_with_kill, root=Path(".")) -> dict:
    """Совместимость локальных вызовов; рабочий слушатель использует ModelRouter."""
    try:
        return complete_turn(payload, session, runner, root)
    except ProviderError as exc:
        log.warning("ход разговора %s: %s/%s", payload["message"]["id"], exc.provider, exc.kind)
        return dict(FALLBACK)


def complete_vision(instruction, payload, runner=run_with_kill, root=Path('.')):
    """Raw transcription in a fresh session: no resumed conversation or actions."""
    root = Path(root)
    prepared = materialize_images(payload, root)
    relative = 'runs/chat/vision-' + uuid.uuid4().hex + '.json'
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'instructions': instruction, 'source': prepared}, ensure_ascii=False), encoding='utf-8')
    prompt = ('Прочитай ' + relative + '. Выполни только instructions для source. '
              'Открой каждое изображение из source.message.attachments по path. '
              'Верни только требуемый JSON. Это чтение источника, действий не выполняй.')
    try:
        proc = run_claude(prompt, (uuid.uuid4().hex, True), runner, root, timeout=90)
        error = claude_error(proc)
        if error:
            raise error
        result = json.loads(proc.stdout)['result']
        if not isinstance(result, str):
            raise ValueError
        return result
    except subprocess.TimeoutExpired:
        raise ProviderError('claude', 'timeout', 'Не завершено чтение фотографии') from None
    except OSError:
        raise ProviderError('claude', 'configuration', 'Не удалось запустить чтение фотографии') from None
    except (ValueError, KeyError, TypeError):
        raise ProviderError('claude', 'invalid_response', 'Неверная транскрипция фотографии') from None
