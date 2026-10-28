import copy
import json

import pytest

from listener.intent_validation import validated_intent
from orchestrator_bot import actions
from orchestrator_bot.api import handle_intent
from orchestrator_bot.intents import Outcome, normalize, plan
from orchestrator_bot.store import Store
from orchestrator_bot.supplier_import import normalize_action, prepare_items, source_message, validate_source


def action():
    def row(model, cost, stock, brand="CARVER"):
        return {"name": f"Генератор {brand} {model}", "brand": brand, "model": model,
                "cost_price": cost, "stock": stock, "attachment_index": 0}
    return {"type": "enqueue_supplier_products", "category": "генераторы", "brand": "CARVER",
            "markup_percent": 15, "items": [row("PPG-5000D", "45455", 149),
                row("PPG-5000DE", "52200", 63), row("PPG-5000Di", "50720", 6),
                row("PPG-7000DE", "63910", 0), row("6 GF-3", "55665", 2, "LIFAN")]}


class SupplierFactory:
    def __init__(self):
        self.calls, self.receipts, self.active = [], {}, set()

    def resolve(self, action):
        raise AssertionError("A photo import must never resolve catalog substitutes")

    def enqueue_supplier(self, action, source_id, attachments=()):
        self.calls.append((action, source_id, attachments))
        if source_id in self.receipts:
            return self.receipts[source_id]
        rows = [r for r in prepare_items(action, source_id) if r["key"] not in self.active]
        self.active.update(r["key"] for r in rows)
        result = {"accepted": [r["key"] for r in rows], "items": rows, "manifest_path": source_id}
        self.receipts[source_id] = result
        return result

    def workers(self):
        return {"generation": False, "agent_online": False}

    def cancel(self, keys):
        self.active.difference_update(keys)
        return {"cancelled": len(keys)}


@pytest.fixture
def context(tmp_path):
    store = Store(tmp_path / "orchestrator.db")
    # Source content is opaque to actions; image decoding is tested separately.
    images = [{"mime_type": "image/png", "data": "original-photo-bytes"}]
    mid = store.add_message(42, "photo", "CARVER с фото в генерацию, +15%", now=100, attachments=images)
    return store, SupplierFactory(), mid, images


def test_exact_prices_stocks_brand_and_model_suffixes():
    rows = prepare_items(action(), "test-source")
    assert [r["model"] for r in rows] == ["PPG-5000D", "PPG-5000DE", "PPG-5000Di"]
    assert [r["price"] for r in rows] == [52273, 60030, 58328]
    assert [r["stock"] for r in rows] == [149, 63, 6]
    assert len({r["key"] for r in rows}) == 3


@pytest.mark.parametrize("field,value", [("markup_percent", True), ("markup_percent", float("nan")),
    ("markup_percent", -1), ("markup_percent", "1001"), ("markup_percent", "15%")])
def test_invalid_markup_cannot_be_coerced(field, value):
    incoming = action()
    incoming[field] = value
    with pytest.raises(ValueError):
        normalize_action(incoming)


@pytest.mark.parametrize("field,value", [("stock", True), ("stock", "149"), ("stock", -1),
    ("cost_price", 0), ("cost_price", "Infinity"), ("cost_price", "45455=0"),
    ("cost_price", "1.999"), ("attachment_index", True), ("model", "PPG|5000D")])
def test_ambiguous_or_invalid_source_fields_are_rejected(field, value):
    incoming = action()
    incoming["items"][0][field] = value
    with pytest.raises(ValueError):
        normalize_action(incoming)


def test_conflicting_duplicate_blocks_whole_import():
    incoming = action()
    duplicate = {**incoming["items"][0], "stock": 1}
    incoming["items"].append(duplicate)
    with pytest.raises(ValueError, match="Противоречивые"):
        prepare_items(incoming, "test")
    duplicate["stock"] = 149
    assert len(prepare_items(incoming, "test")) == 3


def test_model_whitespace_normalization_preserves_suffixes():
    incoming = action()
    incoming["items"][0]["model"] = " PPG-11000  DVR "
    assert prepare_items(incoming, "test")[0]["key"] == "excel|carver|ppg-11000 dvr"


def test_intent_whitelist_retains_facts_and_never_calls_catalog():
    incoming = {"reply": "Принял фотографии", "actions": [action()]}
    checked = validated_intent(json.dumps(incoming))
    assert checked["actions"][0]["markup_percent"] == "15"
    outcomes = plan(checked["actions"], lambda _: pytest.fail("catalog called"))
    assert outcomes[0].kind == "enqueue_supplier_products"
    malformed = copy.deepcopy(incoming)
    malformed["actions"][0]["items"][0]["selling_price"] = 1
    with pytest.raises(ValueError):
        normalize(malformed)


def test_photo_to_queue_has_cancel_batch_price_report_and_worker_notice(context):
    store, factory, mid, images = context
    sent = []
    response = handle_intent(store, factory, lambda *args: sent.append(args), mid,
                             {"reply": "Прочитал фото", "actions": [action()]})
    assert response["ok"]
    assert len(factory.calls) == 1 and factory.calls[0][2] == images
    batch, = store.batches("open")
    assert batch["category"] == "генераторы" and len(batch["keys"]) == 3
    assert "52273 ₽" in response["reply"] and "остаток 149" in response["reply"]
    assert "Задачи ждут" in response["reply"]
    assert any(button["callback_data"].startswith("c:") for row in sent[-1][1] for button in row)
    repeated = handle_intent(store, factory, lambda *args: sent.append(args), mid,
                            {"reply": "Прочитал фото", "actions": [action()]})
    assert repeated["duplicate"] and len(factory.calls) == 1


def test_unavailable_image_never_queues(context):
    store, factory, mid, _ = context
    incoming = action()
    incoming["items"][0]["attachment_index"] = 1
    response = handle_intent(store, factory, lambda *args: None, mid, {"reply": "", "actions": [incoming]})
    assert factory.calls == [] and store.batches("open") == []
    assert "фотографии" in response["reply"]


def test_photo_first_then_voice_uses_stable_original_context(context):
    store, _, mid, images = context
    voice = store.add_message(43, "voice", "Из этих фото CARVER +15%", now=101)
    before = source_message(store, voice)
    store.add_message(44, "photo", "later unrelated photo", now=102, attachments=[{"data": "later"}])
    assert source_message(store, voice) == before
    assert before["attachments"] == [{**images[0], "source_message_id": mid}]
    assert validate_source(normalize_action(action()), before)


def test_new_text_list_does_not_bind_unrelated_recent_photos(context):
    store, factory, _, _ = context
    mid = store.add_message(43, "text", "Новый список поставщика CARVER +15%", now=101)
    incoming = action()
    for row in incoming["items"]:
        row.pop("attachment_index")
    handle_intent(store, factory, lambda *args: None, mid, {"reply": "", "actions": [incoming]})
    assert factory.calls[0][2] == []


def test_restore_reselects_instead_of_reusing_stale_receipt(context):
    store, factory, mid, _ = context
    result = actions.execute(Outcome("enqueue_supplier_products", action(), ""), factory, store, mid)
    original_source = factory.calls[0][1]
    actions.cancel_logged(result.log_id, factory, store, None)
    assert not factory.active
    restored = actions.restore_logged(result.log_id, factory, store)
    assert len(factory.active) == 3
    assert factory.calls[-1][1] != original_source
    assert store.get_batch(result.log_id)["status"] == "closed"
    assert store.get_batch(restored.log_id)["status"] == "open"


def test_crash_after_dispatch_recovers_receipt_and_single_batch(context):
    store, factory, mid, _ = context
    assert store.mark_intent(mid)
    # Simulate dispatch+log succeeded, process crashed before response/done.
    actions.execute(Outcome("enqueue_supplier_products", action(), ""), factory, store, mid)
    response = handle_intent(store, factory, lambda *args: None, mid, {"reply": "", "actions": [action()]})
    assert response["ok"] and not response.get("duplicate")
    assert len(store.actions_since(0)) == 1 and len(store.batches("open")) == 1
    assert store.count_new() == 0 and len(factory.active) == 3
