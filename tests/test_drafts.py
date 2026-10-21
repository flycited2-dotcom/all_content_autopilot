"""Этап 3: черновик → картинка → превью с кнопками → ✅ публикация (один раз)."""
from orchestrator_bot import drafts
from orchestrator_bot.store import Store

CHAT = 1


class FakeTg:
    def __init__(self):
        self.sent, self.photos, self.cleared = [], [], []

    def send(self, chat_id, text, keyboard=None, reply_keyboard=None):
        self.sent.append((text, keyboard))
        return 100 + len(self.sent)

    def send_photo(self, chat_id, path, caption="", keyboard=None):
        self.photos.append((path, caption))
        return 500

    def clear_buttons(self, chat_id, message_id):
        self.cleared.append(message_id)


class FakeFactory:
    def __init__(self):
        self.status = {"status": "pending", "path": None, "error": None}
        self.submitted, self.published = [], []
        self.publish_result = {"ok": True, "message_id": 42, "url": "https://t.me/c/1/42"}

    def visual_submit(self, scene, request_key, new=False):
        self.submitted.append((scene, request_key, new))
        return {"job_id": 7}

    def visual_status(self, job_id):
        return self.status

    def publish_tg(self, key, caption, image):
        self.published.append(("tg", key, caption, image))
        return self.publish_result

    def plan_vk(self, key, caption, image):
        self.published.append(("vk", key, caption, image))
        return {"ok": True, "plan_id": 3, "due_at": 1793176200}


def _draft(id_="heat-pumps", channel="tg_editorial"):
    return {"id": id_, "channel": channel, "title": "Тепловые насосы", "text": "Текст поста",
            "visual_prompt": "насос у дома зимой"}


def _setup(tmp_path, draft=None):
    store, factory, tg = Store(tmp_path / "o.db"), FakeFactory(), FakeTg()
    drafts.submit(store, factory, tg, CHAT, draft or _draft())
    return store, factory, tg


def test_submit_orders_picture_once(tmp_path):
    store, factory, tg = _setup(tmp_path)
    assert factory.submitted == [("насос у дома зимой", "orch-heat-pumps", False)]
    assert store.get_draft("heat-pumps")["status"] == "visual"
    assert "Рисую картинку" in tg.sent[0][0] and "id: heat-pumps" in tg.sent[0][0]
    assert drafts.submit(store, factory, tg, CHAT, _draft()) == {"ok": True, "new": False}
    assert len(factory.submitted) == 1


def test_ready_picture_sends_preview_with_buttons(tmp_path):
    store, factory, tg = _setup(tmp_path)
    drafts.poll(store, factory, tg, CHAT)
    assert tg.photos == []                                   # ещё рисуется
    factory.status = {"status": "done", "path": "/out/a.png", "error": None}
    drafts.poll(store, factory, tg, CHAT)
    assert tg.photos == [("/out/a.png", "Текст поста")]      # как будет в канале
    text, keyboard = tg.sent[-1]
    assert "id: heat-pumps" in text and keyboard[0][0]["callback_data"].startswith("d:p:")
    assert store.get_draft("heat-pumps")["status"] == "ready"


def test_publish_once_and_supersede_other_versions(tmp_path):
    store, factory, tg = _setup(tmp_path)
    drafts.submit(store, factory, tg, CHAT, _draft("heat-pumps-v2"))
    factory.status = {"status": "done", "path": "/out/a.png", "error": None}
    drafts.poll(store, factory, tg, CHAT)
    n = store.get_draft("heat-pumps-v2")["n"]
    drafts.publish(store, factory, tg, CHAT, n)
    assert factory.published == [("tg", "heat-pumps-v2", "Текст поста", "/out/a.png")]
    assert "https://t.me/c/1/42" in tg.sent[-1][0]
    assert store.get_draft("heat-pumps")["status"] == "superseded"
    drafts.publish(store, factory, tg, CHAT, n)               # повторное нажатие
    assert len(factory.published) == 1 and "уже опубликован" in tg.sent[-1][0]


def test_failed_publication_can_be_retried(tmp_path):
    store, factory, tg = _setup(tmp_path)
    factory.status = {"status": "done", "path": "/out/a.png", "error": None}
    drafts.poll(store, factory, tg, CHAT)
    factory.publish_result = {"ok": False, "error": "Bad Request"}
    n = store.get_draft("heat-pumps")["n"]
    drafts.publish(store, factory, tg, CHAT, n)
    assert "Не опубликовано: Bad Request" in tg.sent[-1][0]
    assert store.get_draft("heat-pumps")["status"] == "ready"


def test_vk_goes_to_plan_with_date(tmp_path):
    store, factory, tg = _setup(tmp_path, _draft("vk-post", "vk_editorial"))
    factory.status = {"status": "done", "path": "/out/v.png", "error": None}
    drafts.poll(store, factory, tg, CHAT)
    assert tg.photos == [("/out/v.png", "")] and "Текст поста" in tg.sent[-1][0]
    drafts.publish(store, factory, tg, CHAT, store.get_draft("vk-post")["n"])
    assert factory.published[0][0] == "vk" and "ВК-плане, выйдет" in tg.sent[-1][0]


def test_failed_picture_offers_retry(tmp_path):
    store, factory, tg = _setup(tmp_path)
    factory.status = {"status": "failed", "path": None, "error": "limit"}
    drafts.poll(store, factory, tg, CHAT)
    assert "не получилась: limit" in tg.sent[-1][0] and tg.sent[-1][1][0][0]["callback_data"].startswith("d:i:")
    drafts.retry_image(store, factory, tg, CHAT, store.get_draft("heat-pumps")["n"])
    assert factory.submitted[-1][1].startswith("orch-heat-pumps-r")
    assert factory.submitted[-1][2] is True and store.get_draft("heat-pumps")["status"] == "visual"


def test_reject_and_unknown_channel(tmp_path):
    store, factory, tg = _setup(tmp_path)
    drafts.reject(store, tg, CHAT, store.get_draft("heat-pumps")["n"])
    assert store.get_draft("heat-pumps")["status"] == "rejected"
    import pytest
    with pytest.raises(ValueError):
        drafts.submit(store, factory, tg, CHAT, _draft("x", "tg_products"))


def test_redo_visual_keeps_text_and_adds_wishes(tmp_path):
    store, factory, tg = _setup(tmp_path)
    factory.status = {"status": "done", "path": "/out/a.png", "error": None}
    drafts.poll(store, factory, tg, CHAT)
    reply = drafts.redo_visual(store, factory, "heat-pumps", "без грязи на блоке")
    assert "Текст не меняю" in reply
    assert "без грязи на блоке" in factory.submitted[-1][0] and factory.submitted[-1][2] is True
    row = store.get_draft("heat-pumps")
    assert row["status"] == "visual" and row["text"] == "Текст поста"
    assert "картинка ещё рисуется" in drafts.redo_visual(store, factory, "heat-pumps", "ещё")
