"""Расшифровка голосовых: faster-whisper small (CPU, int8) с подсказкой из каталога.

Выбрано проверкой на 6 голосовых владельца (2026-10-20): small + initial_prompt с
брендами распознаёт названия верно и в 3–4 раза быстрее длительности записи.
Аудио декодируем ffmpeg сами: встроенный декодер faster-whisper (av) несовместим.
"""
from __future__ import annotations

import subprocess
import re
from collections import Counter


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
        audio = decode_ogg(ogg)
        # condition_on_previous_text=False: иначе на длинной записи модель зацикливается
        # («RULE, RULE, RULE…», голосовое владельца 2026-10-21)
        first = self._pass(audio, hint or None, vad=True)
        if not unreliable_transcript(first, hint):
            return collapse_repeats(first)
        # A product title is a vocabulary hint, never evidence of what the
        # owner said. Whisper can echo it after VAD removed actual speech.
        # Decode the SAME audio once more without either source of bias.
        if not _audible(audio):
            return ''
        retried = self._pass(audio, None, vad=False)
        return '' if unreliable_transcript(retried, hint) else collapse_repeats(retried)

    def _pass(self, audio, hint, *, vad):
        segments, _ = self._model.transcribe(audio, language="ru", vad_filter=vad,
                                             beam_size=5, initial_prompt=hint,
                                             condition_on_previous_text=False)
        return ' '.join(s.text.strip() for s in segments if s.text.strip())


def _words(text):
    return re.findall(r'[^\W_]+', (text or '').casefold())


def _audible(audio):
    # Only used after an empty/suspect pass. Never ask an unprompted model to
    # invent speech for digitally silent audio.
    import numpy as np
    values = np.asarray(audio)
    return bool(values.size and np.max(np.abs(values)) >= 0.008)


def unreliable_transcript(text, hint):
    words = _words(text)
    if not words:
        return True
    counts = Counter(words)
    if len(words) >= 3 and max(counts.values()) >= max(3, len(words) / 2):
        return True
    if len(_words(collapse_repeats(text))) < len(words) * 0.8:
        return True
    # Keep short brand/model answers usable. A longer transcript containing
    # almost only the prompt is suspect; retry it rather than forwarding a
    # product description as the owner's correction.
    if hint and len(words) >= 3:
        hint_words = set(_words(hint))
        compact_hint = ''.join(_words(hint))
        echoes = sum(word in hint_words or len(word) >= 3 and word in compact_hint for word in words)
        if echoes / len(words) >= 0.85:
            return True
    return False


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
