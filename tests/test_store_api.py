import json
import threading
import urllib.error
import urllib.request

import pytest

from orchestrator_bot.api import make_server
from orchestrator_bot.config import Settings
from orchestrator_bot.store import Store

TOKEN = "t0ken"


class FakeFactory:
    def __init__(self, total=2):
        self.total = total
        self.hints = 0
        self.fail_enqueue = False
        self.started = False
        self.worker_state = {"generation": True, "agent_online": True, "last_seen": None, "lanes": [], "commands": {}}
        self.enqueued = []
        self.cancelled = []

    def hint(self):
        self.hints += 1
        return {"brands": ["Beko", "Ресанта"], "sources": ["manual"]}

    def resolve(self, action):
        return {"total_matches": self.total, "items": [{}] * self.total}

    def enqueue(self, action):
        if self.fail_enqueue:
            raise RuntimeError("database is locked")
        self.enqueued.append(action)
        return {"requested": 2, "accepted": ["excel|beko|x100", "excel|beko|x200"],
                "items": [{"name": "Холодильник Beko X100"}, {"name": "Холодильник Beko X200"}], "due_at": None}

    def cancel(self, keys):
        self.cancelled.append(keys)
        return {"cancelled": len(keys), "text": ""}

    def status(self, since):
        return {"by_status": {}}

    def workers(self):
        return self.worker_state

    def start_workers(self):
        self.started = True
        return {"generation": True, "commanded": ["agent_command"], "requested_run": True}



@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path / "orch.db")
    factory = FakeFactory()
    sent = []
    srv = make_server(store, factory, lambda text, kb=None: sent.append((text, kb)), TOKEN, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield store, factory, sent, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _call(base, method, path, body=None, token=TOKEN):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Orch-Token": token, "Content-Type": "application/json"} if token
                                 else {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _enqueue_intent(count=None):
    return {"reply": "Сделаю.", "actions": [{"type": "enqueue_products", "category": "холодильники",
                                             "count": count, "quotas": {"beko": 2}}]}


def test_settings_from_env_defaults():
    s = Settings.from_env({"ORCH_BOT_TOKEN": "x", "ORCH_OWNER_CHAT_ID": "1264067528", "ORCH_API_TOKEN": "y"})
    assert (s.owner_chat_id, s.api_port, s.doubt_threshold, s.whisper_model) == (1264067528, 8790, 40, "small")


def test_claim_returns_oldest_and_requeues_stale(tmp_path):
    store = Store(tmp_path / "orch.db")
    first = store.add_message(1, "text", "раз", now=100.0)
    store.add_message(2, "text", "два", now=101.0)
    assert store.claim_next(now=200.0)["id"] == first
    assert store.claim_next(now=201.0)["text"] == "два"
    assert store.claim_next(now=300.0) is None
    assert store.claim_next(now=200.0 + 601)["id"] == first      # слушатель упал — вернулось


def test_api_rejects_missing_token(server):
    *_, base = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _call(base, "POST", "/heartbeat", {}, token=None)
    assert exc.value.code == 401


def test_intent_enqueue_sends_reply_with_cancel_button(server):
    store, factory, sent, base = server
    msg = store.add_message(10, "voice", "поставь 2 холодильника беко")
    out = _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent(count=2)})
    assert out["ok"] is True
    text, kb = sent[-1]
    assert "Сделаю." in text and "Поставил" in text
    assert kb[0][0]["callback_data"].startswith("c:")
    assert len(factory.enqueued) == 1


def test_duplicate_intent_executes_once(server):
    store, factory, sent, base = server
    msg = store.add_message(11, "text", "поставь")
    _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent(count=2)})
    out = _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent(count=2)})
    assert out["duplicate"] is True
    assert len(factory.enqueued) == 1


def test_intent_with_doubt_saves_ask_with_buttons(server):
    store, factory, sent, base = server
    factory.total = 57
    msg = store.add_message(12, "text", "все холодильники")
    _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent()})
    text, kb = sent[-1]
    assert [row[0]["callback_data"].rsplit(":", 1)[1] for row in kb] == ["0", "1", "2"]   # по кнопке в строке
    assert "57" in text
    assert factory.enqueued == []


def test_next_includes_catalog_hint_and_marks_claimed(server):
    store, factory, sent, base = server
    store.add_message(13, "text", "привет")
    out = _call(base, "GET", "/next")
    assert out["message"]["text"] == "привет"
    assert out["context"]["hint"] == factory.hint()
    assert "list_products" in out["context"]["capabilities"]
    assert _call(base, "GET", "/next")["message"] is None


def test_editorial_actions_returned_to_listener(server):
    store, factory, sent, base = server
    msg = store.add_message(14, "text", "сделай пост")
    intent = {"reply": "Запускаю.", "actions": [{"type": "run_editorial", "brief": "тепловые насосы",
                                                 "channels": ["vk_editorial"]}]}
    out = _call(base, "POST", "/intent", {"message_id": msg, "intent": intent})
    assert out["editorial"] == [{"type": "run_editorial", "brief": "тепловые насосы", "channels": ["vk_editorial"]}]


def test_heartbeat_and_reply(server):
    store, factory, sent, base = server
    _call(base, "POST", "/heartbeat", {})
    assert store.last_heartbeat() is not None
    _call(base, "POST", "/reply", {"text": "Черновик готов"})
    assert sent[-1][0] == "Черновик готов"


def test_unparseable_intent_is_rejected_without_burning_the_message(server):
    store, factory, sent, base = server
    msg = store.add_message(15, "text", "поставь")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _call(base, "POST", "/intent", {"message_id": msg, "intent": "не json"})
    assert exc.value.code == 400
    out = _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent(count=2)})
    assert "duplicate" not in out and len(factory.enqueued) == 1


def test_next_without_message_does_not_compute_hint(server):
    store, factory, sent, base = server
    assert _call(base, "GET", "/next")["message"] is None
    assert factory.hints == 0


def test_failed_action_reports_error_and_completes_message(server):
    store, factory, sent, base = server
    factory.fail_enqueue = True
    msg = store.add_message(20, "text", "поставь")
    out = _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent(count=2)})
    assert out["ok"] is True
    assert "❌" in sent[-1][0]
    assert store.claim_next(now=10**10) is None              # сообщение завершено, а не зациклено


def test_duplicate_replays_editorial_and_marks_done(server):
    store, factory, sent, base = server
    msg = store.add_message(21, "text", "сделай пост")
    intent = {"reply": "Запускаю.", "actions": [{"type": "run_editorial", "brief": "насосы", "channels": []}]}
    first = _call(base, "POST", "/intent", {"message_id": msg, "intent": intent})
    again = _call(base, "POST", "/intent", {"message_id": msg, "intent": intent})
    assert again["duplicate"] is True and again["editorial"] == first["editorial"]
    assert store.claim_next(now=10**10) is None


def test_send_failure_still_completes(tmp_path):
    from orchestrator_bot.api import handle_intent
    store = Store(tmp_path / "o.db")

    def broken_send(text, kb=None):
        raise RuntimeError("Telegram 429")
    msg = store.add_message(22, "text", "x")
    out = handle_intent(store, FakeFactory(), broken_send, msg, _enqueue_intent(count=2))
    assert out["ok"] is True
    assert store.claim_next(now=10**10) is None


def test_enqueue_warns_and_offers_start_when_agent_not_running(server):
    store, factory, sent, base = server
    factory.worker_state = {"generation": False, "agent_online": False, "last_seen": None, "lanes": [], "commands": {}}
    msg = store.add_message(30, "text", "поставь")
    _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent(count=2)})
    text, kb = sent[-1]
    assert "⚠️" in text and "генерация выключена" in text
    assert ["w:start"] in [[b["callback_data"]] for row in kb for b in row]


def test_no_warning_when_agent_ready(server):
    store, factory, sent, base = server
    msg = store.add_message(31, "text", "поставь")
    _call(base, "POST", "/intent", {"message_id": msg, "intent": _enqueue_intent(count=2)})
    assert "⚠️" not in sent[-1][0]


def test_next_context_includes_content_agent_state(server):
    store, factory, sent, base = server
    factory.worker_state = {"generation": False, "agent_online": True, "last_seen": None, "lanes": [], "commands": {}}
    store.add_message(32, "text", "почему не генерируется?")
    assert "генерация выключена" in _call(base, "GET", "/next")["context"]["workers"]


def test_catalog_lists_all_models_without_enqueue_or_quantity_question(server):
    store, factory, sent, base = server
    items = [{"name": f"Холодильник Модель-{i:03d} " + "🧊" * 50} for i in range(47)]
    factory.resolve = lambda action: {"total_matches": 47, "items": items}
    msg = store.add_message(40, "text", "Покажи мне все модели")
    result = _call(base, "POST", "/intent", {"message_id": msg, "intent": {
        "reply": "Показываю холодильники.", "actions": [{"type": "list_products", "category": "холодильники"}]}})
    assert len(sent) > 1
    text = "".join(t for t, _ in sent)
    assert all(item["name"] in text for item in items)
    assert text == result["reply"]
    assert all(len(t.encode("utf-16-le")) // 2 <= 3800 for t, _ in sent)
    assert not any(kb for _, kb in sent)
    assert factory.enqueued == [] and store.batches("open") == []
    assert store.actions_since(0) == []
    again = _call(base, "POST", "/intent", {"message_id": msg, "intent": {"reply": "", "actions": []}})
    assert again["duplicate"] and len(sent) > 1 and again["reply"] == text


def test_next_history_includes_actual_quantity_question_and_button_exchange(server):
    store, factory, sent, base = server
    factory.total = 47
    first = store.add_message(41, "voice", "Поставь холодильники в генерацию")
    _call(base, "POST", "/intent", {"message_id": first, "intent": _enqueue_intent()})
    second = store.add_message(42, "text", "Покажи мне все модели")
    # Даже если более поздний ответ уже записан, он не должен попасть в прошлое.
    store.record_exchange("Выбор: Все 47", "Поставил холодильники: 47 поз.")
    out = _call(base, "GET", "/next")
    assert out["message"]["id"] == second
    assert out["context"]["history"] == [
        {"role": "user", "content": "Поставь холодильники в генерацию"},
        {"role": "assistant", "content": sent[-1][0]}]
    assert "47 позиций — сколько ставить?" in out["context"]["history"][1]["content"]
    third = store.add_message(43, "text", "что поставлено?")
    assert store.dialogue_before(third)[-2:] == [
        {"role": "user", "content": "Выбор: Все 47"},
        {"role": "assistant", "content": "Поставил холодильники: 47 поз."}]


def test_empty_catalog_does_not_enqueue(server):
    store, factory, sent, base = server
    factory.total = 0
    msg = store.add_message(44, "text", "покажи холодильники")
    _call(base, "POST", "/intent", {"message_id": msg, "intent": {
        "reply": "", "actions": [{"type": "list_products", "category": "холодильники"}]}})
    assert "Нет доступных" in sent[-1][0] and factory.enqueued == []
