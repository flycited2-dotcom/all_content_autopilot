"""Validate owner-supplied image bytes and expose them to dialogue providers."""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import warnings
from pathlib import Path

from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 8
MAX_PIXELS = 40_000_000
FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"),
           "WEBP": ("image/webp", ".webp")}


def decode_images(payload: dict) -> list[dict]:
    """No remote URLs or caller-selected local paths are accepted."""
    message = payload.get("message", {})
    if not isinstance(message, dict):
        raise ValueError("Неверное сообщение с фотографиями")
    raw = message.get("attachments", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > MAX_IMAGES:
        raise ValueError("Неверный список фотографий")
    images, total = [], 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("mime_type") not in {v[0] for v in FORMATS.values()}:
            raise ValueError("Неподдерживаемый формат фотографии")
        encoded = item.get("data")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("Фотография слишком большая")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Повреждены данные фотографии") from None
        total += len(data)
        if not data or len(data) > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Превышен размер фотографий")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as photo:
                    if photo.format not in FORMATS or photo.width * photo.height > MAX_PIXELS:
                        raise ValueError("Неподдерживаемая фотография")
                    mime_type, extension = FORMATS[photo.format]
                    if mime_type != item["mime_type"]:
                        raise ValueError("Формат фотографии не соответствует данным")
                    photo.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError("Повреждена фотография") from None
        metadata = {}
        if type(item.get("source_message_id")) is int and item["source_message_id"] > 0:
            metadata["source_message_id"] = item["source_message_id"]
        images.append({"index": index, "mime_type": mime_type, "extension": extension,
                       "bytes": data, "data": base64.b64encode(data).decode("ascii"),
                       "sha256": hashlib.sha256(data).hexdigest(), "metadata": metadata})
    return images


def metadata_payload(payload: dict, images: list[dict]) -> dict:
    """Keep binary image data out of the JSON the model reads as text."""
    value = copy.deepcopy(payload)
    if images:
        value["message"]["attachments"] = [
            {"attachment_index": image["index"], "mime_type": image["mime_type"], "sha256": image["sha256"],
             **image["metadata"]}
            for image in images]
    return value


def materialize_images(payload: dict, root: Path) -> dict:
    """Claude Read receives only files generated from verified image bytes."""
    images = decode_images(payload)
    value = metadata_payload(payload, images)
    if not images:
        return value
    root = Path(root).resolve()
    directory = root / "runs" / "chat" / "attachments"
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Папка фотографий выходит за пределы проекта")
    for image, metadata in zip(images, value["message"]["attachments"]):
        path = directory / (image["sha256"] + image["extension"])
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Неверный путь фотографии")
        if not path.exists() or path.read_bytes() != image["bytes"]:
            path.write_bytes(image["bytes"])
        metadata["path"] = path.relative_to(root).as_posix()
    return value
