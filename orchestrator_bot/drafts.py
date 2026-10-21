"""Этап 3: принятый контролёром черновик → картинка → превью владельцу → ✅ публикация.

Публикует только этот детерминированный код по кнопке владельца; каналы, токены и защита
от повторов — в content-factory (agent_cli publish-tg / plan-vk). Статусы черновика:
visual (картинка рисуется) → ready (превью с кнопками) → publishing → published;
visual_failed, rejected, superseded (опубликована другая версия).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

log = logging.getLogger("orchestrator_bot")

CHANNELS = {"tg_editorial": "Telegram-канал", "vk_editorial": "группу ВК"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
POLL_EVERY = 60                      # опрос готовности картинок — раз в минуту
WISH_MARKER = "Обязательно учесть пожелания владельца"


def base_id(draft_id: str) -> str:
    return re.sub(r"-v\d+$", "", draft_id)


def buttons(n: int, channel: str) -> list:
    where = "в канал" if channel == "tg_editorial" else "в ВК-план"
    return [[{"text": f"✅ Опубликовать {where}", "callback_data": f"d:p:{n}"},
             {"text": "❌ Отклонить", "callback_data": f"d:r:{n}"}],
            [{"text": "🖼 Другая картинка", "callback_data": f"d:v:{n}"},
             {"text": "✏️ Править текст", "callback_data": f"d:e:{n}"}]]


def retry_buttons(n: int) -> list:
    return [[{"text": "🔄 Ещё картинку", "callback_data": f"d:i:{n}"},
             {"text": "❌ Отклонить", "callback_data": f"d:r:{n}"}]]


def submit(store, factory, tg, chat_id, draft: dict) -> dict:
    """Принять принятую контролёром версию от слушателя и заказать картинку."""
    if draft.get("channel") not in CHANNELS:
        raise ValueError(f"канал не публикуется из бота: {draft.get('channel')}")
    if not _ID.match(str(draft.get("id", ""))) or not str(draft.get("text", "")).strip():
        raise ValueError("у черновика нет id или текста")
    if not store.add_draft(draft, base_id(draft["id"])):
        return {"ok": True, "new": False}
    row = store.get_draft(draft["id"])
    carried = store.sibling_prompt(row["base"], row["id"], WISH_MARKER)
    if carried:                                       # пожелания к картинке прошлой версии не теряем
        store.update_draft(row["n"], visual_prompt=carried)
        row = store.get_draft(row["n"])
    tg.send(chat_id, f"📝 {row['title']}\n\n{row['text']}\n\nid: {row['id']}\n\n"
                     f"🖼 Рисую картинку — пришлю готовый пост с кнопкой «Опубликовать».")
    _order_visual(store, factory, tg, chat_id, row, new=False)
    return {"ok": True, "new": True}


def _order_visual(store, factory, tg, chat_id, row, new: bool):
    scene = row["visual_prompt"] or row["title"] or row["text"][:300]
    try:
        # ключ фото-агента — только [A-Za-z0-9_-]; повтор — новый ключ (старый привязан к упавшему заданию)
        key = f"orch-{row['id']}" + (f"-r{int(time.time())}" if new else "")
        job = factory.visual_submit(scene, key[:128], new=new)
    except Exception as exc:
        log.exception("заказ картинки %s", row["id"])
        store.update_draft(row["n"], status="visual_failed")
        tg.send(chat_id, f"❌ Картинку для «{row['title']}» заказать не удалось: {exc.__class__.__name__}",
                retry_buttons(row["n"]))
        return
    store.update_draft(row["n"], status="visual", image_job=int(job["job_id"]))


def poll(store, factory, tg, chat_id) -> None:
    """Готовые картинки → превью владельцу; упавшие → кнопка «Ещё картинку»."""
    for row in store.drafts_with_status("visual"):
        try:
            state = factory.visual_status(row["image_job"])
        except Exception:
            log.exception("статус картинки %s", row["id"])
            continue
        if state.get("path"):
            try:
                _preview(store, factory, tg, chat_id, row, state["path"])
            except Exception:
                log.exception("превью %s", row["id"])
        elif state.get("status") in ("failed", "cancelled", "error", "missing", "missing_file"):
            store.update_draft(row["n"], status="visual_failed")
            why = state.get("error") or state.get("status")
            tg.send(chat_id, f"❌ Картинка для «{row['title']}» не получилась: {str(why)[:200]}",
                    retry_buttons(row["n"]))


def final_text(store, factory, row) -> str:
    """Текст, который уйдёт в канал: тело редактора + подвал системы (раздел сайта, телефон)."""
    if row.get("final_text"):
        return row["final_text"]
    try:
        text = factory.compose(row["id"], row["channel"], row["text"])
    except Exception:
        log.exception("подвал поста %s", row["id"])
        return row["text"]
    store.update_draft(row["n"], final_text=text)
    return text


def _preview(store, factory, tg, chat_id, row, path):
    """Как будет в канале: TG — фото с подписью; ВК — фото и текст. Кнопки — под отдельным сообщением с id
    (ответ на него — правка этой версии)."""
    text = final_text(store, factory, row)
    if row["channel"] == "tg_editorial":
        tg.send_photo(chat_id, path, caption=text)
        control = f"👆 Пост для Telegram-канала готов.\nid: {row['id']}"
    else:
        tg.send_photo(chat_id, path)
        control = f"{text}\n\n👆 Пост для группы ВК готов.\nid: {row['id']}"
    mid = tg.send(chat_id, control, buttons(row["n"], row["channel"]))
    store.update_draft(row["n"], status="ready", image_path=path, control_message_id=mid)


def publish(store, factory, tg, chat_id, n: int) -> None:
    row = store.get_draft(n)
    if row is None or not store.move_draft(n, ("ready",), "publishing"):
        tg.send(chat_id, _already(row))
        return
    try:
        text = final_text(store, factory, row)
        if row["channel"] == "tg_editorial":
            res = factory.publish_tg(row["id"], text, row["image_path"])
        else:
            res = factory.plan_vk(row["id"], text, row["image_path"])
    except Exception as exc:
        log.exception("публикация %s", row["id"])
        res = {"ok": False, "error": f"{exc.__class__.__name__}: {str(exc)[-200:]}"}
    if not res.get("ok"):
        store.move_draft(n, ("publishing",), "ready")
        tg.send(chat_id, f"❌ Не опубликовано: {res.get('error') or 'без пояснения'}. Можно нажать ещё раз.")
        return
    if row["channel"] == "tg_editorial":
        done = f"✅ Опубликовано в канале: {res['url']}" if res.get("url") else "✅ Опубликовано в канале"
    else:
        when = datetime.fromtimestamp(res["due_at"]).strftime("%d.%m в %H:%M") if res.get("due_at") else ""
        done = f"✅ Пост в ВК-плане, выйдет {when}" if when else "✅ Пост в ВК-плане"
    store.update_draft(n, status="published", result=done)
    others = store.supersede_versions(row["base"], n)
    _clear(tg, chat_id, row)
    tg.send(chat_id, done + (f"\nДругие версии этого поста ({len(others)}) больше не публикуются." if others else ""))


def reject(store, tg, chat_id, n: int) -> None:
    row = store.get_draft(n)
    if row is None or not store.move_draft(n, ("new", "visual", "visual_failed", "ready"), "rejected"):
        tg.send(chat_id, _already(row))
        return
    _clear(tg, chat_id, row)
    tg.send(chat_id, f"❌ «{row['title']}» отклонён, публиковаться не будет.")


def redo_visual(store, factory, key, notes: str, tg=None, chat_id=None) -> str:
    """Новая картинка с пожеланиями владельца; текст поста не меняется. key — id или номер черновика."""
    row = store.get_draft(key)
    if row is None or row["status"] not in ("ready", "visual_failed"):
        return _already(row)
    base_prompt = row["visual_prompt"] or row["title"]
    scene = f"{base_prompt}. {WISH_MARKER}: {notes.strip()}"[:1500]
    store.update_draft(row["n"], visual_prompt=scene)
    if tg is not None:
        _clear(tg, chat_id, row)
    row = store.get_draft(row["n"])
    _order_visual(store, factory, tg or _Silent(), chat_id, row, new=True)
    if store.get_draft(row["n"])["status"] != "visual":
        return f"❌ Новую картинку для «{row['title']}» заказать не удалось"
    return f"🖼 Заказал новую картинку для «{row['title']}» с учётом: {notes.strip()[:200]}. Текст не меняю."


class _Silent:
    def send(self, *args, **kwargs):
        return None


def retry_image(store, factory, tg, chat_id, n: int) -> None:
    row = store.get_draft(n)
    if row is None or row["status"] != "visual_failed":
        tg.send(chat_id, _already(row))
        return
    tg.send(chat_id, f"🔄 Заказал новую картинку для «{row['title']}».")
    _order_visual(store, factory, tg, chat_id, row, new=True)


def _clear(tg, chat_id, row):
    if row.get("control_message_id"):
        try:
            tg.clear_buttons(chat_id, row["control_message_id"])
        except Exception:
            log.info("кнопки не убраны", exc_info=True)


_STATE = {"published": "уже опубликован", "publishing": "уже публикуется", "rejected": "уже отклонён",
          "superseded": "опубликована другая версия", "visual": "картинка ещё рисуется"}


def _already(row) -> str:
    if row is None:
        return "Черновик не найден"
    return f"«{row['title']}»: {_STATE.get(row['status'], 'действие уже выполнено')}"
