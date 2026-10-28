"""Submit owner-supplied price rows while retaining their original evidence.

The content-factory source stays unchanged. Supplier screenshots are evidence
for prices and stock; they are never installed as product reference photos.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 8
MAX_BUNDLE_BYTES = 150 * 1024 * 1024
MIME_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _immutable(path: Path, data: bytes) -> None:
    """Retries may reuse evidence, but can never silently overwrite it."""
    try:
        with path.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError("Данные поставщика для этого сообщения уже сохранены и отличаются") from None


def _attachments(values) -> list[tuple[dict, bytes]]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_ATTACHMENTS:
        raise ValueError("Неверный список фотографий поставщика")
    result, total = [], 0
    for index, value in enumerate(values):
        if not isinstance(value, dict) or value.get("mime_type") not in MIME_EXTENSIONS:
            raise ValueError("Поддерживаются фотографии PNG, JPEG и WEBP")
        encoded = value.get("data")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("Фотография поставщика пуста или больше 10 МБ")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Повреждены данные фотографии поставщика") from None
        mime = value["mime_type"]
        signature = ((mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                     or (mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
                     or (mime == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP"))
        if not data or len(data) > MAX_IMAGE_BYTES or not signature:
            raise ValueError("Фотография поставщика не соответствует указанному формату")
        total += len(data)
        if total > 20 * 1024 * 1024:
            raise ValueError("Суммарный размер фотографий превышает 20 МБ")
        meta = {"index": index, "mime_type": mime, "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data), "path": f"source-{index}{MIME_EXTENSIONS[mime]}"}
        # Names are retained only as metadata; Telegram filenames never choose paths.
        for key in ("file_unique_id", "file_name"):
            if value.get(key) is not None:
                if not isinstance(value[key], str) or len(value[key]) > 512:
                    raise ValueError("Неверные метаданные фотографии поставщика")
                meta[key] = value[key]
        if type(value.get("source_message_id")) is int and value["source_message_id"] > 0:
            meta["source_message_id"] = value["source_message_id"]
        result.append((meta, data))
    return result


def _pipeline_name(item, category):
    """The factory derives its research category from the name before the brand."""
    def normalized(value):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()
    name = normalized(item["name"])
    if any(re.match(re.escape(normalized(item[key])) + r"(?:\b|$)", name)
           for key in ("brand", "model")):
        return f"{category} {item['name']}"
    return item["name"]


def _wake(queue_db) -> None:
    from content_factory.bot import worker_control
    from content_factory.cards_pipeline import wake_agent
    try:
        worker_control.request_run()
    except Exception:
        pass  # The factory's existing timer remains the recovery path.
    if queue_db:
        try:
            wake_agent(queue_db)
        except sqlite3.Error:
            pass


def enqueue_bundle(cf, args, bundle: dict) -> dict:
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    from orchestrator_bot.supplier_import import normalize_action, prepare_items

    if not isinstance(bundle, dict):
        raise ValueError("Неверные данные поставщика")
    source_id = bundle.get("source_id")
    if not isinstance(source_id, str) or not source_id or len(source_id) > 160:
        raise ValueError("Не передан источник данных поставщика")
    action = normalize_action(bundle.get("action"))
    images = _attachments(bundle.get("attachments", []))
    items = [{**item, "pipeline_name": _pipeline_name(item, action["category"])}
             for item in prepare_items(action, source_id)]
    for item in items:
        index = item.get("attachment_index")
        if index is not None and (type(index) is not int or not 0 <= index < len(images)):
            raise ValueError("У товара не найдена исходная фотография поставщика")

    state_db, _, queue_db = cf._paths(args)
    # Only the factory's trusted configuration determines where evidence is saved.
    folder = Path(state_db).resolve().parent / "supplier_imports" / hashlib.sha256(source_id.encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "source_id": source_id, "action": action, "items": items,
                "attachments": [meta for meta, _ in images]}
    manifest_path = folder / "manifest.json"
    for meta, data in images:
        _immutable(folder / meta["path"], data)
    _immutable(manifest_path, _json_bytes(manifest))

    receipt_path = folder / "selection.json"
    if receipt_path.exists():
        result = json.loads(receipt_path.read_text(encoding="utf-8"))
        return {**result, "duplicate": True}

    # The factory selection transaction and this evidence store are separate.
    # A process can die after selection but before its receipt is written. Such
    # an attempt must remain visible and must never blindly select failed tasks
    # again (which could buy a second generation).
    dispatch_path = folder / "selection-started.json"
    try:
        with dispatch_path.open("xb") as output:
            output.write(_json_bytes({"manifest_sha256": hashlib.sha256(_json_bytes(manifest)).hexdigest()}))
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        raise ValueError("Результат постановки товаров неизвестен. Проверьте очередь перед повторным запуском; "
                         "автоматическая повторная постановка остановлена") from None
    rows = [(item["key"], item["brand"], item["model"], item["pipeline_name"], int(item["price"])) for item in items]
    # Generation starts immediately. Any publication schedule stays in the
    # orchestrator batch, just as it does for ordinary catalog selections.
    accepted = ExcelStore(state_db).select_items(rows, queue_db=queue_db) if rows else []
    keys = [row[0] for row in accepted]
    selected = set(keys)
    result = {"requested": len(items), "accepted": keys,
              "items": [item for item in items if item["key"] in selected],
              "manifest_path": str(manifest_path)}
    _immutable(receipt_path, _json_bytes(result))
    if keys and not getattr(args, "no_wake", False):
        _wake(queue_db)
    return result


def run(cf, args) -> dict:
    path = Path(args.supplier_file)
    if not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Не найден или слишком велик файл данных поставщика")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("Повреждён файл данных поставщика") from None
    return enqueue_bundle(cf, args, bundle)
