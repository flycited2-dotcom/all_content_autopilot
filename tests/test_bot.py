import json

import httpx
import pytest

from orchestrator_bot import bot
from orchestrator_bot.bot import Deps
from orchestrator_bot.store import Store
from orchestrator_bot.telegram_api import TelegramApi

OWNER = 1264067528
NOW = 1_800_000_000.0


class FakeTg:
    def __init__(self):
        self.calls = []
        self._next_id = 100

    def send(self, chat_id, text, keyboard=None):
        self._next_id += 1
        self.calls.append(("send", chat_id, text, keyboard))
        return self._next_id

    def edit(self, chat_id, message_id, text, keyboard=None):
        self.calls.append(("edit", chat_id, text, keyboard))

    def download_voice(self, file_id):
        return b"OGG"

    def answer_callback(self, callback_id, text=""):
        self.calls.append(("answer", callback_id, text))

    def texts(self):
        return [c[2] for c in self.calls if c[0] in ("send", "edit")]


class FakeFactory:
    def __init__(self):
        self.enqueued, self.cancelled = [], []
        self.started = False
        self.worker_state = {"generation": False, "agent_online": False, "last_seen": "2026-10-19T23:10:58",
                             "lanes": [{"lane": "desktop-a1", "seen_at": "2026-10-19T23:10:58", "online": False}],
                             "commands": {}}

    def hint(self):
        return {"brands": ["Ресанта", "Daichi"], "sources": ["splithub"]}

    def resolve(self, action):
        return {"total_matches": 57, "items": []}

    def enqueue(self, action):
        self.enqueued.append(action)
        return {"accepted": [f"k{i}" for i in range(action["count"] or 1)], "items": []}

    def cancel(self, keys):
        self.cancelled.append(keys)
        return {"cancelled": len(keys)}

    def status(self, since):
        return {"by_status": {"new": 3}}

    def workers(self):
        return self.worker_state

    def start_workers(self):
        self.started = True
        return {"generation": True, "commanded": ["agent_command"], "requested_run": True}



@pytest.fixture
def deps(tmp_path):
    store = Store(tmp_path / "o.db")
    store.heartbeat(NOW)
    return Deps(store=store, tg=FakeTg(), transcriber=lambda ogg, hint: "поставь стабилизаторы",
                factory=FakeFactory(), owner_chat_id=OWNER, clock=lambda: NOW)


def _msg(text=None, voice=False, chat=OWNER):
    m = {"message_id": 5, "chat": {"id": chat}, "from": {"id": chat}}
    if voice:
        m["voice"] = {"file_id": "F1", "duration": 40}
    else:
        m["text"] = text
    return {"update_id": 1, "message": m}


def _cb(data, chat=OWNER):
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data, "from": {"id": chat},
                                               "message": {"message_id": 9, "chat": {"id": chat}}}}


def test_ignores_strangers(deps):
    bot.handle_update(_msg("привет", chat=999), deps)
    bot.handle_update(_cb("c:1", chat=999), deps)
    assert deps.store.count_new() == 0
    assert deps.tg.calls == []


def test_text_goes_to_queue(deps):
    bot.handle_update(_msg("что сегодня поставлено?"), deps)
    assert deps.store.claim_next(NOW)["text"] == "что сегодня поставлено?"


def test_voice_echo_then_queue(deps):
    bot.handle_update(_msg(voice=True), deps)
    assert deps.tg.texts()[:2] == ["🎙 Расшифровываю…", "🎙 Услышал: поставь стабилизаторы"]
    item = deps.store.claim_next(NOW)
    assert (item["kind"], item["text"]) == ("voice", "поставь стабилизаторы")


@pytest.mark.parametrize("transcriber", [lambda o, h: "  ", lambda o, h: (_ for _ in ()).throw(RuntimeError("ffmpeg"))])
def test_failed_or_empty_transcript_not_queued(deps, transcriber):
    deps.transcriber = transcriber
    bot.handle_update(_msg(voice=True), deps)
    assert "Не разобрал" in deps.tg.texts()[-1]
    assert deps.store.count_new() == 0


def test_offline_notice_when_heartbeat_stale(deps):
    deps.store.heartbeat(NOW - 500)
    bot.handle_update(_msg("поставь"), deps)
    assert deps.tg.texts()[-1] == "Принял, разберу, когда ПК будет на связи"


def test_no_offline_notice_when_heartbeat_fresh(deps):
    bot.handle_update(_msg("поставь"), deps)
    assert deps.tg.calls == []


def test_cancel_callback_cancels_logged_keys(deps):
    log_id = deps.store.log_action(1, "enqueue", {"category": "x"}, {"accepted": ["k1", "k2"]})
    bot.handle_update(_cb(f"c:{log_id}"), deps)
    assert deps.factory.cancelled == [["k1", "k2"]]
    assert "Отменено: 2" in deps.tg.texts()[-1]


def test_ask_callback_first_n_enqueues_n_once(deps):
    action = {"type": "enqueue_products", "category": "стабилизаторы", "count": None, "quotas": {},
              "source": None, "at": None, "day": None}
    ask_id = deps.store.save_ask(1, action, ["Первые 40", "Все 57", "Отмена"])
    bot.handle_update(_cb(f"a:{ask_id}:0"), deps)
    bot.handle_update(_cb(f"a:{ask_id}:0"), deps)
    assert [a["count"] for a in deps.factory.enqueued] == [40]


def test_build_hint_uses_catalog_only():
    hint = bot.build_hint({"brands": ["Ресанта", "Daichi"], "sources": ["splithub"]})
    assert "Ресанта, Daichi" in hint and "splithub" in hint
    assert "Indesit" not in hint


def test_menu_has_status_report_and_task_wizard_link(deps):
    bot.handle_update(_msg("/menu"), deps)
    keyboard = deps.tg.calls[-1][3]
    buttons = [b for row in keyboard for b in row]
    assert [b.get("callback_data") for b in buttons[:2]] == ["m:status", "m:report"]
    assert buttons[2]["url"] == "https://t.me/Sendpr1ce_bot"


def test_status_works_without_pc(deps):
    deps.store.heartbeat(NOW - 10_000)
    bot.handle_update(_msg("/status"), deps)
    assert "new: 3" in deps.tg.texts()[-1] and "ПК" in deps.tg.texts()[-1]
    assert deps.store.count_new() == 0


def test_offline_alert_once_per_12h_only_with_pending(deps):
    deps.store.heartbeat(NOW - 13 * 3600)
    bot.offline_alert(deps, NOW)
    assert deps.tg.calls == []
    deps.store.add_message(1, "text", "ждёт")
    bot.offline_alert(deps, NOW)
    bot.offline_alert(deps, NOW + 3600)
    alerts = [t for t in deps.tg.texts() if "больше 12 часов" in t]
    assert len(alerts) == 1


def test_telegram_api_send_builds_inline_keyboard():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    api = TelegramApi("TOK", httpx.Client(transport=httpx.MockTransport(handler)))
    mid = api.send(OWNER, "привет", [[{"text": "Отменить", "callback_data": "c:1"}]])
    assert mid == 77 and seen["path"] == "/botTOK/sendMessage"
    assert seen["body"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "c:1"


def test_same_update_twice_queued_once(deps):
    bot.handle_update(_msg("поставь"), deps)
    bot.handle_update(_msg("поставь"), deps)
    assert deps.store.count_new() == 1


def test_process_updates_persists_offset(deps):
    bot.process_updates([{**_msg("раз"), "update_id": 41}, {**_msg("два"), "update_id": 42}], deps)
    assert deps.store.get_meta("tg_offset") == "43"


def test_cancel_works_even_if_answer_callback_fails(deps):
    def broken(*a, **k):
        raise RuntimeError("query is too old")
    deps.tg.answer_callback = broken
    log_id = deps.store.log_action(1, "enqueue", {"category": "x"}, {"accepted": ["k1"]})
    bot.handle_update(_cb(f"c:{log_id}"), deps)
    assert deps.factory.cancelled == [["k1"]]


def test_start_button_starts_workers(deps):
    bot.handle_update(_cb("w:start"), deps)
    assert deps.factory.started is True
    assert "Запуск" in deps.tg.texts()[-1]


def test_status_shows_content_agent_state(deps):
    bot.handle_update(_msg("/status"), deps)
    assert "генерация выключена" in deps.tg.texts()[-1]


def test_status_offers_start_button_when_agent_not_ready(deps):
    bot.handle_update(_msg("/status"), deps)
    keyboard = deps.tg.calls[-1][3]
    assert keyboard and keyboard[0][0]["callback_data"] == "w:start"


def test_status_has_no_start_button_when_agent_ready(deps):
    deps.factory.worker_state = {"generation": True, "agent_online": True, "last_seen": None, "lanes": [], "commands": {}}
    bot.handle_update(_msg("/status"), deps)
    assert not deps.tg.calls[-1][3]
