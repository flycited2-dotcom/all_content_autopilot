"""Ошибки обработчиков: только безопасное описание, без токенов и сырых ответов."""
from __future__ import annotations


class ProviderError(RuntimeError):
    def __init__(self, provider: str, kind: str, message: str, retry_at: float | None = None):
        self.provider = provider
        self.kind = kind
        self.retry_at = retry_at
        super().__init__(message)


class ProvidersUnavailable(RuntimeError):
    """Ни один обработчик не дал проверенного ответа; сообщение остаётся ожидающим."""
