"""Локальная долговечная доставка: намерение не генерируется повторно после потери ACK."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class PendingTurn:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        # Повреждение не закрывает сообщение и не подменяет его следующим.
        if not isinstance(value, dict) or not isinstance(value.get("payload", {}).get("message", {}).get("id"), int):
            raise ValueError("Повреждено сохранённое сообщение слушателя")
        return value

    def save(self, payload: dict, intent=None, editorial=None) -> None:
        write_json(self.path, {"payload": payload, "intent": intent, "editorial": editorial})

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
