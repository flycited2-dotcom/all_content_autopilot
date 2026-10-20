"""Одна сессия Claude на день; новый день начинается в 04:00 по местному времени ПК."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DAY_STARTS_AT = 4


def _day(now: datetime) -> str:
    return (now - timedelta(hours=DAY_STARTS_AT)).date().isoformat()


def day_session(now: datetime, state_path: Path) -> tuple[str, bool]:
    state_path = Path(state_path)
    day = _day(now)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("day") == day and state.get("session"):
        return state["session"], False
    session = str(uuid.uuid4())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"day": day, "session": session}), encoding="utf-8")
    return session, True
