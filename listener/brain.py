"""Один ход разговора: claude -p "/chat-turn …" в песочнице этапа 1 → JSON-намерение."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from orchestrator_bot.intents import IntentError, parse_intent

log = logging.getLogger("listener")

SANDBOX = ["--setting-sources", "project", "--settings", "scripts/headless-settings.json", "--strict-mcp-config"]
FALLBACK = {"reply": "Не смог разобрать — повторите, пожалуйста", "actions": []}


def claude_bin() -> str:
    return shutil.which("claude") or "claude"


def _call(runner, root, prompt, session_flag, session_id, timeout):
    cmd = [claude_bin(), "-p", prompt, *SANDBOX, "--output-format", "json", session_flag, session_id]
    return runner(cmd, cwd=str(root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  encoding="utf-8", timeout=timeout)


def run_claude(prompt: str, session: tuple[str, bool], runner=subprocess.run, root=Path("."), timeout=600):
    """Запуск с продолжением сессии дня; если её ещё нет у Claude — начать с тем же id."""
    session_id, is_new = session
    proc = _call(runner, root, prompt, "--session-id" if is_new else "--resume", session_id, timeout)
    if proc.returncode != 0 and not is_new:
        proc = _call(runner, root, prompt, "--session-id", session_id, timeout)
    return proc


def run_turn(payload: dict, session: tuple[str, bool], runner=subprocess.run, root=Path(".")) -> dict:
    root = Path(root)
    chat_dir = root / "runs" / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    msg_id = payload["message"]["id"]
    (chat_dir / f"{msg_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        proc = run_claude(f"/chat-turn runs/chat/{msg_id}.json", session, runner, root)
        if proc.returncode != 0:
            raise IntentError(f"claude код {proc.returncode}")
        return parse_intent(json.loads(proc.stdout)["result"])
    except (IntentError, ValueError, KeyError, subprocess.TimeoutExpired, OSError):
        log.exception("ход разговора %s не удался", msg_id)
        return dict(FALLBACK)
