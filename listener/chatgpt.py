"""ChatGPT plan inference through the official OAuth Responses route.

Only completed text is returned to the intent validator. This adapter never offers
tools, executes actions, uses API keys, or calls ChatGPT's private endpoints.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from email.utils import parsedate_to_datetime
from typing import Iterator

import httpx

from listener.provider_errors import ProviderError

BASE_URL = "https://api.openai.com/v1"
MODEL_TTL = 600
STREAM_SECONDS = 180
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
TIMEOUT = httpx.Timeout(60, connect=10, write=30, pool=10)

_CODE_KINDS = {
    "subscription_sharing_usage_limit_exceeded": "usage_limit",
    "subscription_sharing_usage_unavailable": "network",
    "subscription_sharing_user_unavailable": "network",
    "subscription_sharing_user_not_eligible": "configuration",
    "subscription_sharing_unsupported_capability": "configuration",
    "subscription_sharing_route_not_supported": "configuration",
    "subscription_sharing_invalid_user": "auth",
    "chatpass_v2_scope_not_authorized": "configuration",
    "chatpass_v2_invalid_authorization_context": "configuration",
    "insufficient_quota": "usage_limit",
    "quota_exceeded": "usage_limit",
    "rate_limit_exceeded": "rate_limit",
    "invalid_api_key": "auth",
    "invalid_authentication": "auth",
    "model_not_found": "configuration",
}
_MESSAGES = {
    "usage_limit": "ChatGPT: достигнут лимит использования плана",
    "network": "ChatGPT временно недоступен; запрос не завершён",
    "configuration": "ChatGPT: проверьте разрешения и настройки подключения",
    "auth": "ChatGPT: требуется проверить вход в аккаунт",
    "rate_limit": "ChatGPT: временно превышена частота запросов",
    "invalid_response": "ChatGPT: не получен корректный завершённый ответ",
}


def _error_code(body) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        response = body.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        error = body
    code = error.get("code")
    return code if isinstance(code, str) and code in _CODE_KINDS else None


def _retry_at(value: str | None, now: float) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return now + max(0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value).timestamp()
            return max(now, timestamp) if math.isfinite(timestamp) else None
        except (ValueError, TypeError, OverflowError):
            return None


def _failure(status=0, body=None, retry_after=None, now=None, default="invalid_response") -> ProviderError:
    code = _error_code(body)
    if code:
        kind = _CODE_KINDS[code]
    elif status == 401:
        kind = "auth"
    elif status == 408:
        kind = "network"
    elif status == 429:
        kind = "rate_limit"
    elif status >= 500:
        kind = "network"
    elif 400 <= status < 500:
        kind = "configuration"
    else:
        kind = default
    retry = _retry_at(retry_after, time.time() if now is None else now)
    return ProviderError("chatgpt", kind, _MESSAGES[kind] + (f" ({code})" if code else ""), retry_at=retry)


def _error_body(response: httpx.Response):
    """Bound error diagnostics; never copy arbitrary server text to exceptions."""
    raw = bytearray()
    for chunk in response.iter_bytes():
        raw.extend(chunk)
        if len(raw) > MAX_ERROR_BYTES:
            return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def _events(response: httpx.Response) -> Iterator[tuple[str, str]]:
    """SSE framing, including multiple data fields and an event at clean EOF."""
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    buffer, data, event = "", [], ""
    received = 0
    deadline = time.monotonic() + STREAM_SECONDS

    def lines(final=False):
        nonlocal buffer
        while buffer:
            # A trailing CR can be half of CRLF in the next HTTP chunk.
            positions = [p for p in (buffer.find("\n"), buffer.find("\r")) if p >= 0]
            if not positions:
                break
            at = min(positions)
            if buffer[at] == "\r" and at == len(buffer) - 1 and not final:
                break
            skip = 2 if buffer[at:at + 2] == "\r\n" else 1
            line, buffer = buffer[:at], buffer[at + skip:]
            yield line
        if final and buffer:
            line, buffer = buffer, ""
            yield line

    def consume(line):
        nonlocal data, event
        if not line:
            found = (event, "\n".join(data)) if data else None
            data, event = [], ""
            return found
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "event":
            event = value
        return None

    try:
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received > MAX_STREAM_BYTES or time.monotonic() > deadline:
                raise _failure(default="network")
            buffer += decoder.decode(chunk)
            for line in lines():
                found = consume(line)
                if found:
                    yield found
        buffer += decoder.decode(b"", final=True)
        for line in lines(final=True):
            found = consume(line)
            if found:
                yield found
        if data:
            yield event, "\n".join(data)
    except UnicodeDecodeError:
        raise _failure() from None


class ChatGPTProvider:
    def __init__(self, auth, http=None, model=None, clock=time.time):
        self.auth = auth
        self.http = http if http is not None else httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self.model = model or os.environ.get("ORCH_CHATGPT_MODEL") or None
        self.clock = clock
        self._catalog = None
        self._catalog_until = 0
        self._catalog_identity = None

    def ready(self) -> bool:
        return bool(self.auth.ready())

    def _model(self, token: str) -> str:
        identity = hashlib.sha256(token.encode("utf-8")).digest()
        if self._catalog is None or self.clock() >= self._catalog_until or identity != self._catalog_identity:
            response = self.http.get(BASE_URL + "/models", headers={"Authorization": f"Bearer {token}"},
                                     timeout=TIMEOUT, follow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise _failure(response.status_code, _error_body(response), response.headers.get("Retry-After"), self.clock())
            try:
                models = response.json()["models"]
                if not isinstance(models, list):
                    raise ValueError
                catalog = [(m["slug"], m.get("visibility")) for m in models if isinstance(m, dict)
                           and isinstance(m.get("slug"), str) and m["slug"]]
            except (ValueError, KeyError, TypeError):
                raise _failure() from None
            self._catalog, self._catalog_identity = catalog, identity
            self._catalog_until = self.clock() + MODEL_TTL
        if self.model:
            if self.model not in {slug for slug, _ in self._catalog}:
                raise _failure(default="configuration")
            return self.model
        for slug, visibility in self._catalog:
            if visibility == "list":
                return slug
        raise _failure(default="configuration")

    def complete(self, instructions: str, payload: dict, history: list = ()) -> str:
        if not isinstance(instructions, str) or not isinstance(payload, dict):
            raise _failure(default="configuration")
        inputs = []
        for item in history:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant") or not isinstance(item.get("content"), str):
                raise _failure(default="configuration")
            inputs.append({"role": item["role"], "content": item["content"]})
        try:
            inputs.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))})
        except (TypeError, ValueError):
            raise _failure(default="configuration") from None
        return self._respond(instructions, inputs)["text"]

    def inspect(self, instructions: str, content: list, *, search=False, domains=()) -> dict:
        """Vision/research for the photo auditor; never enables generation tools."""
        if not isinstance(instructions, str) or not isinstance(content, list) or not content:
            raise _failure(default="configuration")
        return self._respond(instructions, [{"role": "user", "content": content}], search=search, domains=domains)

    def _respond(self, instructions, inputs, *, search=False, domains=()):
        try:
            token = self.auth.access_token()
            body = {"model": self._model(token), "instructions": instructions, "input": inputs,
                    "store": False, "stream": True}
            if search:
                tool = {"type": "web_search"}
                if domains:
                    tool["filters"] = {"allowed_domains": list(domains)}
                body.update(tools=[tool], include=["web_search_call.action.sources"])
            headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
            finalized = {}
            output_items = []
            with self.http.stream("POST", BASE_URL + "/responses", json=body, headers=headers,
                                  timeout=TIMEOUT, follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise _failure(response.status_code, _error_body(response), response.headers.get("Retry-After"), self.clock())
                for name, data in _events(response):
                    if data.strip() == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                        if not isinstance(event, dict):
                            raise ValueError
                    except (ValueError, TypeError):
                        raise _failure() from None
                    kind = event.get("type") or name
                    if name and event.get("type") and name != event["type"]:
                        raise _failure()
                    if kind in ("response.failed", "response.incomplete", "error"):
                        raise _failure(body=event, retry_after=response.headers.get("Retry-After"), now=self.clock())
                    if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                        output_items.append(event["item"])
                    if kind == "response.output_text.done":
                        # ChatGPT plan streams can finalize text here, then send completed.output=[].
                        # Keep finalized parts, but never return them until response.completed arrives.
                        output_index, content_index = event.get("output_index"), event.get("content_index")
                        if (type(output_index) is not int or output_index < 0 or
                                type(content_index) is not int or content_index < 0 or
                                not isinstance(event.get("text"), str) or
                                not isinstance(event.get("item_id"), str)):
                            raise _failure()
                        key = (output_index, content_index)
                        part = (event["item_id"], event["text"])
                        if key in finalized and finalized[key] != part:
                            raise _failure()
                        finalized[key] = part
                    if kind == "response.completed":
                        result = event.get("response")
                        if not isinstance(result, dict) or result.get("status", "completed") != "completed":
                            raise _failure()
                        output = result.get("output")
                        if not isinstance(output, list):
                            raise _failure()
                        output = output or output_items
                        parts = []
                        for message in output:
                            if not isinstance(message, dict):
                                raise _failure()
                            content = message.get("content", [])
                            if not isinstance(content, list):
                                raise _failure()
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                                    parts.append(part["text"])
                        text = "".join(parts)
                        if not text and finalized:
                            text = "".join(finalized[key][1] for key in sorted(finalized))
                        if not text.strip():
                            raise _failure()
                        sources = set()
                        for item in output:
                            if item.get("type") == "web_search_call":
                                action = item.get("action") or {}
                                if isinstance(action.get("url"), str):
                                    sources.add(action["url"])
                                for source in action.get("sources") or []:
                                    if isinstance(source, dict) and isinstance(source.get("url"), str):
                                        sources.add(source["url"])
                            for part in item.get("content") or []:
                                for annotation in part.get("annotations") or []:
                                    if annotation.get("type") == "url_citation" and isinstance(annotation.get("url"), str):
                                        sources.add(annotation["url"])
                        return {"text": text, "sources": sorted(sources)}
            raise _failure(default="network")
        except httpx.RequestError:
            raise _failure(default="network") from None
