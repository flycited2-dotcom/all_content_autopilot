import json
import threading
from datetime import datetime
from types import SimpleNamespace

from listener import brain, session
from listener.__main__ import process_once
from listener.editorial import EditorialWorker

SANDBOX = ["--setting-sources", "project", "--settings", "scripts/headless-settings.json", "--strict-mcp-config"]


def test_day_session_resets_at_4am(tmp_path):
    state = tmp_path / "session.json"
    a, new_a = session.day_session(datetime(2026, 10, 21, 3, 59), state)
    b, new_b = session.day_session(datetime(2026, 10, 21, 4, 1), state)
    c, new_c = session.day_session(datetime(2026, 10, 21, 23, 0), state)
    assert a != b and b == c
    assert (new_a, new_b, new_c) == (True, True, False)


def _claude(results):
    """Фейковый runner: по очереди отдаёт заготовленные (код, result)."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        code, result = results.pop(0)
        return SimpleNamespace(returncode=code, stdout=json.dumps({"result": result}), stderr="")
    return run, calls


def test_run_turn_uses_resume_for_existing_session(tmp_path):
    run, calls = _claude([(0, '{"reply": "ок", "actions": []}')])
    out = brain.run_turn({"message": {"id": 5, "text": "привет"}, "context": {}}, ("u-1", False),
                         runner=run, root=tmp_path)
    assert out == {"reply": "ок", "actions": []}
    cmd, kwargs = calls[0]
    assert cmd[cmd.index("--resume") + 1] == "u-1"
    for flag in SANDBOX:
        assert flag in cmd
    assert kwargs["stdin"] is not None                       # stdin закрыт, иначе claude ждёт ввод
    assert json.loads((tmp_path / "runs" / "chat" / "5.json").read_text(encoding="utf-8"))["message"]["text"] == "привет"


def test_run_turn_starts_session_when_resume_fails(tmp_path):
    run, calls = _claude([(1, ""), (0, '{"reply": "ок", "actions": []}')])
    brain.run_turn({"message": {"id": 6, "text": "x"}, "context": {}}, ("u-2", False), runner=run, root=tmp_path)
    assert "--session-id" in calls[1][0]


def test_run_turn_falls_back_on_garbage(tmp_path):
    run, _ = _claude([(0, "не json")])
    out = brain.run_turn({"message": {"id": 7, "text": "x"}, "context": {}}, ("u-3", True), runner=run, root=tmp_path)
    assert out["actions"] == [] and "Не смог разобрать" in out["reply"]


class FakeApi:
    def __init__(self, messages):
        self.messages = list(messages)
        self.intents, self.replies = [], []

    def next(self):
        return {"message": self.messages.pop(0) if self.messages else None, "context": {"hint": {}, "today": []}}

    def intent(self, message_id, intent):
        self.intents.append((message_id, intent))
        return {"ok": True, "editorial": intent.get("_editorial", [])}

    def reply(self, text, keyboard=None):
        self.replies.append(text)


def test_editorial_does_not_block_chat(tmp_path):
    gate = threading.Event()
    started = threading.Event()

    def slow_runner(cmd, **kwargs):
        started.set()
        gate.wait(5)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    api = FakeApi([{"id": 1, "text": "сделай пост"}, {"id": 2, "text": "статус?"}])
    worker = EditorialWorker(slow_runner, api.reply, root=tmp_path)
    turn = lambda payload, sess: {"reply": "ок", "actions": [],
                                  "_editorial": [{"type": "run_editorial", "brief": "насосы", "channels": []}]
                                  if payload["message"]["id"] == 1 else []}
    process_once(api, worker, turn, ("u", False))
    assert started.wait(2)
    process_once(api, worker, turn, ("u", False))           # редакционный прогон ещё идёт
    assert [m for m, _ in api.intents] == [1, 2]
    gate.set()
    worker.join_idle(5)


def test_editorial_posts_summary_and_drafts(tmp_path):
    day = tmp_path / "runs" / "2026-10-21"

    def runner(cmd, **kw):                                 # прогон /morning-run пишет итог, черновики, вердикты
        (day / "drafts").mkdir(parents=True, exist_ok=True)
        (day / "verdicts").mkdir(exist_ok=True)
        (day / "summary.md").write_text("Готово 2, прошли 1", encoding="utf-8")
        (day / "drafts" / "a.json").write_text(json.dumps({"id": "a", "title": "Насосы", "text": "Текст А"}, ensure_ascii=False), encoding="utf-8")
        (day / "drafts" / "b.json").write_text(json.dumps({"id": "b", "title": "Плохой", "text": "Текст Б"}, ensure_ascii=False), encoding="utf-8")
        (day / "verdicts" / "a.json").write_text('{"id": "a", "ok": true}', encoding="utf-8")
        (day / "verdicts" / "b.json").write_text('{"id": "b", "ok": false}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    posted = []
    worker = EditorialWorker(runner, lambda text, keyboard=None: posted.append(text), root=tmp_path,
                             today=lambda: "2026-10-21")
    worker.submit({"type": "run_editorial", "brief": "насосы", "channels": []})
    worker.join_idle(5)
    assert posted[0] == "Готово 2, прошли 1"
    assert len(posted) == 2 and "Текст А" in posted[1] and "id: a" in posted[1]
    assert (tmp_path / "inbox" / "brief.md").read_text(encoding="utf-8").strip() == "насосы"


def test_second_run_same_day_posts_only_new_drafts(tmp_path):
    day = tmp_path / "runs" / "2026-10-21"
    (day / "drafts").mkdir(parents=True)
    (day / "verdicts").mkdir()
    (day / "drafts" / "old.json").write_text('{"id": "old", "title": "Утро", "text": "Старый"}', encoding="utf-8")
    (day / "verdicts" / "old.json").write_text('{"ok": true}', encoding="utf-8")

    def runner(cmd, **kw):                                 # прогон создаёт итог и новый принятый черновик
        (day / "summary.md").write_text("Вечерний итог", encoding="utf-8")
        (day / "drafts" / "new.json").write_text('{"id": "new", "title": "Вечер", "text": "Новый"}', encoding="utf-8")
        (day / "verdicts" / "new.json").write_text('{"ok": true}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    posted = []
    worker = EditorialWorker(runner, lambda text, keyboard=None: posted.append(text), root=tmp_path,
                             today=lambda: "2026-10-21")
    worker.submit({"type": "run_editorial", "brief": "ещё пост", "channels": []})
    worker.join_idle(5)
    assert any("Новый" in p for p in posted)
    assert not any("Старый" in p for p in posted)


def test_run_with_kill_enforces_timeout():
    import subprocess
    import sys
    import time
    from listener.brain import run_with_kill
    started = time.time()
    try:
        run_with_kill([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("ожидался TimeoutExpired")
    assert time.time() - started < 10


def test_worker_survives_post_failure(tmp_path):
    def post(text, keyboard=None):
        raise RuntimeError("туннель упал")
    ran = []
    worker = EditorialWorker(lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0, stdout="{}", stderr=""),
                             post, root=tmp_path, today=lambda: "2026-10-21")
    worker.submit({"type": "run_editorial", "brief": "раз", "channels": []})
    worker.join_idle(5)
    worker.submit({"type": "run_editorial", "brief": "два", "channels": []})
    worker.join_idle(5)
    assert len(ran) == 2


def test_failed_editorial_run_reports_error_not_stale_summary(tmp_path):
    day = tmp_path / "runs" / "2026-10-21"
    day.mkdir(parents=True)
    (day / "summary.md").write_text("Утренний итог", encoding="utf-8")
    posted = []
    worker = EditorialWorker(lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="limit"),
                             lambda text, keyboard=None: posted.append(text), root=tmp_path, today=lambda: "2026-10-21")
    worker.submit({"type": "run_editorial", "brief": "x", "channels": []})
    worker.join_idle(5)
    assert posted and "❌" in posted[0] and "Утренний итог" not in posted[0]


def test_revise_finds_draft_from_previous_day(tmp_path):
    old = tmp_path / "runs" / "2026-10-20" / "drafts"
    old.mkdir(parents=True)
    (old / "nasosy.json").write_text('{"id": "nasosy", "title": "t", "text": "x"}', encoding="utf-8")
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        (old / "nasosy-v2.json").write_text('{"id": "nasosy-v2", "title": "Новый", "text": "Короче"}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    posted = []
    worker = EditorialWorker(runner, lambda text, keyboard=None: posted.append(text), root=tmp_path, today=lambda: "2026-10-21")
    worker.submit({"type": "revise_editorial", "draft_id": "nasosy", "notes": "короче"})
    worker.join_idle(5)
    assert "/revise-draft 2026-10-20 nasosy" in seen[0][2]
    assert "Короче" in posted[-1]
