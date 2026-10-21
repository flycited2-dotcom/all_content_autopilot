"""Намерения от Claude: разбор, проверка, правила сомнения, текст ответа.

Claude не пишет в базу — он возвращает JSON-намерение. Здесь оно проверяется по
белому списку типов, а правила сомнения решают: ставить сразу или переспросить.
Чистые функции, без ввода-вывода.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

ACTION_TYPES = ("enqueue_products", "cancel", "run_editorial", "revise_editorial", "redo_visual", "ask")
_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_DRAFT_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class IntentError(ValueError):
    pass


@dataclass
class Outcome:
    kind: str                       # enqueue | ask | cancel | run_editorial | revise_editorial | not_found
    action: dict
    text: str
    options: list[str] = field(default_factory=list)
    matches: int = 0


@dataclass
class Result:
    outcome: Outcome
    text: str
    log_id: int | None = None
    ask_id: int | None = None
    buttons: list[dict] = field(default_factory=list)


def parse_intent(raw: str) -> dict:
    start = (raw or "").find("{")
    if start < 0:
        raise IntentError("в ответе нет JSON-объекта")
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        raise IntentError(f"ответ не JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise IntentError("намерение должно быть JSON-объектом")
    return obj


def _enqueue(action: dict) -> dict | None:
    category = str(action.get("category") or "").strip()
    if not category:
        return None
    count = action.get("count")
    quotas = action.get("quotas") or {}
    if not isinstance(quotas, dict):
        quotas = {}
    at = action.get("at")
    btu = action.get("btu")
    btu = [int(b) for b in btu if isinstance(b, int) and 5 <= b <= 60][:2] if isinstance(btu, list) else []
    frost = action.get("min_temp")
    clean = {"type": "enqueue_products", "category": category,
            "count": count if isinstance(count, int) and count > 0 else None,
            "quotas": {str(k).lower(): (v if isinstance(v, int) and v > 0 else None) for k, v in quotas.items()},
            "source": action.get("source") or None,
            "at": at if isinstance(at, str) and _TIME.match(at) else None,
            "day": action.get("day") or None}
    if btu:                                           # отбор по мощности/морозу — только если сказаны
        clean["btu"] = sorted(btu)
    if isinstance(frost, int) and 5 <= frost <= 40:
        clean["min_temp"] = frost
    return clean


def normalize(intent: dict) -> tuple[str, list[dict]]:
    reply = str(intent.get("reply") or "").strip()
    actions = []
    for action in intent.get("actions") or []:
        if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
            continue
        kind = action["type"]
        if kind == "enqueue_products":
            clean = _enqueue(action)
        elif kind == "cancel":
            clean = {"type": "cancel", "task_ref": str(action.get("task_ref") or "")} if action.get("task_ref") else None
        elif kind == "run_editorial":
            clean = {"type": "run_editorial", "brief": str(action.get("brief") or ""),
                     "channels": [c for c in action.get("channels") or [] if c in ("tg_editorial", "vk_editorial")]}
            at = action.get("at")
            if isinstance(at, str) and _TIME.match(at):    # «пост на вечер» — выход после ✅ в это время
                clean.update(at=at, day=action.get("day") or None)
            clean = clean if clean["brief"] else None
        elif kind in ("revise_editorial", "redo_visual"):
            draft_id = str(action.get("draft_id") or "")
            clean = ({"type": kind, "draft_id": draft_id, "notes": str(action.get("notes") or "")}
                     if _DRAFT_ID.match(draft_id) and action.get("notes") else None)
        else:
            options = [str(o) for o in action.get("options") or []]
            clean = {"type": "ask", "question": str(action.get("question") or ""), "options": options} \
                if action.get("question") else None
        if clean:
            actions.append(clean)
    return reply, actions


def due_text(action: dict) -> str | None:
    at = action.get("at")
    if not at:
        return None
    day = action.get("day") or "сегодня"
    if day in ("сегодня", "завтра"):
        return f"{day} {at}"
    match = _ISO_DAY.match(str(day))
    if match:
        return f"{match.group(3)}.{match.group(2)} {at}"
    return f"сегодня {at}"


def plan(actions: list[dict], resolve: Callable[[dict], dict], threshold: int = 40) -> list[Outcome]:
    outcomes = []
    for action in actions:
        kind = action["type"]
        if kind == "ask":
            outcomes.append(Outcome("ask", action, action["question"], options=list(action["options"])))
            continue
        if kind != "enqueue_products":
            outcomes.append(Outcome(kind, action, action.get("brief") or action.get("notes") or ""))
            continue
        found = resolve(action)
        total = int(found.get("total_matches") or 0)
        selected = len(found.get("items") or [])          # сколько реально поставится с учётом квот
        if total == 0:
            outcomes.append(Outcome("not_found", action, f"Не нашёл в наличии: «{action['category']}»"))
        elif action["count"] is None and selected > threshold:
            outcomes.append(Outcome("ask", action,
                                    f"По «{action['category']}» нашлось {selected} позиций — сколько ставить?",
                                    options=[f"Первые {threshold}", f"Все {selected}", "Отмена"], matches=selected))
        else:
            outcomes.append(Outcome("enqueue", action, action["category"], matches=total))
    return outcomes


def render(reply: str, results: list[Result]) -> tuple[str, list[list[dict]]]:
    lines = [reply] if reply else []
    keyboard: list[list[dict]] = []
    for result in results:
        if result.text:
            lines.append(result.text)
        if result.log_id is not None:
            keyboard.append([{"text": "Отменить", "callback_data": f"c:{result.log_id}"}])
        if result.ask_id is not None and result.outcome.options:
            # по одной кнопке в строке: три варианта в ряд Telegram обрезает до нечитаемого
            keyboard += [[{"text": option, "callback_data": f"a:{result.ask_id}:{n}"}]
                         for n, option in enumerate(result.outcome.options)]
        if result.buttons:
            keyboard.append(list(result.buttons))
    return "\n".join(lines), keyboard
