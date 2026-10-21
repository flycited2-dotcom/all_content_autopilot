"""Исполнение исходов (общий код для API слушателя и кнопок Telegram)."""
from __future__ import annotations

from orchestrator_bot import drafts, feed
from orchestrator_bot.intents import Outcome, Result, due_text


def _names(items, limit=10) -> str:
    names = [i.get("name") or i.get("key", "") for i in items[:limit]]
    more = f"\n…и ещё {len(items) - limit}" if len(items) > limit else ""
    return "".join(f"\n— {n}" for n in names) + more


def execute(outcome: Outcome, factory, store, msg_id) -> Result:
    kind = outcome.kind
    if kind == "enqueue":
        result = factory.enqueue(outcome.action)
        accepted = result.get("accepted") or []
        if not accepted:
            return Result(outcome, f"ℹ️ По «{outcome.text}» новых позиций нет — всё уже в работе")
        log_id = store.log_action(msg_id, "enqueue", outcome.action, result)
        store.add_batch(log_id, outcome.action.get("category", ""), accepted, feed.publish_at(outcome.action))
        when = f" (выход пачкой: {due_text(outcome.action)})" if outcome.action.get("at") else ""
        return Result(outcome, f"✅ Поставил{when}: «{outcome.text}» — {len(accepted)} поз.{_names(result.get('items') or [])}",
                      log_id=log_id)
    if kind == "ask":
        ask_id = store.save_ask(msg_id, outcome.action, outcome.options)
        return Result(outcome, outcome.text, ask_id=ask_id)
    if kind == "cancel":
        return Result(outcome, cancel_logged(outcome.action.get("task_ref"), factory, store, msg_id))
    if kind == "redo_visual":                           # картинку переделывает сервер, текст не трогаем
        store.log_action(msg_id, kind, outcome.action, {})
        return Result(outcome, drafts.redo_visual(store, factory, outcome.action["draft_id"], outcome.action["notes"]))
    if kind in ("run_editorial", "revise_editorial"):
        store.log_action(msg_id, kind, outcome.action, {})
        what = "экспертные посты" if kind == "run_editorial" else f"правку черновика {outcome.action['draft_id']}"
        return Result(outcome, f"📝 Запустил на ПК {what} — пришлю, когда будет готово")
    return Result(outcome, outcome.text)                 # not_found и прочее — только текст


def cancel_logged(log_ref, factory, store, msg_id) -> str:
    try:
        entry = store.get_log(int(log_ref))
    except (TypeError, ValueError):
        entry = None
    if not entry or entry["kind"] != "enqueue":
        return "❌ Не нашёл, что отменять"
    keys = entry["result"].get("accepted") or []
    result = factory.cancel(keys)
    store.log_action(msg_id, "cancel", {"log_id": entry["id"], "keys": keys}, result)
    store.move_batch(entry["id"], ("open", "ready", "approved"), "closed")
    lines = [f"↩️ Отменено: {result.get('cancelled', 0)}"]
    by_status: dict[str, int] = {}
    for item in result.get("not_cancelled") or []:
        by_status[item.get("status", "")] = by_status.get(item.get("status", ""), 0) + 1
    for status, n in by_status.items():
        lines.append(f"{n} — {_NOT_CANCELLED.get(status, f'не отменить (статус {status})')}")
    return "\n".join(lines)


def restore_logged(log_ref, factory, store) -> Result:
    """«Вернуть» после случайной отмены: та же задача ставится заново (отменённые позиции свободны)."""
    try:
        entry = store.get_log(int(log_ref))
    except (TypeError, ValueError):
        entry = None
    if not entry or entry["kind"] != "enqueue":
        return Result(Outcome("not_found", {}, ""), "❌ Не нашёл, что вернуть")
    action = entry["payload"]
    return execute(Outcome("enqueue", action, action.get("category", "")), factory, store, entry["msg_id"])


_NOT_CANCELLED = {
    "preview": "уже на превью в канале проверки: отменять поздно, отклоните кнопкой ❌ там",
    "rejected": "уже отклонены вами в канале проверки — в работе их нет",
    "published": "уже опубликованы",
    "cancelled": "уже были отменены",
    "failed": "уже упали с ошибкой — в работе их нет",
    "held": "отложены конвейером",
    "missing": "не найдены в конвейере",
}
