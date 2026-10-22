"""OAuth tests use generated identities and mock HTTP only, never existing credentials."""
import base64
import hashlib
import io
import json
import os
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from listener import chatgpt_auth as module
from listener.chatgpt_auth import ChatGPTAuth, DIRECT_SCOPE, INITIAL_CLIENT, ISSUER, RESOURCE
from listener.provider_errors import ProviderError

REAL_DPAPI = module._dpapi

NOW = 1_790_000_000
CLIENT = "oaiapp_test_registration"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_windows_dpapi_roundtrip_with_synthetic_bytes():
    original = b"synthetic noncredential auth storage check"
    protected = REAL_DPAPI(original)
    assert protected != original and original not in protected
    assert REAL_DPAPI(protected, decrypt=True) == original


@pytest.fixture
def keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    return key, jwk


@pytest.fixture(autouse=True)
def fake_dpapi(monkeypatch):
    # Real credentials and Windows user keys are never used in these tests.
    def crypt(data, decrypt=False):
        if decrypt:
            assert data.startswith(b"mock-protected:")
            return bytes(value ^ 0xA5 for value in data[15:])
        return b"mock-protected:" + bytes(value ^ 0xA5 for value in data)
    monkeypatch.setattr(module, "_dpapi", crypt)


class Service:
    def __init__(self, keys):
        self.key, self.jwk = keys
        self.calls = []
        self.response = None
        self.response_code = 200
        self.metadata = {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/api/accounts/authorize",
                         "token_endpoint": ISSUER + "/api/accounts/oauth/token", "jwks_uri": ISSUER + "/jwks"}

    def handle(self, request):
        self.calls.append(request)
        if str(request.url) == module.DISCOVERY_URL:
            return httpx.Response(200, json=self.metadata)
        if str(request.url) == self.metadata["jwks_uri"]:
            return httpx.Response(200, json={"keys": [self.jwk]})
        if str(request.url) == self.metadata["token_endpoint"]:
            return httpx.Response(self.response_code, json=self.response, headers={"Retry-After": "90"})
        raise AssertionError("unexpected endpoint")

    def tokens(self, nonce, subject="test-subject", audience=CLIENT, **overrides):
        claims = {"iss": ISSUER, "aud": audience, "sub": subject, "nonce": nonce,
                  "iat": NOW, "exp": NOW + 3600, "email": "test@example.invalid"}
        claims.update(overrides.pop("claims", {}))
        result = {"access_token": "test-access-secret", "refresh_token": "test-refresh-secret",
                  "id_token": jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"}),
                  "token_type": "Bearer", "expires_in": 3600, "scope": module.SCOPES}
        result.update(overrides)
        return result


@pytest.fixture
def setup(tmp_path, keys):
    service = Service(keys)
    http = httpx.Client(transport=httpx.MockTransport(service.handle))
    auth = ChatGPTAuth(root=tmp_path, http=http, clock=lambda: NOW)
    return auth, service


def sign_in(auth, service):
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    service.response = service.tokens(attempt.nonce)
    auth._complete_login({"state": [attempt.state], "code": ["test-code"], "client_id": [CLIENT]}, attempt)
    return attempt


def test_pkce_host_and_initial_registration(setup):
    auth, service = setup
    attempt, url = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    params = parse_qs(urlsplit(url).query)
    assert params["client_id"] == [INITIAL_CLIENT]
    assert params["agent_name_hint"] == ["Agents MY Orchestrator"]
    assert params["resource"] == [RESOURCE]
    assert DIRECT_SCOPE in params["scope"][0].split()
    digest = hashlib.sha256(attempt.verifier.encode()).digest()
    assert params["code_challenge"] == [base64.urlsafe_b64encode(digest).decode().rstrip("=")]
    assert params["code_challenge_method"] == ["S256"]
    another, _ = auth._begin_login(attempt.redirect_uri)
    assert another.host_id == attempt.host_id and another.host_id.startswith("urn:uuid:")
    assert another.state != attempt.state and another.nonce != attempt.nonce
    assert another.verifier != attempt.verifier
    assert not auth.ready()


def test_callback_exchange_and_safe_status(setup):
    auth, service = setup
    attempt = sign_in(auth, service)
    posted = next(request for request in service.calls if request.method == "POST")
    fields = parse_qs(posted.content.decode())
    assert fields["client_id"] == [CLIENT] and fields["redirect_uri"] == [attempt.redirect_uri]
    assert fields["code_verifier"] == [attempt.verifier] and "client_secret" not in fields
    assert auth.access_token() == "test-access-secret"
    status = auth.status()
    assert status["ready"] and status["account"] == "test@example.invalid"
    encoded = json.dumps(status)
    assert "test-access-secret" not in encoded and "test-refresh-secret" not in encoded
    assert "id_token" not in encoded
    assert b"test-access-secret" not in auth.path.read_bytes() or not module._windows()


@pytest.mark.parametrize("query", [
    {"state": ["wrong"], "error": ["access_denied"], "client_id": ["evil"]},
    {"state": ["wrong"]},
    {"code": ["test-code"]},
])
def test_invalid_state_rejected_before_oauth_error_or_registration(setup, query):
    auth, service = setup
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    with pytest.raises(ProviderError, match="callback"):
        auth._complete_login(query, attempt)
    assert not auth.path.exists()
    assert not any(request.method == "POST" for request in service.calls)


def test_valid_state_denied_does_not_exchange(setup):
    auth, service = setup
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    with pytest.raises(ProviderError):
        auth._complete_login({"state": [attempt.state], "error": ["access_denied"]}, attempt)
    assert not auth.path.exists()
    assert not any(request.method == "POST" for request in service.calls)


@pytest.mark.parametrize("client", [None, INITIAL_CLIENT, ""])
def test_first_callback_requires_issued_client(setup, client):
    auth, service = setup
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    query = {"state": [attempt.state], "code": ["test-code"]}
    if client is not None:
        query["client_id"] = [client]
    with pytest.raises(ProviderError):
        auth._complete_login(query, attempt)
    assert not any(request.method == "POST" for request in service.calls)


def test_failed_exchange_keeps_issued_client_for_next_sign_in(setup):
    auth, service = setup
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    service.response_code, service.response = 400, {"error": "invalid_grant", "error_description": "secret-code"}
    with pytest.raises(ProviderError) as exc:
        auth._complete_login({"state": [attempt.state], "code": ["test-code"], "client_id": [CLIENT]}, attempt)
    assert "secret-code" not in str(exc.value)
    assert auth._load()["client_id"] == CLIENT
    assert "code" not in auth._load()
    again, url = auth._begin_login("http://127.0.0.1:45678/auth/callback")
    assert again.client_id == CLIENT and again.host_id == attempt.host_id
    assert "agent_name_hint" not in parse_qs(urlsplit(url).query)


@pytest.mark.parametrize("change", [
    {"subject": "another-subject"}, {"audience": "other-client"},
    {"claims": {"nonce": "wrong"}}, {"claims": {"iss": "https://evil.invalid"}},
    {"claims": {"exp": NOW - 1}},
])
def test_reauthorization_validates_identity_audience_nonce_issuer_expiry(setup, change):
    auth, service = setup
    sign_in(auth, service)
    original = auth.path.read_bytes()
    attempt, url = auth._begin_login("http://127.0.0.1:45678/auth/callback")
    params = parse_qs(urlsplit(url).query)
    assert "id_token_hint" in params and "agent_name_hint" not in params
    service.response = service.tokens(attempt.nonce, **change)
    with pytest.raises(ProviderError):
        auth._complete_login({"state": [attempt.state], "code": ["fresh-code"]}, attempt)
    assert auth.path.read_bytes() == original


def test_reauthorization_rejects_changed_callback_client(setup):
    auth, service = setup
    sign_in(auth, service)
    previous = len(service.calls)
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    with pytest.raises(ProviderError):
        auth._complete_login({"state": [attempt.state], "code": ["code"], "client_id": ["other"]}, attempt)
    assert len(service.calls) == previous


def test_missing_direct_grant_never_enables_fallback(setup):
    auth, service = setup
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    service.response = service.tokens(attempt.nonce, scope="openid profile email")
    with pytest.raises(ProviderError, match="план ChatGPT"):
        auth._complete_login({"state": [attempt.state], "code": ["code"], "client_id": [CLIENT]}, attempt)
    assert not auth.ready() and "access_token" not in auth._load()


def test_id_signature_must_verify(setup, keys):
    auth, service = setup
    attempt, _ = auth._begin_login("http://127.0.0.1:12345/auth/callback")
    service.response = service.tokens(attempt.nonce)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {"iss": ISSUER, "aud": CLIENT, "sub": "test-subject", "nonce": attempt.nonce,
              "iat": NOW, "exp": NOW + 3600}
    service.response["id_token"] = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(ProviderError):
        auth._complete_login({"state": [attempt.state], "code": ["code"], "client_id": [CLIENT]}, attempt)
    assert not auth.ready()


def test_refresh_rotates_pair_atomically_and_uses_issued_client(setup):
    auth, service = setup
    sign_in(auth, service)
    auth.clock = lambda: NOW + 3550
    service.response = service.tokens("unused", access_token="replacement-access", refresh_token="replacement-refresh")
    service.response.pop("id_token")  # ID token is optional on refresh.
    service.response.pop("scope")     # Omitting scope retains the original grant.
    assert auth.access_token() == "replacement-access"
    record = auth._load()
    assert record["refresh_token"] == "replacement-refresh" and record["expires_at"] == NOW + 7150
    posted = [request for request in service.calls if request.method == "POST"][-1]
    assert parse_qs(posted.content.decode()) == {
        "grant_type": ["refresh_token"], "client_id": [CLIENT],
        "refresh_token": ["test-refresh-secret"], "resource": [RESOURCE]}


@pytest.mark.parametrize("code,error,kind", [(503, "server_error", "network"), (429, "slow_down", "rate_limit")])
def test_transient_refresh_preserves_credentials_and_redacts_response(setup, code, error, kind):
    auth, service = setup
    sign_in(auth, service)
    original = auth.path.read_bytes()
    auth.clock = lambda: NOW + 3601
    service.response_code = code
    service.response = {"error": error, "error_description": "test-refresh-secret Bearer sensitive"}
    with pytest.raises(ProviderError) as exc:
        auth.access_token()
    assert exc.value.kind == kind
    assert "test-refresh-secret" not in str(exc.value) and "sensitive" not in str(exc.value)
    assert auth.path.read_bytes() == original and auth.ready()
    if kind == "rate_limit":
        assert exc.value.retry_at == NOW + 3691


@pytest.mark.parametrize("error", [
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
])
def test_terminal_refresh_erases_tokens_but_keeps_selected_identity(setup, error):
    auth, service = setup
    sign_in(auth, service)
    auth.clock = lambda: NOW + 3601
    service.response_code, service.response = 400, {"error": error, "error_description": "test-refresh-secret"}
    with pytest.raises(ProviderError) as exc:
        auth.access_token()
    assert exc.value.kind == "auth" and error in str(exc.value)
    assert "test-refresh-secret" not in str(exc.value)
    record = auth._load()
    assert record["client_id"] == CLIENT and record["subject"] == "test-subject"
    assert not any(name.endswith("token") for name in record)
    assert not auth.ready()


def test_discovery_rejects_foreign_endpoint_before_sending_credentials(setup):
    auth, service = setup
    service.metadata["token_endpoint"] = "https://attacker.invalid/token"
    with pytest.raises(ProviderError) as exc:
        auth._begin_login("http://127.0.0.1:12345/auth/callback")
    assert exc.value.kind == "configuration"
    assert all(request.url.host == "auth.openai.com" for request in service.calls)


@pytest.mark.skipif(os.name == "nt", reason="Unix mode bits are not available on Windows")
def test_unix_credentials_are_owner_only_and_atomic(setup, monkeypatch):
    auth, service = setup
    sign_in(auth, service)
    assert auth.path.stat().st_mode & 0o777 == 0o600
    assert auth.path.parent.stat().st_mode & 0o777 == 0o700
    before = auth.path.read_bytes()
    monkeypatch.setattr(module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("test failure")))
    with pytest.raises(OSError):
        auth._save({"access_token": "do-not-install"})
    assert auth.path.read_bytes() == before
    assert not list(auth.path.parent.glob(".chatgpt-*"))


def test_system_browser_callback_uses_bound_loopback_and_no_model_calls(setup, monkeypatch):
    auth, service = setup
    opened = []
    responses = []

    class FakeServer:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            self.handler, self.server_port = handler, 61234

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            params = parse_qs(urlsplit(opened[0]).query)
            request = object.__new__(self.handler)
            request.path = "/auth/callback?" + module.urlencode({
                "code": "browser-code", "state": params["state"][0], "client_id": CLIENT})
            request.send_response = responses.append
            request.send_header = lambda *args: None
            request.end_headers = lambda: None
            request.wfile = io.BytesIO()
            request.do_GET()

    monkeypatch.setattr(module, "HTTPServer", FakeServer)

    def opener(url):
        opened.append(url)
        params = parse_qs(urlsplit(url).query)
        callback = params["redirect_uri"][0]
        assert urlsplit(callback).hostname == "127.0.0.1"
        assert urlsplit(callback).path == "/auth/callback"
        service.response = service.tokens(params["nonce"][0])
        return True

    assert auth.login(timeout=5, opener=opener)["ready"]
    assert responses == [200]
    assert all(request.url.host == "auth.openai.com" for request in service.calls)


def test_refresh_serializes_two_auth_instances(setup):
    auth, service = setup
    sign_in(auth, service)
    auth.clock = lambda: NOW + 3550
    another = ChatGPTAuth(auth.root, http=auth.http, clock=auth.clock)
    service.response = service.tokens("unused", access_token="new-access", refresh_token="new-refresh")
    service.response.pop("id_token")
    results, failures = [], []

    def refresh(instance):
        try:
            results.append(instance.access_token())
        except Exception as exc:
            failures.append(type(exc).__name__)

    threads = [threading.Thread(target=refresh, args=(instance,)) for instance in (auth, another)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not failures and results == ["new-access", "new-access"]
    posts = [request for request in service.calls if request.method == "POST"]
    assert len(posts) == 2  # Initial login and exactly one refresh.


def test_corrupt_storage_is_not_returned_in_status(setup):
    auth, _ = setup
    auth.path.parent.mkdir(parents=True)
    auth.path.write_bytes(b"test-access-secret")
    status = auth.status()
    assert not status["ready"]
    assert "test-access-secret" not in json.dumps(status)


def test_retained_host_is_recovered_after_public_metadata_loss(setup):
    auth, service = setup
    original = sign_in(auth, service)
    auth.host_path.unlink()
    attempt, _ = auth._begin_login("http://127.0.0.1:54321/auth/callback")
    assert attempt.host_id == original.host_id


def test_unexpected_credential_types_fail_closed(setup):
    auth, _ = setup
    auth._save({"expires_at": "test-access-secret", "scopes": 42})
    assert not auth.ready()
    assert "test-access-secret" not in json.dumps(auth.status())


def test_failed_atomic_replace_keeps_previous_protected_record(setup, monkeypatch):
    auth, service = setup
    sign_in(auth, service)
    original = auth.path.read_bytes()

    def fail(*args):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        auth._save({"access_token": "uncommitted-replacement"})
    assert auth.path.read_bytes() == original
    assert not list(auth.path.parent.glob(".chatgpt-*"))


def test_cli_reports_sanitized_provider_reason_and_kind(monkeypatch, capsys):
    class FakeAuth:
        def login(self):
            raise ProviderError("chatgpt", "auth", "Вход ChatGPT отменён или разрешение не предоставлено")

    monkeypatch.setattr(module, "ChatGPTAuth", lambda **kwargs: FakeAuth())
    assert module.main(["login"]) == 1
    result = capsys.readouterr()
    assert "[auth]" in result.err and "разрешение не предоставлено" in result.err
    assert not result.out


def test_cli_does_not_echo_unsanitized_os_errors(monkeypatch, capsys):
    class FakeAuth:
        def login(self):
            raise OSError("sensitive local detail")

    monkeypatch.setattr(module, "ChatGPTAuth", lambda **kwargs: FakeAuth())
    assert module.main(["login"]) == 1
    result = capsys.readouterr()
    assert "[configuration]" in result.err and "sensitive local detail" not in result.err
