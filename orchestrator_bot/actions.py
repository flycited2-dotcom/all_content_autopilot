"""Исполнение исходов (общий код для API слушателя и кнопок Telegram)."""
from __future__ import annotations

from orchestrator_bot import drafts, feed
from orchestrator_bot.intents import Outcome, Result, due_text


def _names(items, limit=10) -> str:
    names = [i.get("name") or i.get("key", "") for i in items[:limit]]
    more = f"\n…и ещё {len(items) - limit}" if len(items) > limit else ""
    return "".join(f"\n— {n}" for n in names) + more


def execute(outcome: Outcome, factory, store, msg_id, *, source_suffix="", supplier_inventory=None, supplier_pricing=None) -> Result:
    kind = outcome.kind
    if kind == 'inspect_supplier_photo':
        from orchestrator_bot.supplier_inventory import normalize_inventory, render
        inventory = normalize_inventory(outcome.action['inventory'])
        statuses = factory.supplier_photo_status(inventory).get('statuses', [])
        from orchestrator_bot.existing_cards import notice, pricing
        report = notice(inventory, statuses, supplier_pricing or pricing(store.get_message(msg_id)))
        return Result(outcome, render(inventory, statuses), notices=[report] if report else [])
    if kind == "enqueue_supplier_products":
        import hashlib
        import json
        from orchestrator_bot.supplier_import import normalize_action, source_message, validate_source
        action = normalize_action(outcome.action)
        if supplier_inventory is not None:
            from orchestrator_bot.supplier_inventory import validate_selection
            # Validate again at the durable executor, including after a restart.
            if not supplier_inventory.get('complete'):
                raise ValueError('Разбор фото не прошёл сверку: генерация остановлена')
            validate_selection(action, supplier_inventory)
        message = source_message(store, msg_id)
        images = validate_source(action, message)
        digest = hashlib.sha256(json.dumps(action, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        options = {'new_only': True} if supplier_inventory is not None else {}
        dispatch_action = action
        if not images:
            dispatch_action = {**action, 'items': [{k: v for k, v in item.items() if k != 'attachment_index'}
                                                  for item in action['items']]}
        if message.get('document'):
            from orchestrator_bot.document_transport import original_bundle
            options['document'] = original_bundle(store.path.parent, message['document'])
        result = factory.enqueue_supplier(dispatch_action, f"telegram:{msg_id}:{digest}{source_suffix}", images, **options)
        accepted = result.get("accepted") or []
        if not accepted:
            return Result(outcome, 'Новая генерация не запущена. Статус каждой позиции указан в разборе фото.'
                          if supplier_inventory is not None else
                          'Новая генерация не запущена: сервер не принял позиции. Это не подтверждает, что карточки уже готовы.')
        # Use the same batch/cancel/publication controls as catalog products.
        existing = store.find_supplier_selection(msg_id, action, result.get("manifest_path"))
        log_id = existing["id"] if existing else store.log_action(msg_id, "enqueue", action, result)
        store.add_batch(log_id, action["category"], accepted, feed.publish_at(action))
        lines = [f"✅ Из прайса поставлено в генерацию: {len(accepted)} поз.; наценка {action['markup_percent']}%."]
        for row in result.get("items") or []:
            lines.append(f"— {row['brand']} {row['model']}: {row['cost_price']} ₽ + {action['markup_percent']}% → {row['price']} ₽; остаток {row['stock']}")
        if action.get("at"):
            lines.append(f"Выход пачкой: {due_text(action)}.")
        return Result(outcome, "\n".join(lines), log_id=log_id)
    if kind == 'choose_products':
        from orchestrator_bot.selection_ui import open_selection
        text, buttons = open_selection(store, factory, msg_id, outcome.action)
        return Result(outcome, text, selection_rows=buttons)
    if kind == "list_products":
        found = factory.resolve(outcome.action)
        items = found.get("items") or []
        category = outcome.action["category"]
        if not items:
            return Result(outcome, f"Нет доступных для генерации позиций: «{category}».")
        lines = [f"Доступны для генерации: «{category}» — {len(items)} поз."]
        for n, item in enumerate(items, 1):
            name = item.get("name") or item.get("key") or "Название не передано источником"
            lines.append(f"{n}. {name}")
        total = int(found.get("total_matches") or 0)
        if total > len(items):
            lines.append(f"Показано {len(items)} из {total} совпадений по заданному отбору.")
        lines.append("В генерацию ничего не добавлено.")
        return Result(outcome, "\n".join(lines))
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
    kind = "enqueue_supplier_products" if action.get("type") == "enqueue_supplier_products" else "enqueue"
    if kind == "enqueue_supplier_products":
        import time
        return execute(Outcome(kind, action, action.get("category", "")), factory, store, entry["msg_id"],
                       source_suffix=f":restore:{time.time_ns()}")
    return execute(Outcome(kind, action, action.get("category", "")), factory, store, entry["msg_id"])


_NOT_CANCELLED = {
    "preview": "уже на превью в канале проверки: отменять поздно, отклоните кнопкой ❌ там",
    "rejected": "уже отклонены вами в канале проверки — в работе их нет",
    "published": "уже опубликованы",
    "cancelled": "уже были отменены",
    "failed": "уже упали с ошибкой — в работе их нет",
    "held": "отложены конвейером",
    "missing": "не найдены в конвейере",
}
