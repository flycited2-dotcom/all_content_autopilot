"""The owner can send a supplier image without losing it at the text-only queue."""
import base64
import sqlite3

import httpx
import pytest

from orchestrator_bot import bot
from orchestrator_bot.bot import Deps
from orchestrator_bot.store import Store
from orchestrator_bot.telegram_api import IMAGE_MAX_BYTES, TelegramApi, image_mime

OWNER, NOW = 1264067528, 1_800_000_000.0
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jS3sAAAAASUVORK5CYII=")


class Telegram:
    def __init__(self, store):
        self.store, self.sent, self.downloaded = store, [], []
        self.data = PNG

    def download_file(self, file_id, max_bytes):
        self.downloaded.append((file_id, max_bytes))
        return self.data

    def send(self, chat_id, text, *args, **kwargs):
        # Acceptance must follow the durable write, including after restart.
        if "принята" in text:
            assert self.store.has_tg_message(5)
        self.sent.append(text)
        return 100


@pytest.fixture
def deps(tmp_path):
    store = Store(tmp_path / "orch.db")
    store.heartbeat(NOW)
    return Deps(store, Telegram(store), None, None, OWNER, clock=lambda: NOW)


def message(**fields):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": OWNER},
                                        "from": {"id": OWNER}, **fields}}


def test_captionless_photo_uses_largest_image_and_survives_restart(deps):
    update = message(photo=[{"file_id": "small", "width": 20, "height": 30},
                            {"file_id": "large", "file_unique_id": "stable", "width": 100, "height": 80}])
    bot.handle_update(update, deps)
    bot.handle_update(update, deps)
    assert deps.tg.downloaded == [("large", IMAGE_MAX_BYTES)]
    assert len(deps.tg.sent) == 1
    queued = Store(deps.store.path).claim_next(NOW)
    assert queued["kind"] == "photo" and queued["text"] == "Фотография поставщика"
    assert queued["attachments"] == [{"mime_type": "image/png", "data": base64.b64encode(PNG).decode(),
                                       "file_unique_id": "stable"}]
    assert "bot" not in queued["attachments"][0]["data"]


def test_photo_keeps_caption_and_quoted_context_without_consuming_pending_count(deps):
    deps.store.set_meta("selection_await_count", "some waiting selection")
    bot.handle_update(message(photo=[{"file_id": "photo"}], caption="CARVER +15%",
                              reply_to_message={"caption": "Оранжевые цены, синие остатки"}), deps)
    queued = deps.store.claim_next(NOW)
    assert "CARVER +15%" in queued["text"] and "Оранжевые цены" in queued["text"]
    assert deps.store.get_meta("selection_await_count") == "some waiting selection"


def test_image_sent_as_document_uses_actual_format_and_preserves_name(deps):
    bot.handle_update(message(document={"file_id": "doc", "file_name": "прайс.png",
                                        "mime_type": "application/octet-stream"}, caption="В генерацию"), deps)
    queued = deps.store.claim_next(NOW)
    assert queued["text"] == "В генерацию"
    assert queued["attachments"][0]["file_name"] == "прайс.png"
    assert queued["attachments"][0]["mime_type"] == "image/png"


def test_photo_offline_is_saved_before_offline_notice(deps):
    deps.store.heartbeat(NOW - 1000)
    bot.handle_update(message(photo=[{"file_id": "photo"}]), deps)
    assert deps.store.count_new() == 1 and deps.tg.sent == [bot.OFFLINE_NOTICE]


@pytest.mark.parametrize("fields,data,expected", [
    ({"photo": [{"file_id": "photo", "file_size": IMAGE_MAX_BYTES + 1}]}, PNG, "10 МБ"),
    ({"photo": [{"file_id": "photo"}]}, b"this is not an image", "JPEG"),
    ({"document": {"file_id": "doc", "file_name": "price.pdf"}}, PNG, "JPEG"),
])
def test_invalid_images_get_explanation_and_never_enter_queue(deps, fields, data, expected):
    deps.tg.data = data
    bot.handle_update(message(**fields), deps)
    assert deps.store.count_new() == 0
    assert expected in deps.tg.sent[-1]


def test_stranger_photo_is_not_downloaded(deps):
    update = message(photo=[{"file_id": "photo"}])
    update["message"]["from"]["id"] = 999
    bot.handle_update(update, deps)
    assert deps.tg.downloaded == [] and deps.store.count_new() == 0


def test_existing_database_migrates_without_changing_text_messages(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, tg_message_id INTEGER, kind TEXT, text TEXT, "
                   "status TEXT NOT NULL DEFAULT 'new', created REAL, claimed_at REAL, "
                   "intent_done INTEGER NOT NULL DEFAULT 0, response TEXT)")
        db.execute("INSERT INTO messages(tg_message_id,kind,text,created) VALUES(1,'text','старое',100)")
    store = Store(path)
    assert store.claim_next(200)["text"] == "старое"
    store.add_message(2, "photo", "новое", now=201, attachments=[{"mime_type": "image/png", "data": "a"}])
    assert store.claim_next(202)["attachments"][0]["data"] == "a"


def test_recent_photos_are_bounded_chronological_and_bound_to_source_message(deps):
    attachment = {"mime_type": "image/png", "data": "image"}
    deps.store.add_message(1, "photo", "too old", now=NOW - 1801, attachments=[attachment])
    recent_ids = [deps.store.add_message(n, "photo", "photo", now=NOW - n, attachments=[attachment])
                  for n in (2, 3, 4, 5)]
    current = deps.store.add_message(6, "text", "Теперь в генерацию +15%", now=NOW)
    deps.store.add_message(7, "photo", "later", now=NOW + 1, attachments=[attachment])
    assert deps.store.get_message(current)["text"] == "Теперь в генерацию +15%"
    assert "attachments" not in deps.store.get_message(current)
    assert deps.store.get_message(999) is None
    found = deps.store.recent_photo_attachments(current)
    assert [item["source_message_id"] for item in found] == recent_ids[-3:]
    assert all(item["data"] == "image" for item in found)
    assert deps.store.recent_photo_attachments(999) == []


def test_album_collects_across_polls_restart_and_part_redelivery(deps):
    first = message(photo=[{"file_id": "first"}], media_group_id="album-1", caption="CARVER +15%")
    bot.handle_update(first, deps)
    assert deps.store.count_new() == 1
    assert deps.store.claim_next(NOW + 1) is None
    deps.clock = lambda: NOW + 1.5
    second = message(message_id=6, photo=[{"file_id": "second"}], media_group_id="album-1")
    bot.handle_update(second, deps)
    deps.store = Store(deps.store.path)
    bot.handle_update(second, deps)
    assert deps.tg.downloaded == [("first", IMAGE_MAX_BYTES), ("second", IMAGE_MAX_BYTES)]
    assert len(deps.tg.sent) == 1
    assert deps.store.has_tg_message(6)
    assert deps.store.claim_next(NOW + 3.4) is None
    queued = deps.store.claim_next(NOW + 3.5)
    assert len(queued["attachments"]) == 2
    assert queued["media_group_id"] == "album-1"
    assert queued["text"] == "CARVER +15%"
    assert deps.store.get_message(queued["id"]) == queued
    assert deps.store.claim_next(NOW + 4) is None


def test_album_caption_can_arrive_on_later_part_and_following_text_waits(deps):
    old = deps.store.add_message(1, "voice", "Входящие цены оранжевые", now=NOW - 5)
    deps.store.save_response(old, {"reply": "Присылайте фотографии"})
    deps.store.mark_done(old)
    bot.handle_update(message(photo=[{"file_id": "first"}], media_group_id="album"), deps)
    deps.clock = lambda: NOW + 0.5
    bot.handle_update(message(message_id=6, photo=[{"file_id": "second"}], media_group_id="album",
                              caption="В наличии, CARVER +15%"), deps)
    following = deps.store.add_message(7, "text", "Их в генерацию", now=NOW + 0.6)
    assert deps.store.claim_next(NOW + 1) is None
    album = deps.store.claim_next(NOW + 2.5)
    assert album["text"] == "В наличии, CARVER +15%"
    assert len(album["attachments"]) == 2
    assert deps.store.dialogue_before(album["id"])[0]["content"] == "Входящие цены оранжевые"
    assert deps.store.claim_next(NOW + 2.6)["id"] == following


def test_late_album_part_does_not_mutate_a_claimed_payload(deps):
    bot.handle_update(message(photo=[{"file_id": "first"}], media_group_id="album"), deps)
    claimed = deps.store.claim_next(NOW + 3)
    deps.clock = lambda: NOW + 4
    bot.handle_update(message(message_id=6, photo=[{"file_id": "late"}], media_group_id="album"), deps)
    assert deps.store.get_message(claimed["id"]) == claimed
    second = deps.store.claim_next(NOW + 6)
    assert second["id"] != claimed["id"]
    assert len(second["attachments"]) == 1
    assert deps.store.count_new() == 2


def test_album_image_limit_does_not_change_the_durable_accepted_prefix(deps):
    for index in range(8):
        bot.handle_update(message(message_id=5 + index, photo=[{"file_id": str(index)}],
                                  media_group_id="album"), deps)
    bot.handle_update(message(message_id=13, photo=[{"file_id": "excess"}], media_group_id="album"), deps)
    assert "максимум 8" in deps.tg.sent[-1]
    assert not deps.store.has_tg_message(13)
    queued = deps.store.claim_next(NOW + 3)
    assert len(queued["attachments"]) == 8


def test_album_byte_limit_rejects_before_mutating_and_remains_retryable(deps, monkeypatch):
    import orchestrator_bot.store as storage
    monkeypatch.setattr(storage, "ALBUM_MAX_BYTES", len(PNG) + 1)
    bot.handle_update(message(photo=[{"file_id": "first"}], media_group_id="album"), deps)
    bot.handle_update(message(message_id=6, photo=[{"file_id": "second"}], media_group_id="album"), deps)
    assert "20 МБ" in deps.tg.sent[-1]
    assert not deps.store.has_tg_message(6)
    queued = deps.store.claim_next(NOW + 3)
    assert len(queued["attachments"]) == 1


def test_download_file_bounds_headers_and_actual_body():
    for size_header in (None, "1000"):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/photo.png"}})
            return httpx.Response(200, content=PNG, headers={"Content-Length": size_header} if size_header else {})

        api = TelegramApi("FAKE", httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(ValueError, match="10 МБ"):
            api.download_file("id", max_bytes=10)


def test_download_file_rejects_oversized_metadata_before_get():
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json={"ok": True, "result": {"file_path": "photo.png", "file_size": 11}})

    api = TelegramApi("FAKE", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError):
        api.download_file("id", max_bytes=10)
    assert requests == ["POST"]


def test_download_file_and_magic_format_detection():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photo.png"}})
        return httpx.Response(200, content=PNG)

    api = TelegramApi("FAKE", httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.download_file("id") == PNG
    assert image_mime(PNG) == "image/png"
    assert image_mime(b"\xff\xd8\xff\xe0hello") == "image/jpeg"
    assert image_mime(b"RIFFxxxxWEBP") == "image/webp"
