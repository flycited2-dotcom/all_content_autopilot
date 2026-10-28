"""Validated supplier facts and deterministic prices, independent of the catalog.

The model transcribes source facts. Only this module calculates selling prices.
Original photographs are supplied by the server, never by a model-selected path.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import unicodedata


TYPE = "enqueue_supplier_products"
MAX_ITEMS = 200
_TIME = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d\Z")


def _text(value, label, maximum=300):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Неверное поле поставщика: {label}")
    value = value.strip()
    if any(ord(c) < 32 or c == "|" for c in value):
        raise ValueError(f"Недопустимые символы: {label}")
    return value


def _decimal(value, label, minimum, maximum):
    if type(value) not in (str, int, float, Decimal):
        raise ValueError(f"Неверное число: {label}")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Неверное число: {label}") from None
    if not number.is_finite() or not minimum <= number <= maximum:
        raise ValueError(f"Число вне диапазона: {label}")
    if number.as_tuple().exponent < -2:
        raise ValueError(f"Не более двух знаков после запятой: {label}")
    return number


def normalize_action(action):
    if not isinstance(action, dict) or action.get("type") != TYPE:
        raise ValueError("Неверный импорт поставщика")
    if set(action) - {"type", "category", "brand", "markup_percent", "items", "at", "day"}:
        raise ValueError("Лишние поля импорта поставщика")
    clean = {"type": TYPE, "category": _text(action.get("category"), "категория", 160),
             "markup_percent": str(_decimal(action.get("markup_percent"), "наценка", Decimal(0), Decimal(1000)))}
    if action.get("brand") is not None:
        clean["brand"] = _text(action["brand"], "бренд", 100)
    items = action.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError("Нужен список из 1–200 позиций поставщика")
    clean["items"] = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"name", "brand", "model", "cost_price", "stock", "attachment_index"}:
            raise ValueError("Неверные поля позиции поставщика")
        row = {key: _text(item.get(key), key, 500 if key == "name" else 160)
               for key in ("name", "brand", "model")}
        row["cost_price"] = str(_decimal(item.get("cost_price"), "входящая цена", Decimal("0.01"), Decimal("1000000000")))
        stock = item.get("stock")
        if type(stock) is not int or not 0 <= stock <= 1000000000:
            raise ValueError("Остаток должен быть целым неотрицательным числом")
        row["stock"] = stock
        if "attachment_index" in item:
            index = item["attachment_index"]
            if type(index) is not int or not 0 <= index < 10:
                raise ValueError("Неверный номер фотографии поставщика")
            row["attachment_index"] = index
        clean["items"].append(row)
    at, day = action.get("at"), action.get("day")
    if at is not None:
        if not isinstance(at, str) or not _TIME.fullmatch(at):
            raise ValueError("Неверное время выхода партии")
        clean["at"] = at
    if day is not None:
        from datetime import date
        if day not in ("сегодня", "завтра"):
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError("Неверная дата выхода партии")
            date.fromisoformat(day)
        clean["day"] = day
    return clean


def _identity(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).lower()


def prepare_items(action, source_id):
    action = normalize_action(action)
    factor = 1 + Decimal(action["markup_percent"]) / 100
    selected, seen = [], {}
    brand = _identity(action["brand"]) if action.get("brand") else None
    for item in action["items"]:
        if brand is not None and _identity(item["brand"]) != brand:
            continue
        key = f"excel|{_identity(item['brand'])}|{_identity(item['model'])}"
        facts = (Decimal(item["cost_price"]), item["stock"])
        if key in seen:
            if seen[key] != facts:
                raise ValueError(f"Противоречивые цены или остатки: {item['brand']} {item['model']}")
            continue
        seen[key] = facts
        if item["stock"] == 0:
            continue
        price = int((facts[0] * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if price < 1:
            raise ValueError("Цена продажи после округления должна быть положительной")
        selected.append({**item, "key": key, "price": price, "source_id": str(source_id),
                         "markup_percent": action["markup_percent"], "category": action["category"]})
    return selected


def source_message(store, msg_id):
    """Rebuild the exact bounded photo context from trusted persisted messages."""
    message = store.get_message(msg_id)
    if message is None:
        raise ValueError("Не найдено исходное сообщение поставщика")
    if not message.get("attachments"):
        recent = store.recent_photo_attachments(msg_id, limit=8)
        if recent:
            message = {**message, "attachments": recent, "attachment_context": "recent_photos"}
    return message


def validate_source(action, message):
    """A photo row must name an existing, server-provided source image."""
    images = message.get("attachments") or []
    if message.get("attachment_context") == "recent_photos" and all(
            "attachment_index" not in row for row in action["items"]):
        # A new textual price list is independent of optional older images.
        images = []
    for row in action["items"]:
        index = row.get("attachment_index")
        if images and (type(index) is not int or not 0 <= index < len(images)):
            raise ValueError("Позиция не привязана к исходной фотографии")
        if not images and index is not None:
            raise ValueError("Исходная фотография отсутствует")
    if not images and message.get("kind") not in ("text", "voice", "callback"):
        raise ValueError("Нет исходных данных поставщика")
    return images
