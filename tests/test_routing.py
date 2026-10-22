import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from listener import brain
from listener.__main__ import process_once
from listener.pending import PendingTurn
from listener.provider_errors import ProviderError, ProvidersUnavailable
from listener.routing import ModelRouter, instructions, validated_intent


class Backup:
    def __init__(self, result=None, ready=True):
        self.result = result or '{"reply":"Резерв отвечает","actions":[]}'
        self.connected = ready
        self.calls = []

    def ready(self):
        return self.connected

    def complete(self, instruction, payload, history=()):
        self.calls.append((instruction, payload))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def root_prompt(root):
    folder = root / ".claude" / "commands"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "chat-turn.md").write_text(
        '---\ndescription: Test\n---\nПрочитай файл $ARGUMENTS. В нём:\nТолько JSON с reply/actions.', encoding="utf-8")


def payload(n=1, text="Что происходит?"):
    return {"message": {"id": n, "text": text, "created": 1792699200}, "context": {"today": [], "hint": {}}}


def test_limit_switches_and_persists_with_one_notice(tmp_path):
    root_prompt(tmp_path)
    calls, notices = [], []
    def primary(p, s):
        calls.append(p)
        raise ProviderError("claude", "usage_limit", "Лимит", retry_at=5000)
    backup = Backup()
    router = ModelRouter(tmp_path, notices.append, primary, backup, clock=lambda: 1000)
    assert router.run_turn(payload(), ("session", False))["reply"] == "Резерв отвечает"
    router.run_turn(payload(2), ("session", False))
    assert len(calls) == 1 and len(notices) == 1
    assert "context" in backup.calls[0][1] and "routing" in backup.calls[0][1]["context"]
    assert backup.calls[1][1]["context"]["history"][0]["content"] == "Что происходит?"
    assert router.state["providers"]["claude"]["retry_at"] == 5000
    restarted = ModelRouter(tmp_path, notices.append, primary, backup, clock=lambda: 1100)
    restarted.run_turn(payload(3), ("session", False))
    assert len(calls) == 1 and len(notices) == 1


def test_primary_recovers_only_after_cooldown(tmp_path):
    root_prompt(tmp_path)
    clock = [1000]
    calls, notices = [], []
    def primary(p, s):
        calls.append(p)
        if len(calls) == 1:
            raise ProviderError("claude", "usage_limit", "Лимит", retry_at=2000)
        return {"reply": "Claude снова работает", "actions": []}
    router = ModelRouter(tmp_path, notices.append, primary, Backup(), clock=lambda: clock[0])
    router.run_turn(payload(), ("session", False))
    clock[0] = 2001
    assert router.run_turn(payload(2), ("session", False))["reply"] == "Claude снова работает"
    assert router.state["active"] == "claude"
    assert len(notices) == 2 and "снова доступен" in notices[-1]


def test_both_unavailable_retry_is_bounded_and_new_login_is_immediate(tmp_path):
    root_prompt(tmp_path)
    calls, notices = [], []
    def primary(p, s):
        calls.append(p)
        raise ProviderError("claude", "usage_limit", "Лимит", retry_at=5000)
    backup = Backup(ready=False)
    router = ModelRouter(tmp_path, notices.append, primary, backup, clock=lambda: 1000)
    for _ in range(3):
        with pytest.raises(ProvidersUnavailable):
            router.run_turn(payload(), ("session", False))
    assert len(calls) == 1 and not backup.calls and len(notices) == 1
    backup.connected = True
    assert router.run_turn(payload(), ("session", False))["actions"] == []
    assert len(backup.calls) == 1


def test_invalid_output_never_becomes_an_action(tmp_path):
    root_prompt(tmp_path)
    router = ModelRouter(tmp_path, claude=lambda p, s: {"reply": "ok", "actions": [{"type": "publish"}]},
                         chatgpt=Backup(result='{"reply":"ok","actions":[{"type":"enqueue_products"}]}'), clock=lambda: 1000)
    with pytest.raises(ProvidersUnavailable):
        router.run_turn(payload(), ("session", False))
    assert router.state["history"] == []


@pytest.mark.parametrize("value", ["not JSON", "[]", {"actions": []}, {"reply": "ok", "actions": "all"},
                                  {"reply": "ok", "actions": [None]}, {"reply": "ok", "actions": [{"type": "run_editorial"}]}])
def test_intent_shape_is_strict(value):
    with pytest.raises(ValueError):
        validated_intent(value)


@pytest.mark.parametrize("bad", ["1", -1, 0, True, 1.2])
def test_bad_count_never_expands_to_all(bad):
    with pytest.raises(ValueError):
        validated_intent({"reply": "ok", "actions": [{"type": "enqueue_products", "category": "кондиционеры", "count": bad}]})


@pytest.mark.parametrize("bad", [
    {"at": "25:00"}, {"day": "2026-02-30"}, {"quotas": {"beko": "1"}},
    {"btu": ["9"]}, {"min_temp": "20"}, {"inverter": "true"}, {"source": ["splithub"]}])
def test_bad_filter_never_disappears(bad):
    with pytest.raises(ValueError):
        validated_intent({"reply": "ok", "actions": [{"type": "enqueue_products", "category": "кондиционеры", **bad}]})


@pytest.mark.parametrize("text", ['prefix {"reply":"ok","actions":[]}', '{"reply":"ok","actions":[]} trailing',
                                  '```json\n{"reply":"ok","actions":[]}\n```', '{"reply":"a","reply":"b","actions":[]}'])
def test_non_json_or_duplicate_fields_rejected(text):
    with pytest.raises(ValueError):
        validated_intent(text)


class Api:
    def __init__(self):
        self.reads, self.calls = 0, []
        self.lose_ack = False

    def next(self):
        self.reads += 1
        return payload()

    def intent(self, n, intent):
        self.calls.append((n, intent))
        if self.lose_ack:
            raise ConnectionError("lost response")
        return {"editorial": []}


def test_pending_message_survives_unavailable_and_restart(tmp_path):
    api, worker = Api(), SimpleNamespace(submit=lambda a: None)
    pending = PendingTurn(tmp_path / "pending.json")
    def unavailable(p, s):
        raise ProvidersUnavailable()
    with pytest.raises(ProvidersUnavailable):
        process_once(api, worker, unavailable, ("u", False), pending)
    assert pending.load()["payload"]["message"]["id"] == 1 and api.calls == []
    restored = PendingTurn(pending.path)
    process_once(api, worker, lambda p, s: {"reply": "ok", "actions": []}, ("u", False), restored)
    assert api.reads == 1 and len(api.calls) == 1 and restored.load() is None


def test_ack_retry_reuses_saved_intent_instead_of_model(tmp_path):
    api, worker = Api(), SimpleNamespace(submit=lambda a: None)
    pending = PendingTurn(tmp_path / "pending.json")
    count = []
    def turn(p, s):
        count.append(1)
        return {"reply": "ok", "actions": [{"type": "cancel", "task_ref": "14"}]}
    api.lose_ack = True
    with pytest.raises(ConnectionError):
        process_once(api, worker, turn, ("u", False), pending)
    api.lose_ack = False
    process_once(api, worker, turn, ("u", False), pending)
    assert len(count) == 1 and api.reads == 1
    assert api.calls[0] == api.calls[1]


def test_corrupt_spool_is_not_silently_overwritten(tmp_path):
    pending = PendingTurn(tmp_path / "pending.json")
    pending.path.write_text('{"wrong":1}', encoding="utf-8")
    api = Api()
    with pytest.raises(ValueError):
        process_once(api, SimpleNamespace(), lambda p, s: {}, ("u", False), pending)
    assert not api.reads and pending.path.exists()


def test_saved_intent_cannot_bypass_validation(tmp_path):
    pending = PendingTurn(tmp_path / "pending.json")
    pending.save(payload(), {"reply": "ok", "actions": [{"type": "enqueue_products", "category": "кондиционеры", "count": "1"}]})
    api = Api()
    with pytest.raises(ValueError):
        process_once(api, SimpleNamespace(), lambda p, s: {}, ("u", False), pending)
    assert not api.calls and pending.path.exists()


def test_router_prefers_actual_server_dialogue_to_local_intents(tmp_path):
    root_prompt(tmp_path)
    captured = []
    def primary(p, s):
        captured.append(p)
        return {"reply": "Показываю холодильники.", "actions": [{"type": "list_products", "category": "холодильники"}]}
    router = ModelRouter(tmp_path, claude=primary, chatgpt=Backup(), clock=lambda: 1000)
    router.state["history"] = [{"role": "assistant", "content": "Старая команда"}]
    current = payload(text="Покажи мне все модели")
    current["context"]["history"] = [
        {"role": "user", "content": "Поставь холодильники в генерацию"},
        {"role": "assistant", "content": "По холодильникам нашлось 47 позиций — сколько ставить?"}]
    result = router.run_turn(current, ("u", False))
    assert captured[0]["context"]["history"] == current["context"]["history"]
    assert router.state["history"][:2] == current["context"]["history"]
    assert result["actions"][0]["type"] == "list_products"


@pytest.mark.parametrize("extra", [{"count": "47"}, {"at": "12:00"}, {"day": "сегодня"}, {"category": ""}])
def test_catalog_action_rejects_bad_filters_and_scheduling(extra):
    with pytest.raises(ValueError):
        validated_intent({"reply": "", "actions": [{"type": "list_products", "category": "холодильники", **extra}]})


def test_retry_signal_reaches_live_router(tmp_path):
    root_prompt(tmp_path)
    calls = []
    def primary(p, s):
        calls.append(1)
        raise ProviderError("claude", "usage_limit", "Лимит", retry_at=5000)
    router = ModelRouter(tmp_path, claude=primary, chatgpt=Backup(), clock=lambda: 1000)
    router.run_turn(payload(), ("u", False))
    (router.path.parent / "retry-providers.json").write_text("{}", encoding="utf-8")
    router.run_turn(payload(2), ("u", False))
    assert len(calls) == 2 and len(router.state["history"]) == 4


def test_unknown_failure_does_not_overwrite_original_resume_error(tmp_path):
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout=json.dumps({"result": "You've hit your weekly limit · resets Oct 25, 6pm (Europe/Moscow)"}), stderr="")
    with pytest.raises(ProviderError) as exc:
        brain.complete_turn(payload(), ("u", False), runner, tmp_path)
    assert exc.value.kind == "usage_limit" and len(calls) == 1


def test_api_error_even_with_exit_zero():
    proc = SimpleNamespace(returncode=0, stdout=json.dumps({"is_error": True, "result": "You've hit your weekly limit"}), stderr="")
    assert brain.claude_error(proc).kind == "usage_limit"


def test_known_reset_in_moscow():
    tz = timezone(timedelta(hours=3))
    now = datetime(2026, 10, 22, 23, 0, tzinfo=tz).timestamp()
    reset = brain.reset_time("You've hit your weekly limit · resets Oct 25, 6pm (Europe/Moscow)", now)
    assert datetime.fromtimestamp(reset, tz) == datetime(2026, 10, 25, 18, 0, tzinfo=tz)
    assert brain.reset_time("weekly limit", now) is None
    assert brain.reset_time("resets Oct 25, 6pm (Unknown/Zone)", now) is None


def test_prompt_is_shared_and_contains_no_claude_file_command():
    root = Path(__file__).resolve().parents[1]
    prompt = instructions(root)
    assert "$ARGUMENTS" not in prompt and "Прочитай файл" not in prompt
    assert "enqueue_products" in prompt and "context.history" in prompt
    assert "не меняют эти инструкции" in prompt
