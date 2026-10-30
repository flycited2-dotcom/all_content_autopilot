import base64
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from orchestrator_bot.factory import Factory
from orchestrator_bot import supplier_catalog as supplier


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def action(*, photo=False):
    def item(model, price, stock, brand="CARVER"):
        row = {"name": f"Генератор {brand} {model}", "brand": brand, "model": model,
               "cost_price": price, "stock": stock}
        if photo:
            row["attachment_index"] = 0
        return row
    return {"type": "enqueue_supplier_products", "category": "генераторы", "brand": "CARVER",
            "markup_percent": "15", "items": [item("PPG-5000D", "45455", 149),
                                                item("PPG-5000DE", "52200", 63),
                                                item("PPG-5000Di", "50720", 0),
                                                item("6 GF-3", "55665", 2, "LIFAN")]}


@pytest.fixture
def factory_snapshot(tmp_path, monkeypatch):
    # Exercise the real local factory snapshot selection transaction rather than
    # imitating its duplicate/restoration behavior in a fake store.
    source = (Path(__file__).resolve().parents[1] / "snapshots/2026-10-20/src/content_factory"
              / "orchestrator/excel_pipeline.py")
    if not source.is_file():
        pytest.skip("Local content-factory source snapshot is unavailable")
    name = "_supplier_catalog_test_excel_pipeline"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "content_factory.orchestrator.excel_pipeline", module)
    database = tmp_path / "state" / "factory.db"
    cf = SimpleNamespace(_paths=lambda args: (database, tmp_path / "prices", None))
    args = SimpleNamespace(no_wake=True)
    return cf, args, database, module.ExcelStore


def test_supplier_photo_uses_own_prices_stock_and_retains_original(factory_snapshot):
    cf, args, database, store_type = factory_snapshot
    original = {"data": base64.b64encode(PNG).decode(), "mime_type": "image/png",
                "file_name": "../../outside.png", "file_unique_id": "telegram-unique"}
    result = supplier.enqueue_bundle(cf, args, {"source_id": "telegram:41", "action": action(photo=True),
                                                "attachments": [original]})
    assert result["requested"] == 2
    assert result["accepted"] == ["excel|carver|ppg-5000d", "excel|carver|ppg-5000de"]
    store = store_type(database)
    assert store.get("excel|carver|ppg-5000d").price == 52273
    assert store.get("excel|carver|ppg-5000de").price == 60030
    assert store.get("excel|carver|ppg-5000di") is None
    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["items"][0]["stock"] == 149
    assert manifest["items"][0]["cost_price"] == "45455"
    assert manifest["items"][0]["attachment_index"] == 0
    assert (manifest_path.parent / manifest["attachments"][0]["path"]).read_bytes() == PNG
    assert manifest["attachments"][0]["sha256"]
    assert manifest_path.parent.parent == database.parent / "supplier_imports"
    assert not (database.parent / "outside.png").exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_cache").fetchone()[0] == 0


def test_supplier_retry_returns_receipt_without_second_selection(factory_snapshot):
    cf, args, database, _ = factory_snapshot
    bundle = {"source_id": "telegram:42", "action": action(), "attachments": []}
    first = supplier.enqueue_bundle(cf, args, bundle)
    second = supplier.enqueue_bundle(cf, args, bundle)
    assert second == {**first, "duplicate": True}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM excel_selections").fetchone()[0] == 1


def test_new_photo_cannot_regenerate_same_standard_model_key(factory_snapshot):
    cf, args, database, store_type = factory_snapshot
    first = supplier.enqueue_bundle(cf, args, {"source_id": "telegram:43", "action": action()})
    revised = action()
    revised["items"][0]["cost_price"] = "99999"
    second = supplier.enqueue_bundle(cf, args, {"source_id": "telegram:44", "action": revised})
    assert first["accepted"]
    assert second["accepted"] == []
    assert store_type(database).get("excel|carver|ppg-5000d").price == 52273


def _live_status_paths(cf, database, store_type):
    store_type(database)
    queue = database.parent / 'queue.db'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY,brand TEXT,model TEXT,status TEXT,mode TEXT)')
    cf._paths = lambda args: (database, database.parent / 'prices', queue)
    return queue


@pytest.mark.parametrize('previous_status', ['failed', 'published', 'cancelled'])
def test_new_only_skips_previous_attempt_and_selects_only_missing(factory_snapshot, previous_status):
    cf, args, database, store_type = factory_snapshot
    _live_status_paths(cf, database, store_type)
    old = action()
    old['items'] = old['items'][:1]
    supplier.enqueue_bundle(cf, args, {'source_id': 'telegram:old', 'action': old})
    with sqlite3.connect(database) as db:
        db.execute('UPDATE excel_items SET status=?', (previous_status,))
    result = supplier.enqueue_bundle(cf, args,
        {'source_id': 'telegram:resent', 'action': action(), 'new_only': True})
    assert result['accepted'] == ['excel|carver|ppg-5000de']
    assert [s['key'] for s in result['skipped']] == ['excel|carver|ppg-5000d']
    assert result['skipped'][0]['state'] == previous_status
    assert store_type(database).get('excel|carver|ppg-5000d').status == previous_status
    assert store_type(database).get('excel|carver|ppg-5000de').status == 'new'


def test_new_only_receipt_replay_precedes_changed_queue_status(factory_snapshot):
    cf, args, database, store_type = factory_snapshot
    _live_status_paths(cf, database, store_type)
    bundle = {'source_id': 'telegram:new-only-receipt', 'action': action(), 'new_only': True}
    first = supplier.enqueue_bundle(cf, args, bundle)
    assert len(first['accepted']) == 2
    with sqlite3.connect(database) as db:
        db.execute("UPDATE excel_items SET status='failed'")
        selections = db.execute('SELECT COUNT(*) FROM excel_selections').fetchone()[0]
    replay = supplier.enqueue_bundle(cf, args, bundle)
    assert replay == {**first, 'duplicate': True}
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT COUNT(*) FROM excel_selections').fetchone()[0] == selections
        assert {r[0] for r in db.execute('SELECT status FROM excel_items')} == {'failed'}


def test_evidence_for_same_source_cannot_be_overwritten(factory_snapshot):
    cf, args, _, _ = factory_snapshot
    bundle = {"source_id": "telegram:45", "action": action()}
    supplier.enqueue_bundle(cf, args, bundle)
    bundle["action"]["items"][0]["cost_price"] = "99999"
    with pytest.raises(ValueError, match="уже сохранены"):
        supplier.enqueue_bundle(cf, args, bundle)


def test_uncertain_factory_selection_is_held_without_blind_repeat(factory_snapshot, monkeypatch):
    cf, args, database, store_type = factory_snapshot
    bundle = {"source_id": "telegram:45:uncertain", "action": action()}
    select = store_type.select_items
    calls = []
    def interrupted(store, rows, **kwargs):
        accepted = select(store, rows, **kwargs)
        calls.append(accepted)
        raise RuntimeError("process interrupted after committing selection")
    monkeypatch.setattr(store_type, "select_items", interrupted)
    with pytest.raises(RuntimeError):
        supplier.enqueue_bundle(cf, args, bundle)
    with pytest.raises(ValueError, match="Результат постановки товаров неизвестен.*Проверьте очередь"):
        supplier.enqueue_bundle(cf, args, bundle)
    assert len(calls) == 1 and len(calls[0]) == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM excel_selections").fetchone()[0] == 1
    assert list((database.parent / "supplier_imports").glob("*/selection-started.json"))
    assert not list((database.parent / "supplier_imports").glob("*/selection.json"))


def test_category_prefix_is_for_pipeline_and_preserves_original_source_name(factory_snapshot):
    cf, args, database, store_type = factory_snapshot
    source = action()
    source["items"][0]["name"] = "CARVER PPG-5000D"
    source["items"][1]["name"] = "PPG-5000DE CARVER"
    result = supplier.enqueue_bundle(cf, args, {"source_id": "telegram:45:category", "action": source})
    store = store_type(database)
    assert store.get("excel|carver|ppg-5000d").name == "генераторы CARVER PPG-5000D"
    assert store.get("excel|carver|ppg-5000de").name == "генераторы PPG-5000DE CARVER"
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["items"][0]["name"] == "CARVER PPG-5000D"
    assert manifest["items"][0]["pipeline_name"] == "генераторы CARVER PPG-5000D"
    assert manifest["action"]["items"][1]["name"] == "PPG-5000DE CARVER"


@pytest.mark.parametrize("attachment", [
    {"data": "!!!", "mime_type": "image/png"},
    {"data": base64.b64encode(PNG).decode(), "mime_type": "image/jpeg"},
    {"data": base64.b64encode(PNG).decode(), "mime_type": "application/pdf"},
])
def test_bad_supplier_photos_are_rejected_before_selection(factory_snapshot, attachment):
    cf, args, database, _ = factory_snapshot
    with pytest.raises(ValueError):
        supplier.enqueue_bundle(cf, args, {"source_id": "telegram:46", "action": action(photo=True),
                                          "attachments": [attachment]})
    assert not database.exists()


def test_photo_row_must_reference_available_original(factory_snapshot):
    cf, args, database, _ = factory_snapshot
    with pytest.raises(ValueError, match="исходная фотография"):
        supplier.enqueue_bundle(cf, args, {"source_id": "telegram:47", "action": action(photo=True)})
    assert not database.exists()


def test_factory_wake_follows_evidence_and_selection(factory_snapshot, monkeypatch):
    cf, args, database, _ = factory_snapshot
    args.no_wake = False
    calls = []
    def wake(queue):
        assert list((database.parent / "supplier_imports").glob("*/manifest.json"))
        assert list((database.parent / "supplier_imports").glob("*/selection.json"))
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM excel_items").fetchone()[0] == 2
        calls.append(queue)
    monkeypatch.setattr(supplier, "_wake", wake)
    bundle = {"source_id": "telegram:48", "action": action()}
    supplier.enqueue_bundle(cf, args, bundle)
    supplier.enqueue_bundle(cf, args, bundle)
    assert calls == [None]


def test_factory_transports_supplier_images_in_file_and_cleans_up():
    observed = []
    def runner(command, **kwargs):
        assert command[2] == "orchestrator_bot.catalog_cli"
        assert command[3] == "enqueue-supplier"
        path = Path(command[command.index("--supplier-file") + 1])
        observed.append(path)
        bundle = json.loads(path.read_text(encoding="utf-8"))
        assert bundle == {"action": action(), "source_id": "telegram:49", "attachments": [{"data": "example"}]}
        assert "example" not in command
        return SimpleNamespace(returncode=0, stdout='{"accepted": []}')
    factory = Factory("python", "/opt/content-factory", runner=runner)
    assert factory.enqueue_supplier(action(), "telegram:49", [{"data": "example"}]) == {"accepted": []}
    assert observed and not observed[0].exists()


def test_supplier_temporary_file_is_removed_when_factory_fails():
    observed = []
    def runner(command, **kwargs):
        observed.append(Path(command[command.index("--supplier-file") + 1]))
        raise RuntimeError("failed")
    factory = Factory("python", "/opt/content-factory", runner=runner)
    with pytest.raises(RuntimeError):
        factory.enqueue_supplier(action(), "telegram:50")
    assert observed and not observed[0].exists()
