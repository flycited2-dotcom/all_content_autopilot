import json
from datetime import datetime

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

    def send(self, chat_id, text, keyboard=None, reply_keyboard=None):
        self._next_id += 1
        self.calls.append(("send", chat_id, text, keyboard, reply_keyboard))
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
    assert deps.store.count_new() == 0
    history = deps.store.dialogue_before(999)
    assert len(history) == 2
    assert "Первые 40" in history[0]["content"] and "Поставил" in history[1]["content"]


def test_quantity_cancel_is_in_dialogue_without_enqueuing(deps):
    action = {"type": "enqueue_products", "category": "холодильники"}
    ask_id = deps.store.save_ask(1, action, ["Первые 40", "Все 47", "Отмена"])
    bot.handle_update(_cb(f"a:{ask_id}:2"), deps)
    assert deps.factory.enqueued == [] and deps.store.count_new() == 0
    assert deps.store.dialogue_before(999)[-1]["content"] == "Хорошо, не ставлю."


def test_build_hint_uses_catalog_only():
    hint = bot.build_hint({"brands": ["Ресанта", "Daichi"], "sources": ["splithub"]})
    assert "Ресанта, Daichi" in hint and "splithub" in hint
    assert "Indesit" not in hint


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
    assert deps.tg.calls[-1][3] == [[{"text": "🗂 Лента", "callback_data": "f:show"}]]   # без «Запустить»


def test_menu_installs_persistent_reply_keyboard(deps):
    bot.handle_update(_msg("/start"), deps)
    reply_keyboard = deps.tg.calls[-1][4]
    assert [b for row in reply_keyboard for b in row] == ["📋 Статус", "🧾 Сводка", '📚 Каталоги и товары',
                                                        "🛠 Мастер задач", '📝 Ошибка в карточке']


def test_reply_keyboard_buttons_work_without_queue(deps):
    bot.handle_update(_msg("📋 Статус"), deps)
    assert "📋 Статус" in deps.tg.texts()[-1] and "Конвейер" in deps.tg.texts()[-1]
    bot.handle_update({"update_id": 3, "message": {"message_id": 6, "chat": {"id": OWNER}, "from": {"id": OWNER},
                                                    "text": "🛠 Мастер задач"}}, deps)
    assert deps.tg.calls[-1][3][0][0]["url"] == "https://t.me/Sendpr1ce_bot"
    assert deps.store.count_new() == 0


def test_telegram_api_reply_keyboard_and_commands():
    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1} if "send" in req.url.path else True})

    api = TelegramApi("TOK", httpx.Client(transport=httpx.MockTransport(handler)))
    api.send(OWNER, "x", reply_keyboard=[["📋 Статус"]])
    markup = seen[-1][1]["reply_markup"]
    assert markup["keyboard"] == [[{"text": "📋 Статус"}]] and markup["is_persistent"] and markup["resize_keyboard"]
    api.set_commands([("status", "Статус")])
    assert seen[-1][0] == "/botTOK/setMyCommands"
    assert seen[-1][1]["commands"] == [{"command": "status", "description": "Статус"}]


def test_reply_to_draft_carries_draft_id_into_queue(deps):
    update = _msg("короче и без примеров про Пермь")
    update["message"]["reply_to_message"] = {"message_id": 99, "text": "📝 Тепловой насос\n\nТекст\n\nid: heat-pumps-basics-expert"}
    bot.handle_update(update, deps)
    queued = deps.store.claim_next(NOW)["text"]
    assert "короче и без примеров про Пермь" in queued
    assert "id: heat-pumps-basics-expert" in queued


def test_voice_reply_to_draft_carries_draft_id(deps):
    update = _msg(voice=True)
    update["message"]["reply_to_message"] = {"message_id": 99, "text": "📝 Заголовок\n\nid: nasosy-v2"}
    bot.handle_update(update, deps)
    assert "id: nasosy-v2" in deps.store.claim_next(NOW)["text"]


def test_default_telegram_client_is_resilient_to_connect_timeouts():
    # как publish/telegram.py content-factory: связь VPS↔Telegram периодически не устанавливается
    from orchestrator_bot.telegram_api import telegram_client
    client = telegram_client()
    assert client.timeout.connect <= 5
    assert client.timeout.read >= 30                       # long-poll getUpdates(timeout=25) помещается
    assert client._transport._pool._retries >= 5           # повтор только установки соединения — без дублей


def test_cancel_explains_items_already_in_preview(deps):
    deps.factory.cancel = lambda keys: {"cancelled": 0, "not_cancelled": [{"key": k, "status": "preview"} for k in keys]}
    log_id = deps.store.log_action(1, "enqueue", {"category": "холодильники"}, {"accepted": ["k1", "k2"]})
    bot.handle_update(_cb(f"c:{log_id}"), deps)
    text = deps.tg.texts()[-1]
    assert "Отменено: 0" in text
    assert "уже на превью" in text and "❌" in text and "2" in text


def _ready_draft(deps):
    deps.store.add_draft({"id": "heat-v4", "channel": "vk_editorial", "title": "Насосы", "text": "Т",
                          "visual_prompt": "насос"}, "heat")
    row = deps.store.get_draft("heat-v4")
    deps.store.update_draft(row["n"], status="ready", image_path="/o/a.png")
    submitted = []
    deps.factory.visual_submit = lambda scene, key, new=False: submitted.append((scene, key)) or {"job_id": 9}
    deps.tg.clear_buttons = lambda chat, mid: None
    return row["n"], submitted


def test_other_picture_button_takes_next_message_as_wish(deps):
    n, submitted = _ready_draft(deps)
    bot.handle_update(_cb(f"d:v:{n}"), deps)
    assert "что изменить в картинке" in deps.tg.texts()[-1]
    bot.handle_update(_msg("блок чистый, дом зимой"), deps)
    assert deps.store.count_new() == 0                      # Claude не нужен — перерисовывает сервер
    assert "блок чистый, дом зимой" in submitted[0][0] and submitted[0][1].startswith("orch-heat-v4-r")
    assert "Текст не меняю" in deps.tg.texts()[-1]
    assert deps.store.get_draft(n)["status"] == "visual"


def test_edit_text_button_routes_next_message_to_revision(deps):
    n, _ = _ready_draft(deps)
    bot.handle_update(_cb(f"d:e:{n}"), deps)
    bot.handle_update(_msg("короче вдвое"), deps)
    queued = deps.store.claim_next(NOW)
    assert queued["text"] == "короче вдвое\n[ответ на черновик id: heat-v4]"
    bot.handle_update({"update_id": 3, "message": {"message_id": 6, "chat": {"id": OWNER}, "from": {"id": OWNER},
                                                   "text": "а теперь просто вопрос"}}, deps)
    assert deps.store.claim_next(NOW)["text"] == "а теперь просто вопрос"   # ожидание одноразовое


def test_restore_button_after_accidental_cancel(deps):
    log_id = deps.store.log_action(1, "enqueue", {"type": "enqueue_products", "category": "кондиционеры", "count": 2,
                                                  "quotas": {}, "at": None, "day": None}, {"accepted": ["k0", "k1"]})
    bot.handle_update(_cb(f"c:{log_id}"), deps)
    sent = [c for c in deps.tg.calls if c[0] == "send"][-1]
    assert sent[3] == [[{"text": "↩️ Вернуть", "callback_data": f"u:{log_id}"}]]
    bot.handle_update(_cb(f"u:{log_id}"), deps)
    assert deps.factory.enqueued[-1]["category"] == "кондиционеры"
    assert "Поставил" in deps.tg.texts()[-1]


class FeedFactory(FakeFactory):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.status_by_call = []

    def feed(self):
        return {"tg": {"fresh": 3, "stale": 553, "pending": 560}, "vk_review": 8,
                "review_url": None, "fresh_hours": 72, "stale_days": 7}

    def approve_fresh(self):
        self.calls.append("fresh")
        return {"published": ["a", "b", "c"], "failed": []}

    def batch_status(self, keys):
        return self.status_by_call.pop(0)

    def publish_keys(self, keys):
        self.calls.append(("publish", tuple(keys)))
        return {"published": list(keys), "failed": []}


def test_feed_digest_buttons_and_actions(deps):
    deps.factory = FeedFactory()
    bot.handle_update(_cb("f:show"), deps)
    send = [c for c in deps.tg.calls if c[0] == "send"][-1]
    assert "Свежих товарных превью в группе проверки: 3" in send[2] and "ВК-план на проверке: 8" in send[2]
    assert [row[0].get("callback_data") for row in send[3]] == ["f:tg", "f:old", "f:vk"]
    bot.handle_update(_cb("f:tg"), deps)
    assert deps.factory.calls == ["fresh"] and "опубликовано в канале 3" in deps.tg.texts()[-1]


def _batch(deps, at=None, day=None):
    from orchestrator_bot import feed
    log_id = deps.store.log_action(1, "enqueue", {"category": "холодильники"}, {"accepted": ["k1", "k2"]})
    deps.store.add_batch(log_id, "холодильники", ["k1", "k2"], feed.publish_at({"at": at, "day": day}))
    return log_id


def test_batch_ready_notice_then_publish_whole_batch_now(deps):
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    log_id = _batch(deps)
    deps.factory.status_by_call = [{"total": 2, "ready": 1, "in_work": 1, "published": 0, "dropped": 0},
                                   {"total": 2, "ready": 2, "in_work": 0, "published": 0, "dropped": 0}]
    feed.tick(deps, NOW)
    assert not any("Партия" in t for t in deps.tg.texts())        # ещё генерируется — молчим
    feed.tick(deps, NOW + feed.POLL_EVERY + 1)
    notice = [c for c in deps.tg.calls if c[0] == "send"][-1]
    assert "📦 Партия «холодильники» готова: 2 карточек" in notice[2]
    assert notice[3][0][0] == {"text": "✅ Опубликовать пачкой сейчас", "callback_data": f"b:p:{log_id}"}
    bot.handle_update(_cb(f"b:p:{log_id}"), deps)
    bot.handle_update(_cb(f"b:p:{log_id}"), deps)                   # повторное нажатие — не второй раз
    assert deps.factory.calls == [("publish", ("k1", "k2"))]
    assert "Партия «холодильники»: опубликовано в канале 2" in deps.tg.texts()[-2]


def test_batch_with_planned_time_waits_for_it(deps):
    from datetime import datetime
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    deps.clock = lambda: datetime(2026, 10, 22, 9, 0).timestamp()
    log_id = _batch(deps, at="12:00", day="2026-10-22")
    deps.store.move_batch(log_id, ("open",), "ready")
    bot.handle_update(_cb(f"b:p:{log_id}"), deps)
    assert "выйдет 22.10 в 12:00" in deps.tg.texts()[-1] and deps.factory.calls == []
    feed.tick(deps, datetime(2026, 10, 22, 11, 59).timestamp())
    assert deps.factory.calls == []
    feed.tick(deps, datetime(2026, 10, 22, 12, 0).timestamp())
    assert deps.factory.calls == [("publish", ("k1", "k2"))]


def test_batch_without_ready_cards_reports_once_and_never_publishes(deps):
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    log_id = _batch(deps)
    deps.factory.status_by_call = [{'total': 2, 'ready': 0, 'in_work': 0,
                                    'published': 0, 'dropped': 2}]
    feed.tick(deps, NOW)
    feed.tick(deps, NOW + feed.POLL_EVERY + 1)
    notices = [text for text in deps.tg.texts() if 'без готовых превью' in text]
    assert len(notices) == 1 and 'Остальные карточки: 2' in notices[0]
    assert deps.store.get_batch(log_id)['status'] == 'failed'
    assert not deps.factory.calls


def test_partial_batch_returns_for_review_after_audit_recovery_without_auto_publish(deps):
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    log_id = _batch(deps)
    deps.store.set_meta('digest_day', datetime.fromtimestamp(NOW).date().isoformat())
    partial = {'total': 2, 'ready': 1, 'in_work': 0, 'published': 0, 'dropped': 1}
    deps.factory.status_by_call = [partial,
        {'total': 2, 'ready': 1, 'in_work': 1, 'published': 0, 'dropped': 0},
        {'total': 2, 'ready': 2, 'in_work': 0, 'published': 0, 'dropped': 0}]
    feed.tick(deps, NOW)
    assert any('1 из 2 карточек' in text and 'Остальные карточки: 1' in text for text in deps.tg.texts())
    assert deps.store.get_batch(log_id)['status'] == 'ready'
    deps.store.reopen_recovered_batches('k2', recovery_id='audit:0')
    feed.tick(deps, NOW + feed.POLL_EVERY + 1)
    assert deps.store.get_batch(log_id)['status'] == 'open'
    assert not deps.factory.calls
    feed.tick(deps, NOW + 2 * feed.POLL_EVERY + 2)
    assert deps.store.get_batch(log_id)['status'] == 'ready'
    assert len([text for text in deps.tg.texts() if 'Партия «холодильники» готова: 2' in text]) == 1
    assert not deps.factory.calls
    assert deps.store.reopen_recovered_batches('k2', recovery_id='audit:0') == 0
    feed.tick(deps, NOW + 3 * feed.POLL_EVERY + 3)
    assert len([text for text in deps.tg.texts() if 'Партия «холодильники» готова: 2' in text]) == 1


@pytest.mark.parametrize('ready', [0, 1])
def test_stale_batch_poll_does_not_report_a_recovered_job_as_finished(deps, ready):
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    log_id = _batch(deps)
    deps.store.set_meta('digest_day', datetime.fromtimestamp(NOW).date().isoformat())

    def stale_status(keys):
        deps.store.reopen_recovered_batches('k2', recovery_id='audit:0')
        return {'total': 2, 'ready': ready, 'in_work': 0, 'published': 0, 'dropped': 2 - ready}

    deps.factory.batch_status = stale_status
    feed.tick(deps, NOW)
    assert deps.store.get_batch(log_id)['status'] == 'open'
    assert not deps.tg.texts()
    assert not deps.factory.calls


def test_owner_can_cancel_reopened_batch_using_its_previous_review_button(deps):
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    log_id = _batch(deps)
    deps.store.move_batch(log_id, ('open',), 'ready')
    deps.store.reopen_recovered_batches('k2', recovery_id='audit:0')
    feed.batch_action(deps, OWNER, 'p', log_id)
    assert not deps.factory.calls
    feed.batch_action(deps, OWNER, 'x', log_id)
    assert deps.store.get_batch(log_id)['status'] == 'closed'
    assert deps.store.reopen_recovered_batches('k2', recovery_id='audit:1') == 0


@pytest.mark.parametrize('ready,published,dropped', [(1, 1, 4), (0, 1, 5), (5, 1, 0)])
def test_batch_notice_accounts_for_published_cards_without_calling_every_other_item_an_error(
        deps, ready, published, dropped):
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    log_id = _batch(deps)
    deps.store.set_meta('digest_day', datetime.fromtimestamp(NOW).date().isoformat())
    deps.factory.status_by_call = [{'total': 6, 'ready': ready, 'in_work': 0,
                                    'published': published, 'dropped': dropped}]
    feed.tick(deps, NOW)
    feed.tick(deps, NOW + feed.POLL_EVERY + 1)
    assert len(deps.tg.texts()) == 1
    text = deps.tg.texts()[0]
    assert f'{ready} из 6 карточек' in text
    assert f'Уже опубликовано: {published}' in text
    assert f'Остальные карточки: {dropped}' in text
    # dropped also includes expired, rejected and cancelled items.
    assert 'Ошибок или удержаний:' not in text
    assert deps.store.get_batch(log_id)['status'] == ('ready' if ready else 'failed')
    assert not deps.factory.calls


def test_batch_with_all_cards_already_published_is_not_reported_failed_or_published_again(deps):
    from orchestrator_bot import feed
    deps.factory = FeedFactory()
    log_id = _batch(deps)
    deps.store.set_meta('digest_day', datetime.fromtimestamp(NOW).date().isoformat())
    deps.factory.status_by_call = [{'total': 2, 'ready': 0, 'in_work': 0, 'published': 2, 'dropped': 0}]
    feed.tick(deps, NOW)
    feed.tick(deps, NOW + feed.POLL_EVERY + 1)
    assert len(deps.tg.texts()) == 1
    assert 'уже опубликована: 2 из 2 карточек' in deps.tg.texts()[0]
    assert deps.store.get_batch(log_id)['status'] == 'published'
    assert not deps.factory.calls


def test_cancel_closes_batch(deps):
    log_id = _batch(deps)
    bot.handle_update(_cb(f"c:{log_id}"), deps)
    assert deps.store.get_batch(log_id)["status"] == "closed"
