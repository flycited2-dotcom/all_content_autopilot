import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

from cf_preview_repair import patched_publisher, patched_excel, review_env

CF_ROOT = Path(__file__).resolve().parents[2] / "content-factory"


def load_publisher(tmp_path, monkeypatch):
    original = (CF_ROOT / "src/content_factory/publish/telegram.py").read_text(encoding="utf-8")
    target = tmp_path / "publisher.py"
    target.write_text(patched_publisher(original), encoding="utf-8")
    # Standalone smoke test of the actual patched source, without importing or
    # writing bytecode to the production checkout.
    parent = types.ModuleType("content_factory")
    state = types.ModuleType("content_factory.sqlite_state")
    state.state_connection = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "content_factory", parent)
    monkeypatch.setitem(sys.modules, "content_factory.sqlite_state", state)
    spec = importlib.util.spec_from_file_location("repair_test_publisher", target)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_migration_is_retried_once_on_exact_new_chat_and_retains_buttons(tmp_path, monkeypatch):
    module = load_publisher(tmp_path, monkeypatch)
    calls = []
    def handle(request):
        from urllib.parse import parse_qs
        values = parse_qs(request.content.decode())
        calls.append(values)
        return httpx.Response(400, json={"ok": False, "parameters": {"migrate_to_chat_id": -1003775164695}}) if len(calls) == 1 else httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = module.publish_post("test", "-5428762212", "https://example.com/a.png", "caption", http=client, retries=0,
                                     reply_markup='{"inline_keyboard":[]}')
    assert result.ok and result.message_id == 7
    assert [c["chat_id"] for c in calls] == [["-5428762212"], ["-1003775164695"]]
    assert calls[0]["reply_markup"] == calls[1]["reply_markup"]


def test_repeated_migration_does_not_loop(tmp_path, monkeypatch):
    module = load_publisher(tmp_path, monkeypatch)
    calls = []
    def send(*args, **kwargs):
        calls.append(args)
        return httpx.Response(400, json={"ok": False, "description": "migrated", "parameters": {"migrate_to_chat_id": -1003775164695}})
    monkeypatch.setattr(module, "_send_once", send)
    assert not module.publish_post("test", "-1", "url", "caption", retries=0).ok
    assert len(calls) == 2


@pytest.mark.parametrize("destination", [True, 1, "-1003775164695", None, -(2**53)])
def test_invalid_migration_does_not_change_recipient(tmp_path, monkeypatch, destination):
    module = load_publisher(tmp_path, monkeypatch)
    calls = []
    def send(*args, **kwargs):
        calls.append(args)
        return httpx.Response(400, json={"ok": False, "parameters": {"migrate_to_chat_id": destination}})
    monkeypatch.setattr(module, "_send_once", send)
    assert not module.publish_post("test", "-1", "url", "caption", retries=0).ok
    assert len(calls) == 1


def test_review_config_changes_only_destination():
    original = "SECRET=must-stay-exactly\nTELEGRAM_REVIEW_CHANNEL_ID='-5428762212' # old\nOTHER=12\n"
    updated = review_env(original, -5428762212, -1003775164695)
    assert updated == "SECRET=must-stay-exactly\nTELEGRAM_REVIEW_CHANNEL_ID=-1003775164695\nOTHER=12\n"
    assert review_env(updated, -5428762212, -1003775164695) == updated
    with pytest.raises(ValueError):
        review_env(original.replace("-5428762212", "-99"), -5428762212, -1003775164695)


def test_same_failure_for_100_products_alerts_once_and_survives_restart(tmp_path):
    import ast
    import sqlite3
    import time
    source = patched_excel((CF_ROOT / "src/content_factory/orchestrator/excel_run.py").read_text(encoding="utf-8"))
    module = ast.parse(source)
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "preview_delivery_alert")
    namespace = {"sqlite3": sqlite3, "time": time, "re": __import__("re")}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<alert>", "exec"), namespace)
    sent = []
    db = tmp_path / "state.db"
    for _ in range(100):
        namespace["preview_delivery_alert"](db, "-1", "group chat was upgraded to a supergroup chat", sent.append, now=1000)
    assert len(sent) == 1 and "превью" in sent[0].lower() and "повтор" in sent[0].lower()
    namespace["preview_delivery_alert"](db, "-1", "group chat was upgraded to a supergroup chat", sent.append, now=1001)
    assert len(sent) == 1
    namespace["preview_delivery_alert"](db, "-1", "group chat was upgraded to a supergroup chat", sent.append, now=1901)
    assert len(sent) == 2


@pytest.mark.parametrize("valid", [True, False])
def test_server_preflight_checks_selected_supergroup_and_bot_membership(monkeypatch, valid):
    import repair_review_chat as repair
    real_client = httpx.Client
    def handle(request):
        from urllib.parse import parse_qs
        if request.url.path.endswith("getMe"):
            result = {"id": 8639233666, "username": "Sendpr1ce_bot"}
        elif request.url.path.endswith("getChatMember"):
            result = {"status": "administrator" if valid else "left"}
        else:
            chat = int(parse_qs(request.content.decode())["chat_id"][0])
            result = {"id": chat, "title": "Тест_контент_завод", "type": "group" if chat == repair.OLD else "supergroup"}
        return httpx.Response(200, json={"ok": True, "result": result})
    monkeypatch.setattr(repair.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle)))
    if valid:
        repair.check_telegram("test")
    else:
        with pytest.raises(repair.RepairError):
            repair.check_telegram("test")
