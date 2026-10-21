"""Утренний прогон без команды владельца: раз в день с 08:00 — редакционные посты для TG и ВК.

ПК включили позже — прогон выполняется при первом цикле после 08:00, но не позже 20:00
(вечером пост уже не к месту). Отметка дня — в runs/chat/morning.json; {"off": true} — выключено.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("listener")

START_HOUR, END_HOUR = 8, 20
AUTO_BRIEF = ("Утренний прогон без брифа владельца. Выбери сам одну тему для Telegram-канала и одну для группы ВК: "
              "по сезону (текущий месяц, погода в Крыму) и частым вопросам покупателей климатической техники — "
              "выбор, монтаж, уход, экономия, отопление и охлаждение. Темы не повторяют посты последних 14 дней "
              "(runs/*/drafts). Товарные задачи не ставь.")
ACTION = {"type": "run_editorial", "brief": AUTO_BRIEF, "channels": ["tg_editorial", "vk_editorial"]}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def maybe_run(now: datetime, state_path: Path, worker, post) -> bool:
    """True — прогон поставлен сейчас. День отмечается до постановки: сбой отправки не повторит прогон."""
    if not START_HOUR <= now.hour < END_HOUR:
        return False
    state = _load(state_path)
    day = now.date().isoformat()
    if state.get("off") or state.get("day") == day:
        return False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({**state, "day": day}, ensure_ascii=False), encoding="utf-8")
    worker.submit(dict(ACTION))
    log.info("утренний прогон %s поставлен", day)
    try:
        post("🌅 Утренний прогон: пишу посты на сегодня для Telegram и ВК — пришлю готовыми, с картинкой и кнопкой ✅")
    except Exception:
        log.exception("уведомление об утреннем прогоне не отправлено")
    return True
