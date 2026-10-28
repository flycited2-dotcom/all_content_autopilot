"""Единая лента: что ждёт решения владельца — одной сводкой; товары в Telegram — партиями.

Владелец, 2026-10-21: «по одному каждые полчаса — задолбаются клиенты». Одна задача («холодильники к 12:00»,
«медная труба на 14:00») — одна партия: когда все её карточки готовы, бот присылает «📦 Партия готова» с
кнопкой «Опубликовать пачкой»; названное время — время выхода партии. В ВК товары идут по одному сами:
ВК-план берёт опубликованное и выкладывает по своим слотам.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger("orchestrator_bot")

DIGEST_AT = (9, 30)
POLL_EVERY = 120                         # проверка готовности партий
FEED_BUTTON = {"text": "🗂 Лента", "callback_data": "f:show"}
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def publish_at(action: dict, now: datetime | None = None) -> float | None:
    """Время выхода партии из слов владельца («к 12:00», «завтра 9:00»); не названо — None (по готовности)."""
    at = action.get("at")
    if not at:
        return None
    now = now or datetime.now()
    day = action.get("day") or "сегодня"
    if day == "завтра":
        target = now.date() + timedelta(days=1)
    elif (m := _ISO.match(str(day))):
        target = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        target = now.date()
    hour, minute = (int(x) for x in at.split(":"))
    return datetime(target.year, target.month, target.day, hour, minute).timestamp()


def _when(ts: float | None, now: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m в %H:%M") if ts and ts > now else ""


def batch_buttons(batch: dict, now: float) -> list:
    when = _when(batch["publish_at"], now)
    label = f"✅ Опубликовать пачкой {when}" if when else "✅ Опубликовать пачкой сейчас"
    return [[{"text": label, "callback_data": f"b:p:{batch['log_id']}"}],
            [{"text": "❌ Не публиковать", "callback_data": f"b:x:{batch['log_id']}"}]]


def digest(factory, store, now: float) -> tuple[str, list] | None:
    """None — решать нечего."""
    feed = factory.feed()
    tg, vk = feed["tg"], feed["vk_review"]
    ready = store.batches("ready")
    drafts = store.drafts_summary(0)["ready"]
    if not (tg["pending"] or vk or drafts or ready):
        return None
    lines = ["🗂 Ждут вашего решения"]
    keyboard = []
    for batch in ready:
        lines.append(f"📦 Карточки партии «{batch['category']}» ждут решения — кнопка «Опубликовать пачкой» под её сообщением")
    other = tg["fresh"]
    if other:
        lines.append(f"🛒 Свежих товарных превью в группе проверки: {other}")
        keyboard.append([{"text": f"✅ Все свежие — пачкой сейчас ({other})", "callback_data": "f:tg"}])
    if tg["stale"]:
        lines.append(f"🕸 Устаревших превью (старше {feed['stale_days']} дн., цены неактуальны): {tg['stale']}")
        keyboard.append([{"text": f"🧹 Снять устаревшие ({tg['stale']})", "callback_data": "f:old"}])
    lines.append(f"📣 ВК-план на проверке: {vk}")
    if vk:
        keyboard.append([{"text": f"✅ Одобрить ВК-план ({vk})", "callback_data": "f:vk"}])
    if drafts:
        lines.append(f"📝 Посты оркестратора: {drafts} — кнопки под ними выше")
    if feed.get("review_url"):
        keyboard.append([{"text": "👀 Смотреть по одному", "url": feed["review_url"]}])
    elif tg["pending"] or vk:                       # старая группа проверки — ссылок на неё Telegram не даёт
        lines.append("👀 По одному — в группе проверки @Sendpr1ce_bot, там же кнопки ✅/❌.")
    return "\n".join(lines), keyboard


def act(factory, op: str) -> str:
    if op == "tg":
        res = factory.approve_fresh()
        return _published_text("Свежие превью", res)
    if op == "vk":
        n = factory.vk_approve()["approved"]
        return f"✅ Одобрено в ВК-плане: {n}. Выйдут по своим слотам."
    if op == "old":
        n = factory.expire_stale()["expired"]
        return f"🧹 Снято с ожидания устаревших превью: {n}. Ничего не удалено — товары можно поставить заново со свежей ценой."
    return "Неизвестное действие ленты"


def _published_text(what: str, res: dict) -> str:
    text = f"✅ {what}: опубликовано в канале {len(res.get('published') or [])}"
    failed = res.get("failed") or []
    if failed:
        text += f"\n❌ Не вышло: {len(failed)} ({failed[0].get('error')}) — остались в группе проверки"
    return text


def batch_action(deps, chat_id, op: str, log_id: int) -> None:
    batch = deps.store.get_batch(log_id)
    if batch is None:
        deps.tg.send(chat_id, "Партия не найдена")
        return
    now = deps.clock()
    if op == "x":
        if deps.store.move_batch(log_id, ("open", "ready", "approved"), "closed"):
            deps.tg.send(chat_id, f"Партию «{batch['category']}» не публикую. Превью остались в группе проверки.")
        return
    if batch["publish_at"] and batch["publish_at"] > now:
        if deps.store.move_batch(log_id, ("ready",), "approved"):
            deps.tg.send(chat_id, f"✅ Партия «{batch['category']}» выйдет {_when(batch['publish_at'], now)}.")
        return
    if deps.store.move_batch(log_id, ("ready", "approved"), "publishing"):
        _publish(deps, chat_id, batch)
    else:
        deps.tg.send(chat_id, f"Партия «{batch['category']}» уже обработана")


def _publish(deps, chat_id, batch) -> None:
    try:
        res = deps.factory.publish_keys(batch["keys"])
    except Exception as exc:
        log.exception("публикация партии %s", batch["log_id"])
        deps.store.move_batch(batch["log_id"], ("publishing",), "ready")
        deps.tg.send(chat_id, f"❌ Партия «{batch['category']}» не опубликована: {exc.__class__.__name__}. Можно нажать ещё раз.")
        return
    deps.store.move_batch(batch["log_id"], ("publishing",), "published")
    deps.tg.send(chat_id, _published_text(f"Партия «{batch['category']}»", res))


def tick(deps, now: float) -> None:
    """Из цикла бота: утренняя сводка, готовность партий, выход одобренных партий в своё время."""
    moment = datetime.fromtimestamp(now)
    day = moment.date().isoformat()
    if (moment.hour, moment.minute) >= DIGEST_AT and deps.store.get_meta("digest_day") != day:
        deps.store.set_meta("digest_day", day)
        try:
            found = digest(deps.factory, deps.store, now)
            if found:
                deps.tg.send(deps.owner_chat_id, *found)
        except Exception:
            log.exception("утренняя сводка")
    for batch in deps.store.batches("approved"):
        if batch["publish_at"] and batch["publish_at"] <= now and \
                deps.store.move_batch(batch["log_id"], ("approved",), "publishing"):
            _publish(deps, deps.owner_chat_id, batch)
    last = deps.store.get_meta("batches_poll")
    if last and now - float(last) < POLL_EVERY:
        return
    deps.store.set_meta("batches_poll", str(now))
    for batch in deps.store.batches("open"):
        try:
            status = deps.factory.batch_status(batch["keys"])
        except Exception:
            log.exception("статус партии %s", batch["log_id"])
            continue
        if status["in_work"]:
            continue                                  # ещё генерируется — ждём всю партию
        if not status["ready"]:
            if status['published'] == status['total'] and status['total']:
                if deps.store.move_batch(batch['log_id'], ('open',), 'published', expected_updated=batch['updated']):
                    deps.tg.send(deps.owner_chat_id,
                        f"📦 Партия «{batch['category']}» уже опубликована: "
                        f"{status['published']} из {status['total']} карточек. Новая публикация не запускалась.")
                continue
            if deps.store.move_batch(batch["log_id"], ("open",), "failed", expected_updated=batch['updated']):
                from orchestrator_bot.key_commands import readable_errors
                detail = readable_errors(status.get('errors', []))
                deps.tg.send(deps.owner_chat_id,
                    f"⚠️ Партия «{batch['category']}» сейчас без готовых превью.\n"
                    f"Готово к проверке: 0 из {status['total']} карточек. "
                    f"Уже опубликовано: {status['published']}. Остальные карточки: {status['dropped']}.\n"
                    "Причины — в отчётах фотоаудитора @foto_auditor_bot и статусе Никиты. "
                    "Новая публикация не запускалась." + ('\n\n' + detail if detail else ''))
            continue
        if not deps.store.move_batch(batch["log_id"], ("open",), "ready", expected_updated=batch['updated']):
            continue
        if status['dropped'] or status['published']:
            summary = (f"📦 В партии «{batch['category']}» готово к проверке: "
                       f"{status['ready']} из {status['total']} карточек.\n"
                       f"Уже опубликовано: {status['published']}. Остальные карточки: {status['dropped']}.\n")
        else:
            summary = f"📦 Партия «{batch['category']}» готова: {status['ready']} карточек.\n"
        when = _when(batch["publish_at"], now)
        deps.tg.send(deps.owner_chat_id,
                     summary + "Посмотреть — в группе проверки @Sendpr1ce_bot. "
                     + (f"По вашему плану выход {when}." if when else "Опубликую пачкой по кнопке."),
                     batch_buttons(batch, now))
