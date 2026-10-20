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
    return WhisperModel(size, device="cpu", compute_type="int8")


class Transcriber:
    def __init__(self, model_size: str = "small", model_factory=None):
        self.model_size = model_size
        self.model_factory = model_factory or _whisper
        self._model = None

    def __call__(self, ogg: bytes, hint: str) -> str:
        if self._model is None:
            self._model = self.model_factory(self.model_size)
        segments, _ = self._model.transcribe(decode_ogg(ogg), language="ru", vad_filter=True,
                                             beam_size=5, initial_prompt=hint or None)
        return " ".join(s.text.strip() for s in segments if s.text.strip())
