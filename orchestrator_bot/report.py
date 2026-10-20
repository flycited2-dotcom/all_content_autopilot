"""Вечерняя сводка (21:00, без нейросети): что поставлено, что с конвейером, был ли ПК на связи."""
from __future__ import annotations

import time

PC_FRESH = 600          # heartbeat свежее 10 минут — ПК на связи


def build_report(status: dict, actions: list[dict], last_heartbeat: float | None, now: float) -> str:
    lines = ["📋 Итог дня"]
    enqueued = [a for a in actions if a["kind"] == "enqueue"]
    cancelled = sum(int(a["result"].get("cancelled") or 0) for a in actions if a["kind"] == "cancel")
    editorial = [a for a in actions if a["kind"] in ("run_editorial", "revise_editorial")]
    if not enqueued and not editorial:
        lines.append("Сегодня задач не было.")
    if enqueued:
        total = sum(len(a["result"].get("accepted") or []) for a in enqueued) - cancelled
        lines.append(f"Поставлено задач: {total}")
        lines += [f"— {a['payload'].get('category', '?')} — {len(a['result'].get('accepted') or [])}" for a in enqueued]
    if cancelled:
        lines.append(f"Отменено: {cancelled}")
    by_status = status.get("by_status") or {}
    if by_status:
        lines.append("Конвейер: " + ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())))
    if "published_since" in status:
        lines.append(f"Опубликовано: {status['published_since']}")
    for a in editorial:
        what = a["payload"].get("brief") or f"правка {a['payload'].get('draft_id', '')}"
        lines.append(f"📝 Редакционное: {what}")
    if last_heartbeat is not None and now - last_heartbeat <= PC_FRESH:
        lines.append("ПК: на связи")
    elif last_heartbeat is not None:
        lines.append(time.strftime("ПК: не был на связи с %H:%M %d.%m", time.localtime(last_heartbeat)))
    else:
        lines.append("ПК: ещё ни разу не выходил на связь")
    return "\n".join(lines)
