"""Bounded image snapshots; originals remain byte-for-byte intact."""
import base64
import hashlib
import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps

MAX_BYTES = 10 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 24_000_000


def image_bytes(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ValueError("Изображение пустое или больше 10 МБ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                if img.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError("Поддерживаются PNG, JPEG, WEBP")
                img.verify()
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Не удалось проверить изображение") from None
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def vision_image(data):
    image_bytes(data)
    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((1600, 1600))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=90)
    return {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode(),
            "detail": "high"}


def comparison_sheet(paths: list[Path], destination: Path):
    """Numbered thumbnails, matching the evidence IDs in the report."""
    from PIL import ImageDraw
    sheet = Image.new("RGB", (500 * min(3, len(paths)), 430 * ((len(paths) + 2) // 3)), "white")
    draw = ImageDraw.Draw(sheet)
    for n, path in enumerate(paths):
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((480, 390))
            x, y = n % 3 * 500, n // 3 * 430
            sheet.paste(img, (x + (500 - img.width) // 2, y + 30))
            draw.text((x + 10, y + 8), "GENERATED" if n == 0 else f"SOURCE {n}", fill="black")
    sheet.save(destination, "JPEG", quality=90)
