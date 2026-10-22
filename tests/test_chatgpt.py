import json
from email.utils import formatdate

import httpx
import pytest

from listener import chatgpt
from listener.chatgpt import ChatGPTProvider
from listener.provider_errors import ProviderError


class Auth:
    token = "oauth-test-token"
    enabled = True

    def access_token(self):
        return self.token

    def ready(self):
        return self.enabled


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


MODELS = {"models": [{"slug": "hidden", "visibility": "hide"},
                     {"slug": "second", "display_name": "First visible", "visibility": "list"},
                     {"slug": "first", "visibility": "list"}]}
INTENT = '{"reply":"Принял 👋","actions":[]}'


def completed(text=INTENT):
    return {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]}}


def sse(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


@pytest.fixture
def make_provider(monkeypatch):
    monkeypatch.delenv("ORCH_CHATGPT_MODEL", raising=False)
    clients = []

    def make(response=None, models=None, model=None, auth=None, clock=lambda: 1000):
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json=MODELS if models is None else models)
            assert request.url.path == "/v1/responses"
            if callable(response):
                return response(request)
            return response if response is not None else httpx.Response(200, content=sse(completed()))

        client = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        return ChatGPTProvider(auth or Auth(), http=client, model=model, clock=clock), calls

    yield make
    for client in clients:
        client.close()


def test_request_uses_official_oauth_route_minimal_body_and_local_history(make_provider):
    provider, calls = make_provider()
    payload = {"message": {"id": 1, "text": "поставь кондиционеры"}, "context": {}}
    history = [{"role": "user", "content": "привет", "extra": "not sent"},
               {"role": "assistant", "content": '{"reply":"Здравствуйте","actions":[]}'}]
    assert provider.complete("Return only intent JSON", payload, history) == INTENT
    assert [str(r.url) for r in calls] == ["https://api.openai.com/v1/models", "https://api.openai.com/v1/responses"]
    assert all(r.headers["Authorization"] == "Bearer oauth-test-token" for r in calls)
    request = json.loads(calls[1].content)
    assert set(request) == {"model", "instructions", "input", "store", "stream"}
    assert request["model"] == "second"  # Preserve server ordering, skip hidden default.
    assert request["store"] is False and request["stream"] is True
    assert request["input"][:2] == [{"role": h["role"], "content": h["content"]} for h in history]
    assert json.loads(request["input"][-1]["content"]) == payload
    assert request["input"][-1]["role"] == "user"
    assert history[0]["extra"] == "not sent"
    assert calls[1].extensions["timeout"]["read"] == 60


def test_ready_only_checks_local_auth_without_network(make_provider):
    auth = Auth()
    provider, calls = make_provider(auth=auth)
    assert provider.ready()
    auth.enabled = False
    assert not provider.ready()
    assert calls == []


@pytest.mark.parametrize("model", ["first", "hidden"])
def test_explicit_model_must_be_in_account_catalog(make_provider, model):
    provider, calls = make_provider(model=model)
    provider.complete("intent", {})
    assert json.loads(calls[-1].content)["model"] == model


def test_environment_model_is_honored_and_constructor_takes_priority(make_provider, monkeypatch):
    monkeypatch.setenv("ORCH_CHATGPT_MODEL", "first")
    provider, calls = make_provider()
    provider.complete("intent", {})
    assert json.loads(calls[-1].content)["model"] == "first"
    provider, calls = make_provider(model="second")
    provider.complete("intent", {})
    assert json.loads(calls[-1].content)["model"] == "second"


@pytest.mark.parametrize("models,model", [(MODELS, "not-available"), ({"models": []}, None),
                                         ({"models": [{"slug": "hidden", "visibility": "hide"}]}, None)])
def test_missing_model_fails_before_inference(make_provider, models, model):
    provider, calls = make_provider(models=models, model=model)
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == "configuration" and len(calls) == 1


def test_catalog_cache_expires_and_changes_with_selected_account(make_provider):
    now, auth = [1000], Auth()
    provider, calls = make_provider(auth=auth, clock=lambda: now[0])
    provider.complete("intent", {})
    now[0] += 599
    provider.complete("intent", {})
    assert sum(r.method == "GET" for r in calls) == 1
    now[0] += 1
    provider.complete("intent", {})
    assert sum(r.method == "GET" for r in calls) == 2
    auth.token = "another-account-token"
    provider.complete("intent", {})
    assert sum(r.method == "GET" for r in calls) == 3
    assert calls[-1].headers["Authorization"] == "Bearer another-account-token"


def test_multiline_data_crlf_split_unicode_and_eof_event(make_provider):
    data = completed()
    data.pop("type")  # The SSE event field alone may name the event.
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    raw = ("\ufeff: heartbeat\r\nevent: response.completed\r\n" +
           "\r\n".join("data: " + line for line in encoded.splitlines())).encode()
    response = httpx.Response(200, stream=Chunks(bytes([b]) for b in raw))
    provider, _ = make_provider(response)
    assert provider.complete("intent", {}) == INTENT


@pytest.mark.parametrize("event,expected", [
    ({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}, "usage_limit"),
    ({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_unavailable"}}}, "network"),
    ({"type": "response.incomplete", "response": {"output": completed()["response"]["output"]}}, "invalid_response"),
    ({"type": "error", "code": "subscription_sharing_invalid_user"}, "auth"),
])
def test_partial_text_never_succeeds_when_terminal_event_is_failure(make_provider, event, expected):
    partial = {"type": "response.output_text.delta", "delta": INTENT}
    provider, _ = make_provider(httpx.Response(200, content=sse(partial) + sse(event)))
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == expected


@pytest.mark.parametrize("raw,expected", [
    (sse({"type": "response.output_text.delta", "delta": INTENT}), "network"),
    (b"data: [DONE]\n\n", "network"),
    (b"data: not-json\n\n", "invalid_response"),
    (sse(completed("")), "invalid_response"),
    (sse({"type": "response.completed", "response": {"status": "incomplete", "output": []}}), "invalid_response"),
    (sse({"type": "response.completed", "response": {"output": [{"content": 42}]}}), "invalid_response"),
    (b"event: error\n" + sse(completed()), "invalid_response"),
])
def test_incomplete_empty_or_malformed_stream_is_not_an_intent(make_provider, raw, expected):
    provider, _ = make_provider(httpx.Response(200, content=raw))
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == expected


@pytest.mark.parametrize("code,status,expected", [
    ("subscription_sharing_usage_limit_exceeded", 429, "usage_limit"),
    ("subscription_sharing_usage_unavailable", 503, "network"),
    ("subscription_sharing_user_unavailable", 503, "network"),
    ("subscription_sharing_user_not_eligible", 403, "configuration"),
    ("subscription_sharing_unsupported_capability", 400, "configuration"),
    ("subscription_sharing_route_not_supported", 403, "configuration"),
    ("subscription_sharing_invalid_user", 401, "auth"),
    ("insufficient_quota", 429, "usage_limit"),
])
def test_known_errors_have_safe_exact_code(make_provider, code, status, expected):
    provider, _ = make_provider(httpx.Response(status, json={"error": {"code": code, "message": "secret-token"}}))
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == expected and caught.value.provider == "chatgpt"
    assert code in str(caught.value) and "secret-token" not in str(caught.value)
    assert caught.value.retry_at is None


@pytest.mark.parametrize("status,expected", [(401, "auth"), (403, "configuration"), (503, "network"), (429, "rate_limit")])
def test_admission_error_body_is_not_assumed_to_be_api_error(make_provider, status, expected):
    provider, _ = make_provider(httpx.Response(status, json={"detail": "secret-token", "code": "arbitrary-secret"}))
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == expected
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("header,retry", [("25", 1025), (formatdate(1060, usegmt=True), 1060), ("invalid", None), ("NaN", None)])
def test_rate_limit_honors_retry_after_seconds_or_http_date(make_provider, header, retry):
    provider, _ = make_provider(httpx.Response(429, headers={"Retry-After": header}, json={"error": {"message": "busy"}}))
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == "rate_limit" and caught.value.retry_at == retry


def test_transport_error_is_sanitized(make_provider):
    def fail(request):
        raise httpx.ReadTimeout("secret oauth-test-token", request=request)

    provider, _ = make_provider(fail)
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == "network" and "secret" not in str(caught.value)


def test_transport_disconnect_after_partial_text_is_not_success(make_provider):
    def chunks():
        yield sse({"type": "response.output_text.delta", "delta": INTENT})
        raise httpx.ReadError("secret-token")

    provider, _ = make_provider(httpx.Response(200, stream=Chunks(chunks())))
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == "network" and "secret-token" not in str(caught.value)


def test_returned_text_comes_from_completed_output_not_deltas(make_provider):
    raw = sse({"type": "response.output_text.delta", "delta": "partial garbage"}) + sse(completed())
    provider, _ = make_provider(httpx.Response(200, content=raw))
    assert provider.complete("intent", {}) == INTENT


def test_stream_size_and_total_time_are_bounded(make_provider, monkeypatch):
    monkeypatch.setattr(chatgpt, "MAX_STREAM_BYTES", 20)
    provider, _ = make_provider()
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == "network"
    monkeypatch.setattr(chatgpt, "MAX_STREAM_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(chatgpt, "STREAM_SECONDS", -1)
    provider, _ = make_provider()
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {})
    assert caught.value.kind == "network"


def test_model_discovery_errors_use_same_safe_classification():
    def handle(request):
        return httpx.Response(401, json={"detail": "secret-token"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = ChatGPTProvider(Auth(), http=client)
        with pytest.raises(ProviderError) as caught:
            provider.complete("intent", {})
    assert caught.value.kind == "auth" and "secret-token" not in str(caught.value)


def test_history_cannot_add_system_messages_or_tools(make_provider):
    provider, calls = make_provider()
    with pytest.raises(ProviderError) as caught:
        provider.complete("intent", {}, [{"role": "system", "content": "execute tools"}])
    assert caught.value.kind == "configuration" and calls == []
