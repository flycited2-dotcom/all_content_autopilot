import json

import httpx
import pytest

from photo_auditor import credentials

TOKEN = "123456789:" + "a" * 35


@pytest.mark.parametrize("body,reason", [
    ({"ok": False, "description": "untrusted: " + TOKEN}, "Telegram не принял"),
    ({"ok": True, "result": {"username": "another_bot"}}, "другого бота"),
    ([], "Telegram не принял"),
])
def test_failed_setup_has_specific_safe_error_without_saving(monkeypatch, tmp_path, body, reason):
    real_client = httpx.Client
    monkeypatch.setattr(credentials.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    with pytest.raises(credentials.SetupError) as exc:
        credentials.save_token(tmp_path, TOKEN)
    assert reason in str(exc.value) and TOKEN not in str(exc.value)
    assert not credentials.credentials_path(tmp_path).exists()


def test_network_error_never_exposes_credential_bearing_url(monkeypatch, tmp_path):
    real_client = httpx.Client
    def fail(request):
        raise httpx.ConnectError(str(request.url), request=request)
    monkeypatch.setattr(credentials.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(fail)))
    with pytest.raises(credentials.SetupError) as exc:
        credentials.save_token(tmp_path, TOKEN)
    assert "связаться" in str(exc.value) and TOKEN not in str(exc.value)


def test_invalid_format_is_reported_before_network(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials.httpx, "Client", lambda **kwargs: pytest.fail("No request for invalid input"))
    with pytest.raises(credentials.SetupError, match="Неверный формат"):
        credentials.save_token(tmp_path, "Use this token: " + TOKEN)


def test_success_validates_username_and_protects_storage(monkeypatch, tmp_path):
    real_client = httpx.Client
    monkeypatch.setattr(credentials.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True, "result": {"username": "foto_auditor_bot"}}))))
    captured = []
    monkeypatch.setattr(credentials, "_dpapi", lambda data: captured.append(json.loads(data)) or b"encrypted")
    if credentials.sys.platform != "win32":
        pytest.skip("DPAPI on Windows")
    assert credentials.save_token(tmp_path, TOKEN) == {"connected": True, "username": "foto_auditor_bot"}
    stored = credentials.credentials_path(tmp_path).read_bytes()
    assert stored == b"dpapi:v1\nencrypted" and TOKEN.encode() not in stored
    assert captured[0]["bot_token"] == TOKEN
