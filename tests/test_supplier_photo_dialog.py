import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from listener import attachments, brain
from listener.chatgpt import ChatGPTProvider
from listener.provider_errors import ProviderError


def photo(color="orange", **extra):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, format="PNG")
    return {"mime_type": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii"), **extra}


def payload(*photos):
    return {"message": {"id": 12, "kind": "photo", "text": "CARVER в генерацию, +15%",
                        "attachments": list(photos)}, "context": {"capabilities": ["supplier_photo_import"]}}


def test_chatgpt_sends_real_images_and_only_metadata_in_text(monkeypatch):
    first, second = photo(source_message_id=4), photo("blue")
    original = payload(first, second)
    captured = {}
    provider = ChatGPTProvider(SimpleNamespace(), http=object())

    def respond(instructions, inputs):
        captured.update(instructions=instructions, inputs=inputs)
        return {"text": '{"reply":"Принял","actions":[]}'}

    monkeypatch.setattr(provider, "_respond", respond)
    provider.complete("Read current supplier photos", original)
    content = captured["inputs"][-1]["content"]
    image_parts = [part for part in content if part["type"] == "input_image"]
    assert [part["image_url"] for part in image_parts] == [
        "data:image/png;base64," + image["data"] for image in (first, second)]
    assert all(part["detail"] == "high" for part in image_parts)
    metadata = json.loads(content[0]["text"])
    assert [item["attachment_index"] for item in metadata["message"]["attachments"]] == [0, 1]
    assert metadata["message"]["attachments"][0]["source_message_id"] == 4
    assert all("data" not in item for item in metadata["message"]["attachments"])
    assert "path" not in metadata["message"]["attachments"][0]
    assert original["message"]["attachments"][0] == first  # Caller/pending state remains intact.


def test_claude_materializes_all_images_and_json_references_verified_files(tmp_path):
    incoming = payload(photo(file_name="../../evil.png"), photo("blue"))
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["timeout"] == 60  # Photo vision yields promptly to the reserve.
        prepared = json.loads((tmp_path / "runs/chat/12.json").read_text(encoding="utf-8"))
        for raw, item in zip(incoming["message"]["attachments"], prepared["message"]["attachments"]):
            path = tmp_path / item["path"]
            assert path.resolve().is_relative_to(tmp_path.resolve())
            assert path.read_bytes() == base64.b64decode(raw["data"])
            assert "data" not in item
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": '{"reply":"Принял","actions":[]}'}), stderr="")

    assert brain.complete_turn(incoming, ("session", True), runner=run, root=tmp_path)["reply"] == "Принял"
    assert "/chat-turn runs/chat/12.json" in calls[0]
    first_files = sorted((tmp_path / "runs/chat/attachments").glob("*.png"))
    brain.complete_turn(incoming, ("session", False), runner=run, root=tmp_path)
    assert sorted((tmp_path / "runs/chat/attachments").glob("*.png")) == first_files
    assert len(first_files) == 2


@pytest.mark.parametrize("bad", [
    {"mime_type": "image/png", "data": "https://example.com/photo.png"},
    {"mime_type": "image/png", "path": "C:/private/photo.png"},
    {"mime_type": "image/png", "data": base64.b64encode(b"not a photo").decode()},
    {"mime_type": "image/jpeg", "data": photo()["data"]},
    {"mime_type": "text/html", "data": photo()["data"]},
])
def test_invalid_or_external_images_never_reach_provider(tmp_path, monkeypatch, bad):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid image reached provider")

    provider = ChatGPTProvider(SimpleNamespace(), http=object())
    monkeypatch.setattr(provider, "_respond", forbidden)
    with pytest.raises(ProviderError):
        provider.complete("Read photo", payload(bad))
    with pytest.raises(ProviderError):
        brain.complete_turn(payload(bad), ("session", True), runner=forbidden, root=tmp_path)
    assert not (tmp_path / "runs/chat/12.json").exists()


def test_image_limits_apply_before_provider_call(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 8)
    with pytest.raises(ValueError):
        attachments.decode_images(payload(photo()))
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 1024 * 1024)
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 100)
    with pytest.raises(ValueError):
        attachments.decode_images(payload(photo(), photo("blue")))
    with pytest.raises(ValueError):
        attachments.decode_images(payload(*[photo()] * (attachments.MAX_IMAGES + 1)))


def test_image_cache_cannot_follow_link_outside_project(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    directory = tmp_path / "runs/chat/attachments"
    directory.parent.mkdir(parents=True)
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    with pytest.raises(ValueError):
        attachments.materialize_images(payload(photo()), tmp_path)
    assert not list(outside.iterdir())


def test_text_dialogue_keeps_legacy_string_content(monkeypatch):
    provider = ChatGPTProvider(SimpleNamespace(), http=object())
    captured = []
    monkeypatch.setattr(provider, "_respond", lambda instructions, inputs: captured.extend(inputs) or {"text": "ok"})
    original = {"message": {"id": 5, "text": "статус"}, "context": {}}
    provider.complete("Text only", original)
    assert isinstance(captured[-1]["content"], str)
    assert json.loads(captured[-1]["content"]) == original


def test_corrupt_image_is_completed_without_blocking_providers_or_next_message(tmp_path, caplog):
    from listener.__main__ import process_once
    from listener.pending import PendingTurn
    from listener.routing import ModelRouter

    damaged = {"mime_type": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nprivate-bad-data").decode()}
    bad_payload = payload(damaged)
    next_payload = {"message": {"id": 13, "kind": "text", "text": "статус"}, "context": {}}
    messages = [bad_payload, next_payload]
    calls, responses, notices = [], [], []

    def primary(value, session):
        calls.append(value)
        return {"reply": "Всё работает", "actions": []}

    class Api:
        def next(self):
            return messages.pop(0) if messages else {"message": None}

        def intent(self, message_id, value):
            responses.append((message_id, value))
            return {"editorial": []}

    backup = SimpleNamespace(ready=lambda: pytest.fail("Corrupt input touched backup provider"))
    router = ModelRouter(tmp_path, notify=notices.append, claude=primary, chatgpt=backup)
    pending = PendingTurn(tmp_path / "pending-turn.json")
    worker = SimpleNamespace(submit=lambda action: pytest.fail("Corrupt input executed an action"))
    assert process_once(Api(), worker, router.run_turn, ("s", True), pending)
    assert pending.load() is None
    assert not calls and not notices
    assert router.state["providers"] == {}
    assert responses[0][1]["actions"] == []
    assert "Пришлите заново" in responses[0][1]["reply"]
    assert "private-bad-data" not in caplog.text and damaged["data"] not in caplog.text
    assert process_once(Api(), worker, router.run_turn, ("s", False), pending)
    assert pending.load() is None and len(calls) == 1
    assert responses[1] == (13, {"reply": "Всё работает", "actions": []})
    assert router.state["providers"] == {} and router.state["active"] == "claude"
    assert "Пришлите заново" in calls[0]["context"]["history"][1]["content"]


def test_too_many_images_is_not_a_provider_outage(tmp_path):
    from listener.routing import ModelRouter

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid image list reached provider")

    router = ModelRouter(tmp_path, claude=forbidden, chatgpt=SimpleNamespace(ready=forbidden))
    reply = router.run_turn(payload(*[photo()] * (attachments.MAX_IMAGES + 1)), ("s", True))
    assert reply["actions"] == [] and "суммарно" in reply["reply"]
    assert router.state["providers"] == {} and "active" not in router.state


def test_invalid_optional_recent_photos_do_not_interrupt_unrelated_text(tmp_path):
    from listener.routing import ModelRouter

    incoming = payload({"mime_type": "image/png", "data": "invalid"})
    incoming["message"].update(kind="text", text="Как дела?", attachment_context="recent_photos")
    calls = []

    def primary(value, session):
        calls.append(value)
        return {"reply": "ПК на связи", "actions": []}

    router = ModelRouter(tmp_path, claude=primary, chatgpt=SimpleNamespace())
    assert router.run_turn(incoming, ("s", False)) == {"reply": "ПК на связи", "actions": []}
    assert len(calls) == 1 and "attachments" not in calls[0]["message"]
    assert "attachment_context" not in calls[0]["message"]
    assert "только если текущая задача относится к ним" in calls[0]["context"]["photo_error"]
    assert incoming["message"]["attachments"][0]["data"] == "invalid"
    assert router.state["providers"] == {} and router.state["active"] == "claude"
