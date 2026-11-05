"""Резерв фотоаудитора: ChatGPT (вход Codex) ⇄ Claude Code по подписке.

2026-11-05 лимит ChatGPT (subscription_sharing_usage_limit_exceeded) остановил проверку
фото на весь вечер: у аудитора не было второго обработчика. Здесь — Claude с тем же
интерфейсом inspect(instructions, content, search=, domains=) -> {"text", "sources"} и
обёртка, которая при лимите/сбое одного обработчика переходит на другой.
Строгость не меняется: ответ проверяют те же strict_json/checked_comparison, а страницы
источников аудитор сам скачивает и сверяет.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from listener import brain
from listener.provider_errors import ProviderError

log = logging.getLogger("listener")

SWITCH_KINDS = {"usage_limit", "rate_limit", "auth", "network", "timeout", "configuration"}
COOLDOWNS = {"usage_limit": 3600, "rate_limit": 120, "network": 60, "timeout": 120,
             "auth": 900, "configuration": 900}
_URL = re.compile(r"https?://[^\s\"'<>\\)\]]+")
_FENCE = re.compile(r"\A\s*```[a-zA-Z]*\s*\n?(.*?)\n?```\s*\Z", re.S)


def unfence(text: str) -> str:
    """Claude любит заворачивать ответ в ```json … ``` — проверка ждёт голый ответ."""
    match = _FENCE.match(text or "")
    return match.group(1).strip() if match else (text or "").strip()


class ClaudeInspector:
    """inspect() через `claude -p`: картинки — файлами, которые Claude открывает инструментом Read."""

    def __init__(self, root, runner=brain.run_with_kill, timeout=240, search_timeout=480):
        self.root, self.runner = Path(root), runner
        self.timeout, self.search_timeout = timeout, search_timeout

    def ready(self) -> bool:
        return shutil.which("claude") is not None

    def inspect(self, instructions: str, content: list, *, search=False, domains=()) -> dict:
        if not isinstance(instructions, str) or not isinstance(content, list) or not content:
            raise ProviderError("claude", "configuration", "Неверный запрос проверки")
        relative = Path("runs") / "photo-auditor" / "claude-inspect" / uuid.uuid4().hex
        folder = self.root / relative
        folder.mkdir(parents=True, exist_ok=True)
        try:
            parts, images = [], 0
            for item in content:
                if item.get("type") == "input_text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "input_image":
                    header, _, data = str(item.get("image_url", "")).partition(",")
                    if not header.startswith("data:image/") or ";base64" not in header:
                        raise ProviderError("claude", "configuration", "Изображение не в виде данных")
                    images += 1
                    ext = "png" if "png" in header else "webp" if "webp" in header else "jpg"
                    name = f"image-{images}.{ext}"
                    (folder / name).write_bytes(base64.b64decode(data))
                    parts.append(f"[ИЗОБРАЖЕНИЕ {images}: {(relative / name).as_posix()}]")
                else:
                    raise ProviderError("claude", "configuration", "Неизвестный тип материала")
            (folder / "task.md").write_text("# Инструкции\n\n" + instructions + "\n\n# Материалы\n\n"
                                            + "\n\n".join(parts) + "\n", encoding="utf-8")
            prompt = (f"Прочитай {(relative / 'task.md').as_posix()} и выполни раздел «Инструкции» для «Материалов». "
                      "Каждое [ИЗОБРАЖЕНИЕ N: путь] открой инструментом Read и смотри на сами пиксели. ")
            tools = ["Read"]
            if search:
                tools += ["WebSearch", "WebFetch"]
                prompt += "Для поиска используй WebSearch и WebFetch. "
                if domains:
                    prompt += "Ищи только на доменах: " + ", ".join(map(str, domains)) + ". "
            prompt += ("Файлы не создавай и не меняй, команды не запускай. Верни только требуемый ответ "
                       "в точном формате из инструкций, без пояснений и без рамок ```.")
            cmd = [brain.claude_bin(), "-p", prompt, *brain.SANDBOX, "--output-format", "stream-json", "--verbose",
                   "--allowedTools", ",".join(tools),
                   "--disallowedTools", "Bash,Edit,Write,NotebookEdit,Task,PowerShell"]
            try:
                proc = self.runner(cmd, cwd=str(self.root), stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, encoding="utf-8",
                                   timeout=self.search_timeout if search else self.timeout)
            except subprocess.TimeoutExpired:
                raise ProviderError("claude", "timeout", "Claude не завершил проверку вовремя") from None
            except OSError:
                raise ProviderError("claude", "configuration", "Не удалось запустить Claude") from None
            return self._parse(proc, search)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _parse(proc, search):
        result, sources = None, set()
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result":
                result = event
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "WebFetch":
                    url = (block.get("input") or {}).get("url")
                    if isinstance(url, str):
                        sources.add(url)
                elif block.get("type") == "tool_result" and search:
                    sources.update(_URL.findall(json.dumps(block.get("content"), ensure_ascii=False)))
        envelope = SimpleNamespace(returncode=proc.returncode, stderr=proc.stderr or "",
                                   stdout=json.dumps(result, ensure_ascii=False) if result else "")
        error = brain.claude_error(envelope)
        if error:
            raise error
        text = unfence(result.get("result")) if result and isinstance(result.get("result"), str) else ""
        if not text:
            raise ProviderError("claude", "invalid_response", "Claude не вернул ответ проверки")
        return {"text": text, "sources": sorted(sources)}


class FallbackInspector:
    """Первый готовый обработчик; при лимите или сбое — следующий. Пауза общая для всех
    экземпляров процесса (аудитор создаёт Auditor на каждое задание)."""

    _lock = threading.Lock()
    _until: dict[str, float] = {}
    _errors: dict[str, ProviderError] = {}

    def __init__(self, providers, clock=time.time):
        self.providers, self.clock = list(providers), clock

    def ready(self) -> bool:
        return any(p.ready() for _, p in self.providers)

    def _cooling(self, name):
        with self._lock:
            return self.clock() < self._until.get(name, 0)

    def _cool(self, name, error):
        now = self.clock()
        until = error.retry_at if error.retry_at and error.retry_at > now else now + COOLDOWNS.get(error.kind, 120)
        with self._lock:
            self._until[name] = until
        log.warning("Фотоаудитор: обработчик %s недоступен (%s) до %s — переключаюсь на резерв",
                    name, error.kind, time.strftime("%H:%M", time.localtime(until)))

    def inspect(self, instructions, content, *, search=False, domains=()):
        candidates = [(n, p) for n, p in self.providers if not self._cooling(n)] or self.providers[:1]
        for name, provider in candidates:
            try:
                result = provider.inspect(instructions, content, search=search, domains=domains)
            except ProviderError as error:
                if error.kind not in SWITCH_KINDS:
                    raise
                with self._lock:
                    self._errors[name] = error
                self._cool(name, error)
                continue
            with self._lock:
                self._until.pop(name, None)
            return result
        # Все на паузе. Отдаём аудитору ошибку основного обработчика (по ней он пишет причину)
        # как временное ожидание до ближайшего освобождения любого из них — не как «настройку»,
        # которая закрыла бы проверку навсегда.
        primary = self.providers[0][0]
        with self._lock:
            base = self._errors.get(primary) or next(iter(self._errors.values()), None)
            retry = min((self._until.get(n, 0) for n, _ in self.providers), default=0) or None
        kind = base.kind if base and base.provider == primary and base.kind in ("usage_limit", "rate_limit") else "network"
        raise ProviderError(primary, kind, "Обработчики проверки фото временно недоступны", retry_at=retry,
                            code=base.code if base and kind == base.kind else None)


def auditor_provider(root):
    from listener.chatgpt import ChatGPTProvider
    from listener.chatgpt_auth import ChatGPTAuth
    return FallbackInspector([("chatgpt", ChatGPTProvider(ChatGPTAuth(root))), ("claude", ClaudeInspector(root))])
