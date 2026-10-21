"""Единая лента: что ждёт решения владельца — одной сводкой, одобрение пачкой, публикация из очереди.

Товарные превью живут в канале проверки @Sendpr1ce_bot, ВК-материалы — в ВК-плане, посты оркестратора — здесь.
Сводка сводит их в одно сообщение (утром в 09:30 и по кнопке «🗂 Лента»). «Одобрить свежие товары» ставит их
в очередь: в канал уходят по одному раз в 30 минут с 09:00 до 21:00, тем же кодом, что кнопка ✅.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("orchestrator_bot")

DIGEST_AT = (9, 30)
QUEUE_HOURS = (9, 21)
QUEUE_EVERY = 1800
FEED_BUTTON = {"text": "🗂 Лента", "callback_data": "f:show"}


def digest(factory, store) -> tuple[str, list] | None:
    """None — решать нечего."""
    feed = factory.feed()
    tg, vk = feed["tg"], feed["vk_review"]
    drafts = store.drafts_summary(0)["ready"]
    if not (tg["fresh"] or tg["stale"] or tg["queued"] or vk or drafts or tg["pending"]):
        return None
    lines = ["🗂 Ждут вашего решения"]
    middle = tg["pending"] - tg["fresh"] - tg["stale"]
    tg_parts = [f"свежих {tg['fresh']}"]
    if middle:
        tg_parts.append(f"старше {feed['fresh_hours'] // 24} дн. — {middle}")
    if tg["stale"]:
        tg_parts.append(f"устаревших (старше {feed['stale_days']} дн.) — {tg['stale']}")
    lines.append("🛒 Товары в Telegram: " + ", ".join(tg_parts))
    if tg["queued"]:
        lines.append(f"⏳ В очереди на публикацию: {tg['queued']} (по одному раз в 30 минут, 09:00–21:00)")
    lines.append(f"📣 ВК-план на проверке: {vk}")
    if drafts:
        lines.append(f"📝 Посты оркестратора: {drafts} — кнопки под ними выше")
    keyboard = []
    if tg["fresh"]:
        keyboard.append([{"text": f"✅ Свежие товары в очередь ({tg['fresh']})", "callback_data": "f:tg"}])
    if vk:
        keyboard.append([{"text": f"✅ Одобрить ВК-план ({vk})", "callback_data": "f:vk"}])
    if tg["stale"]:
        keyboard.append([{"text": f"🧹 Снять устаревшие ({tg['stale']})", "callback_data": "f:old"}])
    if feed.get("review_url"):
        keyboard.append([{"text": "👀 Смотреть по одному", "url": feed["review_url"]}])
    return "\n".join(lines), keyboard


def act(factory, op: str) -> str:
    if op == "tg":
        n = factory.approve_fresh()["queued"]
        return f"✅ В очереди: {n}. Уходят в канал по одному раз в 30 минут с 09:00 до 21:00."
    if op == "vk":
        n = factory.vk_approve()["approved"]
        return f"✅ Одобрено в ВК-плане: {n}. Выйдут по своим слотам."
    if op == "old":
        n = factory.expire_stale()["expired"]
        return f"🧹 Снято с ожидания устаревших превью: {n}. Ничего не удалено — товары можно поставить заново со свежей ценой."
    return "Неизвестное действие ленты"


def tick(deps, now: float) -> None:
    """Из цикла бота: утренняя сводка и очередь публикации."""
    moment = datetime.fromtimestamp(now)
    day = moment.date().isoformat()
    if (moment.hour, moment.minute) >= DIGEST_AT and deps.store.get_meta("digest_day") != day:
        deps.store.set_meta("digest_day", day)
        try:
            found = digest(deps.factory, deps.store)
            if found:
                deps.tg.send(deps.owner_chat_id, *found)
        except Exception:
            log.exception("утренняя сводка")
    if not QUEUE_HOURS[0] <= moment.hour < QUEUE_HOURS[1]:
        return
    last = deps.store.get_meta("queue_last")
    if last and now - float(last) < QUEUE_EVERY:
        return
    deps.store.set_meta("queue_last", str(now))
    try:
        res = deps.factory.publish_next()
    except Exception:
        log.exception("публикация из очереди")
        return
    if res.get("published") is None and res.get("error"):
        deps.tg.send(deps.owner_chat_id, f"❌ Из очереди не опубликовано: {res['error']}. Пост вернулся в канал проверки.")
