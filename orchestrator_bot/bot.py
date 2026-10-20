"""Telegram-бот оркестратора: приём сообщений владельца, голос, кнопки, режим без ПК."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from orchestrator_bot import actions, intents, workers
from orchestrator_bot.api import start_of_day
from orchestrator_bot.intents import Outcome

log = logging.getLogger("orchestrator_bot")

OFFLINE_NOTICE = "Принял, разберу, когда ПК будет на связи"
NOT_HEARD = "🎙 Не разобрал голосовое — повторите или напишите текстом"
TASK_WIZARD_URL = "https://t.me/Sendpr1ce_bot"
ALERT_EVERY = 12 * 3600


@dataclass
class Deps:
    store: object
    tg: object
    transcriber: Callable[[bytes, str], str]
    factory: object
    owner_chat_id: int
    clock: Callable[[], float] = time.time
    heartbeat_stale_after: int = 120
    threshold: int = 40
    report_fn: Callable[[], str] | None = field(default=None)


def build_hint(hint: dict) -> str:
    parts = ["Задачи контент-завода."]
    if hint.get("brands"):
        parts.append("Бренды и поставщики: " + ", ".join(hint["brands"]) + ".")
    if hint.get("sources"):
        parts.append("Источники: " + ", ".join(hint["sources"]) + ".")
    return " ".join(parts)


def _pc_line(deps, now) -> str:
    hb = deps.store.last_heartbeat()
    if hb is not None and now - hb <= deps.heartbeat_stale_after:
        return "ПК: на связи"
    return "ПК: не на связи" + (time.strftime(" с %H:%M %d.%m", time.localtime(hb)) if hb else "")


def status_message(deps) -> tuple[str, list | None]:
    now = deps.clock()
    try:
        by_status = deps.factory.status(start_of_day(now)).get("by_status", {})
        pipeline = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())) or "пусто"
    except Exception as exc:                        # content-factory недоступен — всё равно отвечаем
        pipeline = f"недоступен ({exc.__class__.__name__})"
    ready = True
    try:
        ready, agent = workers.readiness(deps.factory.workers())
    except Exception as exc:
        agent = f"Контент-агент: не удалось проверить ({exc.__class__.__name__})"
    text = (f"📋 Статус\nКонвейер: {pipeline}\n{agent}\n{_pc_line(deps, now)}\n"
            f"В очереди к ПК: {deps.store.count_new()}")
    return text, (None if ready else [[workers.START_BUTTON]])     # остановлен — запуск одной кнопкой


BTN_STATUS, BTN_REPORT, BTN_WIZARD = "📋 Статус", "🧾 Сводка", "🛠 Мастер задач"
MAIN_KEYBOARD = [[BTN_STATUS, BTN_REPORT], [BTN_WIZARD]]
BOT_COMMANDS = [("status", "Статус завода и контент-агента"), ("report", "Сводка за день"),
                ("menu", "Показать кнопки")]


def _menu(deps, chat_id):
    # постоянная клавиатура внизу чата — не уезжает с лентой, /start больше не нужен
    deps.tg.send(chat_id, "Кнопки внизу. Задачу можно просто написать или надиктовать.",
                 reply_keyboard=MAIN_KEYBOARD)


def _wizard(deps, chat_id):
    deps.tg.send(chat_id, "Мастер задач — в боте конвейера:", [[{"text": BTN_WIZARD, "url": TASK_WIZARD_URL}]])


def _report(deps, chat_id):
    deps.tg.send(chat_id, deps.report_fn() if deps.report_fn else "Сводка пока недоступна")


def _with_reply_context(message: dict, text: str) -> str:
    """Ответ на черновик: id черновика («id: …» в конце его текста) едет в разговор с Claude."""
    replied = (message.get("reply_to_message") or {}).get("text") or ""
    found = re.findall(r"(?m)^id: ([A-Za-z0-9_-]{1,80})\s*$", replied)
    return f"{text}\n[ответ на черновик id: {found[-1]}]" if found else text


def _queue(deps, chat_id, tg_message_id, kind, text):
    if deps.store.add_message(tg_message_id, kind, text, now=deps.clock()) is None:
        return                                       # повторная доставка — уже в очереди
    hb = deps.store.last_heartbeat()
    if hb is None or deps.clock() - hb > deps.heartbeat_stale_after:
        deps.tg.send(chat_id, OFFLINE_NOTICE)


def _voice(deps, chat_id, message):
    mid = deps.tg.send(chat_id, "🎙 Расшифровываю…")
    try:
        try:
            hint = build_hint(deps.factory.hint())
        except Exception:
            hint = build_hint({})
        text = (deps.transcriber(deps.tg.download_voice(message["voice"]["file_id"]), hint) or "").strip()
    except Exception:
        log.exception("расшифровка голосового не удалась")
        text = ""
    if not text:
        deps.tg.edit(chat_id, mid, NOT_HEARD)
        return
    deps.tg.edit(chat_id, mid, f"🎙 Услышал: {text}")
    _queue(deps, chat_id, message["message_id"], "voice", _with_reply_context(message, text))


def _callback(deps, cq):
    chat_id = cq["message"]["chat"]["id"]
    data = cq.get("data") or ""
    try:
        deps.tg.answer_callback(cq["id"])
    except Exception:                                # «query is too old» — кнопку всё равно исполняем
        log.info("answerCallbackQuery не прошёл", exc_info=True)
    if data == "m:status":
        deps.tg.send(chat_id, *status_message(deps))
    elif data == "m:report":
        _report(deps, chat_id)
    elif data == "w:start":
        try:
            deps.tg.send(chat_id, workers.start(deps.factory))
        except Exception as exc:
            deps.tg.send(chat_id, f"❌ Запустить не удалось: {exc.__class__.__name__}")
    elif data.startswith("c:"):
        deps.tg.send(chat_id, actions.cancel_logged(data[2:], deps.factory, deps.store, None))
    elif data.startswith("a:"):
        _, ask_id, n = data.split(":")
        ask = deps.store.get_ask(int(ask_id))
        if not ask or not deps.store.use_ask(ask["id"]):
            return
        option = ask["options"][int(n)]
        action = ask["action"]
        if action.get("type") != "enqueue_products":
            # вариант из вопроса Claude — отдаём ответ обратно в разговор
            _queue(deps, chat_id, None, "text", f"Ответ на вопрос «{action.get('question', '')}»: {option}")
            return
        if option == "Отмена":
            deps.tg.send(chat_id, "Хорошо, не ставлю.")
            return
        count = deps.threshold if option.startswith("Первые") else int(option.split()[-1])
        result = actions.execute(Outcome("enqueue", {**action, "count": count}, action["category"]),
                                 deps.factory, deps.store, ask["msg_id"])
        results = [result]
        if result.log_id is not None and (warning := workers.note(deps.factory)):
            results.append(warning)
        text, keyboard = intents.render("", results)
        deps.tg.send(chat_id, text, keyboard)


def handle_update(update: dict, deps: Deps) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        if cq.get("from", {}).get("id") != deps.owner_chat_id:
            return
        _callback(deps, cq)
        return
    message = update.get("message")
    if not message:
        return
    chat_id = message.get("chat", {}).get("id")
    if chat_id != deps.owner_chat_id or message.get("from", {}).get("id") != deps.owner_chat_id:
        return
    if deps.store.has_tg_message(message.get("message_id")):
        return                                       # повторная доставка после перезапуска
    if message.get("voice"):
        _voice(deps, chat_id, message)
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    command = text.split()[0].lower()
    if command in ("/start", "/menu"):
        _menu(deps, chat_id)
    elif command == "/status" or text == BTN_STATUS:
        deps.tg.send(chat_id, *status_message(deps))
    elif command == "/report" or text == BTN_REPORT:
        _report(deps, chat_id)
    elif text == BTN_WIZARD:
        _wizard(deps, chat_id)
    else:
        _queue(deps, chat_id, message["message_id"], "text", _with_reply_context(message, text))


def offline_alert(deps: Deps, now: float) -> None:
    pending = deps.store.count_new()
    hb = deps.store.last_heartbeat()
    if not pending or (hb is not None and now - hb <= ALERT_EVERY):
        return
    last = deps.store.get_meta("offline_alert")
    if last and now - float(last) <= ALERT_EVERY:
        return
    deps.tg.send(deps.owner_chat_id, f"⚠️ ПК не на связи больше 12 часов, сообщений в очереди: {pending}")
    deps.store.set_meta("offline_alert", str(now))


def process_updates(updates: list[dict], deps: Deps) -> None:
    for update in updates:
        try:
            handle_update(update, deps)
        except Exception:
            log.exception("обработка обновления %s", update.get("update_id"))
        deps.store.set_meta("tg_offset", str(update["update_id"] + 1))   # переживает перезапуск


def run_forever(deps: Deps) -> None:
    while True:
        try:
            offset = deps.store.get_meta("tg_offset")
            process_updates(deps.tg.get_updates(int(offset) if offset else None), deps)
            offline_alert(deps, deps.clock())
        except Exception:
            log.exception("цикл бота")
            time.sleep(5)
