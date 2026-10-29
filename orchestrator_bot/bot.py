"""Telegram-бот оркестратора: приём сообщений владельца, голос, кнопки, режим без ПК."""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from orchestrator_bot import actions, drafts, feed, intents, workers
from orchestrator_bot.api import start_of_day
from orchestrator_bot.intents import Outcome

log = logging.getLogger("orchestrator_bot")

OFFLINE_NOTICE = "Принял, разберу, когда ПК будет на связи"
NOT_HEARD = "🎙 Не разобрал голосовое — повторите или напишите текстом"
TASK_WIZARD_URL = "https://t.me/Sendpr1ce_bot"
ALERT_EVERY = 12 * 3600
AWAIT_NOTE = "await_note"
AWAIT_NOTE_TTL = 1800


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
    parts = ["Задачи контент-завода: кондиционеры, сплит-системы, тепловые насосы, BTU, инверторные.",
             "Поставщики: Бриз (Breeze), СплитХаб, Сплит Хоум (сайт), БытТехОпт, АРУ."]
    if hint.get("brands"):
        # ASR has a short prompt budget; the dialogue receives the complete catalog.
        parts.append("Бренды: " + ", ".join((hint.get('voice_brands') or hint['brands'])[:40]) + ".")
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
            f"В очереди к ПК: {deps.store.count_new()}{drafts_line(deps.store.drafts_summary(start_of_day(now)))}")
    keyboard = [[feed.FEED_BUTTON]] if ready else [[workers.START_BUTTON], [feed.FEED_BUTTON]]  # остановлен — запуск
    return text, keyboard


def drafts_line(summary: dict) -> str:
    parts = []
    if summary["ready"]:
        parts.append(f"ждут вашего ✅ — {summary['ready']}")
    if summary["visual"]:
        parts.append(f"рисуется картинка — {summary['visual']}")
    if summary["published"]:
        parts.append(f"опубликовано сегодня — {len(summary['published'])}")
    return ("\n📝 Посты: " + ", ".join(parts)) if parts else ""


BTN_STATUS, BTN_REPORT, BTN_WIZARD ="📋 Статус", "🧾 Сводка", "🛠 Мастер задач"
BTN_CATALOG = '📚 Каталоги и товары'
BTN_PHOTO_FEEDBACK = '📝 Ошибка в карточке'
MAIN_KEYBOARD = [[BTN_STATUS, BTN_REPORT], [BTN_CATALOG, BTN_WIZARD], [BTN_PHOTO_FEEDBACK]]
BOT_COMMANDS = [("status", "Статус завода и контент-агента"), ("report", "Сводка за день"),
                ("menu", "Показать кнопки"), ('catalog', 'Каталоги, источники и выбор товаров')]


def _menu(deps, chat_id):
    # постоянная клавиатура внизу чата — не уезжает с лентой, /start больше не нужен
    deps.tg.send(chat_id, "Кнопки внизу. Задачу можно просто написать или надиктовать.",
                 reply_keyboard=MAIN_KEYBOARD)


def _wizard(deps, chat_id):
    deps.tg.send(chat_id, "Мастер задач — в боте конвейера:", [[{"text": BTN_WIZARD, "url": TASK_WIZARD_URL}]])


def _catalog(deps, chat_id):
    from orchestrator_bot.api import text_chunks
    data = deps.factory.hint()
    lines = ['📚 Подключённые каталоги']
    for source in data.get('sources', []):
        label = source.removeprefix('manual__').replace('_', ' ')
        count = data.get('source_counts', {}).get(source)
        lines.append(f'• {label}' + (f': {count} поз.' if count is not None else ''))
    lines.append(f"Брендов: {len(data.get('brands', []))}.")
    lines.append('Напишите или надиктуйте: «Холодильники», «Все холодильники Beko», '
                 '«Выбери автоматически 7 телевизоров Xiaomi» или «Хочу выбрать модели сам». '
                 'Система предложит все модели, нужное количество или ручной выбор.')
    lines.append('Товары → дизайнер → проверка по производителю → исправления → превью. '
                 'Публикация — после вашего подтверждения.')
    for part in text_chunks('\n'.join(lines)):
        deps.tg.send(chat_id, part)


def _report(deps, chat_id):
    deps.tg.send(chat_id, deps.report_fn() if deps.report_fn else "Сводка пока недоступна")


def _with_reply_context(message: dict, text: str, include_quote=False) -> str:
    """Ответ на черновик: id черновика («id: …» в конце его текста) едет в разговор с Claude."""
    reply = message.get("reply_to_message") or {}
    replied = reply.get("text") or reply.get("caption") or ""
    found = re.findall(r"(?m)^id: ([A-Za-z0-9_-]{1,80})\s*$", replied)
    if found:
        return f"{text}\n[ответ на черновик id: {found[-1]}]"
    if include_quote and replied:
        return f"{text}\n[контекст ответа: {json.dumps(replied[:4000], ensure_ascii=False)}]"
    return text


def _take_note(deps, chat_id, text) -> str | None:
    """Ждём пожелание после кнопки «Другая картинка»/«Править текст» (30 минут). Картинку переделываем
    сразу на сервере; правку текста отдаём в разговор с пометкой черновика (как ответ на него)."""
    raw = deps.store.get_meta(AWAIT_NOTE)
    if not raw:
        return text
    deps.store.set_meta(AWAIT_NOTE, "")
    note = json.loads(raw)
    if deps.clock() - note["ts"] > AWAIT_NOTE_TTL:
        return text
    row = deps.store.get_draft(int(note["n"]))
    if row is None:
        return text
    if text.strip().casefold() in ('/cancel', 'отмена'):
        deps.tg.send(chat_id, 'Исправление отменено.')
        return None
    if note.get('image_job') is not None and note['image_job'] != row.get('image_job'):
        deps.tg.send(chat_id, 'Карточка уже изменилась. Нажмите «Указать ошибку» под актуальной версией.')
        return None
    if note["kind"] in ("visual", "correction"):
        if len(text.strip()) > 2000:
            deps.store.set_meta(AWAIT_NOTE, raw)
            deps.tg.send(chat_id, 'Замечание длиннее 2000 символов. Сократите его или напишите /cancel.')
            return None
        if note['kind'] == 'correction':
            note.update(notes=text.strip(), phase='confirm')
            deps.store.set_meta(AWAIT_NOTE, json.dumps(note, ensure_ascii=False))
            deps.tg.send(chat_id, f"Замечание к «{row['title']}»:\n\n{text}\n\n"
                         'По кнопке передам замечание дизайнеру и запущу новую картинку. Публикация не запускается.',
                         [[{'text': '🔄 Исправить по замечанию', 'callback_data': f"d:g:{row['n']}"}],
                          [{'text': 'Отмена', 'callback_data': f"d:h:{row['n']}"}]])
            return None
        deps.tg.send(chat_id, drafts.redo_visual(deps.store, deps.factory, row["n"], text, deps.tg, chat_id))
        return None
    return f"{text}\n[ответ на черновик id: {row['id']}]"


def _queue(deps, chat_id, tg_message_id, kind, text, *, raw_text=None, reply_message_id=None):
    from orchestrator_bot import photo_feedback_ui
    if photo_feedback_ui.take_note(deps, chat_id, text if raw_text is None else raw_text,
                                   reply_message_id=reply_message_id, message_id=tg_message_id):
        return
    from orchestrator_bot.selection_ui import handle_count
    if handle_count(deps, chat_id, tg_message_id, text):
        return
    if tg_message_id is not None:                    # живое сообщение владельца (не ответ на вопрос-кнопку)
        text = _take_note(deps, chat_id, text)
        if text is None:
            return
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
        from orchestrator_bot import photo_feedback_ui
        hint += photo_feedback_ui.voice_hint(deps)
        text = (deps.transcriber(deps.tg.download_voice(message["voice"]["file_id"]), hint) or "").strip()
    except Exception:
        log.exception("расшифровка голосового не удалась")
        text = ""
    if not text:
        deps.tg.edit(chat_id, mid, NOT_HEARD)
        return
    deps.tg.edit(chat_id, mid, f"🎙 Услышал: {text}")
    _queue(deps, chat_id, message["message_id"], "voice", _with_reply_context(message, text), raw_text=text,
           reply_message_id=(message.get('reply_to_message') or {}).get('message_id'))


def _photo(deps, chat_id, message):
    """A photograph is a real queued message, independent of the photo worker."""
    from orchestrator_bot.telegram_api import IMAGE_MAX_BYTES, image_mime
    try:
        if message.get("photo"):
            source = max(message["photo"], key=lambda p: (p.get("width", 0) * p.get("height", 0),
                                                        p.get("file_size", 0)))
        else:
            source = message.get("document") or {}
            if not (str(source.get("mime_type", "")).startswith("image/") or
                    re.search(r"\.(?:jpe?g|png|webp)$", str(source.get("file_name", "")), re.I)):
                raise ValueError("Пришлите фотографию или изображение файлом JPEG, PNG или WebP")
        if int(source.get("file_size") or 0) > IMAGE_MAX_BYTES:
            raise ValueError("Фотография слишком большая — максимум 10 МБ")
        data = deps.tg.download_file(source["file_id"], max_bytes=IMAGE_MAX_BYTES)
        if len(data) > IMAGE_MAX_BYTES:
            raise ValueError("Фотография слишком большая — максимум 10 МБ")
        attachment = {"mime_type": image_mime(data), "data": base64.b64encode(data).decode("ascii")}
        for field in ("file_unique_id", "file_name"):
            if source.get(field):
                attachment[field] = str(source[field])[:200]
    except ValueError as exc:
        deps.tg.send(chat_id, f"📷 {exc}.")
        return
    except Exception as exc:
        # HTTP exceptions can contain the bot-token URL; never put them in logs.
        log.warning("Не удалось скачать фотографию: %s", exc.__class__.__name__)
        deps.tg.send(chat_id, "📷 Не удалось загрузить фотографию. Пришлите её ещё раз.")
        return
    text = _with_reply_context(message, (message.get("caption") or "").strip() or "Фотография поставщика",
                               include_quote=True)
    try:
        if message.get("media_group_id"):
            saved = deps.store.add_photo_message(message["message_id"], text, attachment,
                                                  message["media_group_id"], now=deps.clock())
            created = bool(saved and saved[1])
        else:
            created = deps.store.add_message(message["message_id"], "photo", text, now=deps.clock(),
                                             attachments=[attachment]) is not None
    except ValueError as exc:
        deps.tg.send(chat_id, f"📷 {exc}.")
        return
    if not created:
        return
    hb = deps.store.last_heartbeat()
    notice = (OFFLINE_NOTICE if hb is None or deps.clock() - hb > deps.heartbeat_stale_after
              else "📷 Фотография принята, разбираю модели, цены и остатки.")
    deps.tg.send(chat_id, notice)


def _callback(deps, cq):
    chat_id = cq["message"]["chat"]["id"]
    data = cq.get("data") or ""
    if not data.startswith('s:') and deps.store.get_meta('selection_await_count'):
        deps.store.set_meta('selection_await_count', '')
    try:
        deps.tg.answer_callback(cq["id"])
    except Exception:                                # «query is too old» — кнопку всё равно исполняем
        log.info("answerCallbackQuery не прошёл", exc_info=True)
    if data == "m:status":
        deps.tg.send(chat_id, *status_message(deps))
    elif data.startswith('s:'):
        from orchestrator_bot.selection_ui import handle_callback
        handle_callback(deps, chat_id, cq)
    elif data.startswith('pf:'):
        from orchestrator_bot import photo_feedback_ui
        photo_feedback_ui.callback(deps, chat_id, data)
    elif data == "m:report":
        _report(deps, chat_id)
    elif data == "w:start":
        try:
            deps.tg.send(chat_id, workers.start(deps.factory))
        except Exception as exc:
            deps.tg.send(chat_id, f"❌ Запустить не удалось: {exc.__class__.__name__}")
    elif data == "f:show":
        found = feed.digest(deps.factory, deps.store, deps.clock())
        deps.tg.send(chat_id, *(found or ("🗂 Решать сейчас нечего — всё разобрано.", None)))
    elif data in ("f:tg", "f:vk", "f:old"):
        try:
            deps.tg.send(chat_id, feed.act(deps.factory, data[2:]))
        except Exception as exc:
            deps.tg.send(chat_id, f"❌ Не получилось: {exc.__class__.__name__}")
    elif data.startswith("b:"):                      # партия товаров: опубликовать пачкой / не публиковать
        _, op, log_id = data.split(":")
        feed.batch_action(deps, chat_id, op, int(log_id))
    elif data.startswith("d:"):                      # этап 3: черновик — опубликовать / отклонить / ещё картинку
        _, op, n = data.split(":")
        if op == "p":
            drafts.publish(deps.store, deps.factory, deps.tg, chat_id, int(n))
        elif op == "r":
            drafts.reject(deps.store, deps.tg, chat_id, int(n))
        elif op == "i":
            drafts.retry_image(deps.store, deps.factory, deps.tg, chat_id, int(n))
        elif op in ('g', 'h'):
            try:
                note = json.loads(deps.store.get_meta(AWAIT_NOTE) or '{}')
            except ValueError:
                note = {}
            row = deps.store.get_draft(int(n))
            if (not row or note.get('kind') != 'correction' or note.get('n') != int(n) or
                    note.get('phase') != 'confirm' or deps.clock() - note.get('ts', 0) > AWAIT_NOTE_TTL):
                deps.tg.send(chat_id, 'Это подтверждение больше не действует. Укажите ошибку под актуальной карточкой.')
                return
            deps.store.set_meta(AWAIT_NOTE, '')
            if op == 'h':
                deps.tg.send(chat_id, 'Исправление отменено. Новая картинка не заказывалась.')
            elif row.get('image_job') != note.get('image_job'):
                deps.tg.send(chat_id, 'Карточка уже изменилась. Нажмите «Указать ошибку» под актуальной версией.')
            else:
                deps.tg.send(chat_id, drafts.redo_visual(deps.store, deps.factory, row['n'], note['notes'], deps.tg, chat_id))
        elif op in ("v", "e", "f"):                  # следующее сообщение владельца — пожелание к этому черновику
            row = deps.store.get_draft(int(n))
            if row is None:
                return
            if op == 'f' and (row['status'] not in ('ready', 'visual_failed') or
                    row.get('control_message_id') and row['control_message_id'] != cq['message']['message_id']):
                deps.tg.send(chat_id, 'Эта версия уже не принимает исправления. Откройте актуальную карточку.')
                return
            kind = 'correction' if op == 'f' else ("visual" if op == "v" else "text")
            from orchestrator_bot import photo_feedback_ui
            if not photo_feedback_ui.dismiss_pending(deps.store):
                pending_id = deps.store.get_meta(photo_feedback_ui.PENDING)
                deps.tg.send(chat_id, 'Сначала проверьте передачу предыдущего исправления, чтобы не заказать его дважды.',
                             [[{'text': 'Проверить передачу', 'callback_data': 'pf:send:' + pending_id}]])
                return
            deps.store.set_meta(AWAIT_NOTE, json.dumps({"kind": kind, "n": row["n"], "ts": deps.clock(),
                                                       'image_job': row.get('image_job')}))
            if op == 'f':
                deps.tg.send(chat_id, f"Напишите или надиктуйте, что неверно в карточке «{row['title']}» "
                             "и как должно быть. Передам замечание дизайнеру вместе с предыдущим заданием. "
                             "Например: «Это дизельный генератор, убери бензин». "
                             "Сначала покажу текст, затем нужно подтвердить переделку кнопкой. Отмена — /cancel.")
                return
            what = "картинке" if kind == "visual" else "тексте"
            deps.tg.send(chat_id, f"Напишите или надиктуйте, что изменить в {what} «{row['title']}».")
    elif data.startswith("c:"):
        deps.tg.send(chat_id, actions.cancel_logged(data[2:], deps.factory, deps.store, None),
                     [[{"text": "↩️ Вернуть", "callback_data": f"u:{data[2:]}"}]])   # случайная отмена — одним нажатием
    elif data.startswith("u:"):
        text, keyboard = intents.render("", [actions.restore_logged(data[2:], deps.factory, deps.store)])
        deps.tg.send(chat_id, text, keyboard)
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
            text = "Хорошо, не ставлю."
            deps.store.record_exchange(f"Ответ на выбор количества «{action['category']}»: {option}", text)
            deps.tg.send(chat_id, text)
            return
        count = deps.threshold if option.startswith("Первые") else int(option.split()[-1])
        result = actions.execute(Outcome("enqueue", {**action, "count": count}, action["category"]),
                                 deps.factory, deps.store, ask["msg_id"])
        results = [result]
        if result.log_id is not None and (warning := workers.note(deps.factory)):
            results.append(warning)
        text, keyboard = intents.render("", results)
        deps.store.record_exchange(f"Ответ на выбор количества «{action['category']}»: {option}", text)
        deps.tg.send(chat_id, text, keyboard)


def handle_update(update: dict, deps: Deps) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        if (cq.get("from", {}).get("id") != deps.owner_chat_id or
                cq.get('message', {}).get('chat', {}).get('id') != deps.owner_chat_id):
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
    if message.get("photo") or message.get("document"):
        _photo(deps, chat_id, message)
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
    elif command == '/catalog' or text == BTN_CATALOG:
        _catalog(deps, chat_id)
    elif text == BTN_PHOTO_FEEDBACK:
        from orchestrator_bot import photo_feedback_ui
        photo_feedback_ui.list_cards(deps, chat_id)
    else:
        _queue(deps, chat_id, message["message_id"], "text", _with_reply_context(message, text), raw_text=text,
               reply_message_id=(message.get('reply_to_message') or {}).get('message_id'))


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


def poll_drafts(deps: Deps, now: float) -> None:
    last = deps.store.get_meta("drafts_poll")
    if last and now - float(last) < drafts.POLL_EVERY:
        return
    deps.store.set_meta("drafts_poll", str(now))
    drafts.poll(deps.store, deps.factory, deps.tg, deps.owner_chat_id)


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
            poll_drafts(deps, deps.clock())
            feed.tick(deps, deps.clock())
            drafts.publish_scheduled(deps.store, deps.factory, deps.tg, deps.owner_chat_id, deps.clock())
        except Exception:
            log.exception("цикл бота")
            time.sleep(5)
