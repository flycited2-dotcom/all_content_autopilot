"""Состояние контент-агента (генерация карточек): готов ли он брать поставленные задачи."""
from __future__ import annotations

import logging
from datetime import datetime

from orchestrator_bot.intents import Outcome, Result

log = logging.getLogger("orchestrator_bot")
START_BUTTON = {"text": "▶️ Запустить", "callback_data": "w:start"}


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M %d.%m")
    except (TypeError, ValueError):
        return str(iso)


def readiness(w: dict, now: datetime | None = None) -> tuple[bool, str]:
    reasons = []
    if not w.get("generation"):
        reasons.append("генерация выключена")
    if not w.get("agent_online"):
        if w.get("last_seen"):
            reasons.append(f"фото-агент не на связи с {_when(w['last_seen'])}")
        else:
            reasons.append("фото-агент ни разу не выходил на связь")
        if "stop" in (w.get("commands") or {}).values():
            reasons.append("остановлен вручную")
    if not reasons:
        return True, "🟢 Контент-агент работает"
    return False, "⚠️ Контент-агент: " + ", ".join(reasons)


def note(factory) -> Result | None:
    """Строка-предупреждение с кнопкой «Запустить», если поставленные задачи будут ждать."""
    try:
        ok, text = readiness(factory.workers())
    except Exception:
        log.exception("состояние контент-агента")
        return Result(Outcome("workers", {}, ""), "⚠️ Не удалось проверить контент-агента — задачи в очереди")
    if ok:
        return None
    return Result(Outcome("workers", {}, ""),
                  f"{text}. Задачи ждут. Если компьютер с фото-агентом выключен — включите его.",
                  buttons=[START_BUTTON])


def start(factory) -> str:
    result = factory.start_workers()
    parts = ["▶️ Запуск отправлен: генерация включена"]
    if result.get("commanded"):
        parts.append("сторожам фото-агента — команда «start»")
    if result.get("requested_run"):
        parts.append("конвейер разбужен")
    return ", ".join(parts) + ". Проверьте через минуту: «📋 Статус»."
