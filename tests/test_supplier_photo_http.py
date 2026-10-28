import json
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from orchestrator_bot.api import make_server
from orchestrator_bot.store import Store
from orchestrator_bot.supplier_import import prepare_items

NOW = 1_800_000_000

@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path / "orchestrator.db")
    calls, sent = [], []

    def enqueue(action, source_id, attachments):
        calls.append((action, source_id, attachments))
        rows = prepare_items(action, source_id)
        return {"accepted": [r["key"] for r in rows], "items": rows, "manifest_path": source_id}

    factory = SimpleNamespace(hint=lambda: {}, workers=lambda: {"generation": True, "agent_online": True},
                              resolve=lambda _: pytest.fail("Catalog called"), enqueue_supplier=enqueue)
    srv = make_server(store, factory, lambda *args: sent.append(args), "local-test-token", 0, clock=lambda: NOW + 5)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield store, calls, sent, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def request(base, path, body=None):
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={"X-Orch-Token": "local-test-token", "Content-Type": "application/json"})
    with urlopen(req, timeout=5) as response:
        return json.load(response)


def test_album_wire_contract_preserves_images_captions_and_capability(server):
    store, calls, _, base = server
    first = {"mime_type": "image/png", "data": "aW1hZ2Ux"}
    second = {"mime_type": "image/jpeg", "data": "aW1hZ2Uy"}
    mid, _ = store.add_photo_message(100, "CARVER +15%", first, "album", now=NOW)
    store.add_photo_message(101, "Фотография поставщика", second, "album", now=NOW + 1)
    data = request(base, "/next")
    assert data["message"]["id"] == mid
    assert data["message"]["attachments"] == [first, second]
    assert data["message"]["text"] == "CARVER +15%"
    assert "supplier_photo_import" in data["context"]["capabilities"]
    incoming = {"type": "enqueue_supplier_products", "category": "генераторы", "brand": "CARVER",
                "markup_percent": 15, "items": [{"name": "Генератор CARVER PPG-5000D", "brand": "CARVER",
                "model": "PPG-5000D", "cost_price": "45455", "stock": 149, "attachment_index": 1}]}
    result = request(base, "/intent", {"message_id": mid, "intent": {"reply": "Прочитал", "actions": [incoming]}})
    assert result["ok"] and "52273" in result["reply"]
    assert calls[0][2] == [first, second] and len(store.batches("open")) == 1


def test_photo_first_followup_receives_whole_album_from_trusted_storage(server):
    store, _, _, base = server
    for index in range(5):
        mid, _ = store.add_photo_message(100 + index, "Прайс", {"mime_type": "image/png", "data": "aW1hZ2U="},
                                         "album", now=NOW + index / 10)
    store.mark_done(mid)
    voice_id = store.add_message(110, "voice", "По этим фото CARVER +15%", now=NOW + 4)
    data = request(base, "/next")
    assert data["message"]["id"] == voice_id
    assert data["message"]["attachment_context"] == "recent_photos"
    assert len(data["message"]["attachments"]) == 5
    assert {a["source_message_id"] for a in data["message"]["attachments"]} == {mid}
