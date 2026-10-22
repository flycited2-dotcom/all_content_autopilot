"""Строгая граница модели: неверный фильтр не превращается в более широкую задачу."""
from __future__ import annotations

import json
import re
from datetime import date

from orchestrator_bot.intents import ACTION_TYPES, normalize


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Повтор поля JSON")
        value[key] = item
    return value


def _text(value, maximum=10000):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum


def _positive(value):
    return type(value) is int and value > 0


def _when(action):
    at, day = action.get("at"), action.get("day")
    if at is not None and (not isinstance(at, str) or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", at)):
        raise ValueError("Неверное время")
    if day is not None and day not in ("сегодня", "завтра"):
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("Неверная дата")
        date.fromisoformat(day)


def validated_intent(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Неверная константа JSON")))
    if not isinstance(value, dict) or set(value) != {"reply", "actions"} or not isinstance(value["reply"], str):
        raise ValueError("Неверный формат намерения")
    if len(value["reply"]) > 4000 or not isinstance(value["actions"], list) or len(value["actions"]) > 20:
        raise ValueError("Неверный формат намерения")
    for action in value["actions"]:
        if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
            raise ValueError("Неизвестное действие")
        kind = action["type"]
        allowed = {"type"}
        if kind == "enqueue_products":
            allowed.update(("category", "count", "quotas", "source", "btu", "min_temp", "at", "day", "inverter", "heat_pump", "full_dc"))
            if not _text(action.get("category"), 160):
                raise ValueError("Нет категории")
            count = action.get("count")
            if count is not None and not _positive(count):
                raise ValueError("Неверное количество")
            quotas = action.get("quotas", {})
            if not isinstance(quotas, dict) or len(quotas) > 100 or any(
                    not _text(k, 100) or (v is not None and not _positive(v)) for k, v in quotas.items()):
                raise ValueError("Неверные квоты")
            if action.get("source") is not None and not _text(action["source"], 200):
                raise ValueError("Неверный источник")
            if "btu" in action:
                btu = action["btu"]
                if not isinstance(btu, list) or not 1 <= len(btu) <= 2 or any(type(b) is not int or not 5 <= b <= 60 for b in btu):
                    raise ValueError("Неверная мощность")
            if "min_temp" in action and (type(action["min_temp"]) is not int or not 5 <= action["min_temp"] <= 40):
                raise ValueError("Неверная морозостойкость")
            for flag in ("inverter", "heat_pump", "full_dc"):
                if flag in action and type(action[flag]) is not bool:
                    raise ValueError("Неверный флаг отбора")
            _when(action)
        elif kind == "cancel":
            allowed.add("task_ref")
            if not isinstance(action.get("task_ref"), str) or not re.fullmatch(r"[1-9]\d{0,18}", action["task_ref"]):
                raise ValueError("Неверная ссылка на задачу")
        elif kind == "run_editorial":
            allowed.update(("brief", "channels", "at", "day"))
            if not _text(action.get("brief")):
                raise ValueError("Нет редакционного брифа")
            channels = action.get("channels", [])
            if not isinstance(channels, list) or len(channels) > 2 or any(c not in ("tg_editorial", "vk_editorial") for c in channels):
                raise ValueError("Неверные каналы")
            _when(action)
        elif kind in ("revise_editorial", "redo_visual"):
            allowed.update(("draft_id", "notes"))
            if not isinstance(action.get("draft_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", action["draft_id"]) or not _text(action.get("notes")):
                raise ValueError("Неверная правка черновика")
        elif kind == "ask":
            allowed.update(("question", "options"))
            if not _text(action.get("question"), 1000) or not isinstance(action.get("options"), list) or not 2 <= len(action["options"]) <= 4 or any(not _text(o, 25) for o in action["options"]):
                raise ValueError("Неверное уточнение")
        if set(action) - allowed:
            raise ValueError("Лишние поля действия")
    reply, actions = normalize(value)
    if len(actions) != len(value["actions"]):
        raise ValueError("Действие не прошло проверку")
    return {"reply": reply, "actions": actions}
