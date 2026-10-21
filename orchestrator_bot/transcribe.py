"""Расшифровка голосовых: faster-whisper small (CPU, int8) с подсказкой из каталога.

Выбрано проверкой на 6 голосовых владельца (2026-10-20): small + initial_prompt с
брендами распознаёт названия верно и в 3–4 раза быстрее длительности записи.
Аудио декодируем ffmpeg сами: встроенный декодер faster-whisper (av) несовместим.
"""
from __future__ import annotations

import subprocess


def decode_ogg(data: bytes):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         input=data, capture_output=True, check=True, timeout=120).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def _whisper(size):
    from faster_whisper import WhisperModel
    return WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=2)


class Transcriber:
    def __init__(self, model_size: str = "small", model_factory=None):
        self.model_size = model_size
        self.model_factory = model_factory or _whisper
        self._model = None

    def __call__(self, ogg: bytes, hint: str) -> str:
        if self._model is None:
            self._model = self.model_factory(self.model_size)
        # condition_on_previous_text=False: иначе на длинной записи модель зацикливается
        # («RULE, RULE, RULE…», голосовое владельца 2026-10-21)
        segments, _ = self._model.transcribe(decode_ogg(ogg), language="ru", vad_filter=True,
                                             beam_size=5, initial_prompt=hint or None,
                                             condition_on_previous_text=False)
        return collapse_repeats(" ".join(s.text.strip() for s in segments if s.text.strip()))


def collapse_repeats(text: str, limit: int = 2) -> str:
    """Подряд повторённое слово (больше limit раз) — сбой распознавания: оставляем одно."""
    out: list[str] = []
    for word in text.split():
        key = word.strip(",.;:!?").casefold()
        run = 0
        for prev in reversed(out):
            if prev.strip(",.;:!?").casefold() != key:
                break
            run += 1
        if key and run >= limit:
            continue
        out.append(word)
    return " ".join(out)
