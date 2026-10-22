import json
import time
from types import SimpleNamespace

import pytest

from listener import editorial
from listener.editorial import EditorialWorker


DAY = "2026-10-22"


def eventually(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    assert check()


def action(brief="тема", key=None):
    value = {"type": "run_editorial", "brief": brief, "channels": []}
    if key:
        value["_job_key"] = key
    return value


def successful_runner(root, seen):
    def run(cmd, **kwargs):
        brief = (root / "inbox" / "brief.md").read_text(encoding="utf-8").strip()
        seen.append(brief)
        day = root / "runs" / DAY
        (day / "summary.md").write_text(f"Новый итог: {brief}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    return run


def test_offline_jobs_survive_restart_in_fifo_order(tmp_path):
    seen, posted = [], []
    worker = EditorialWorker(successful_runner(tmp_path, seen), posted.append, root=tmp_path,
                             today=lambda: DAY, claude_available=lambda: False, poll_seconds=0.01)
    worker.submit(action("первая", "21:0"))
    worker.submit(action("вторая", "21:1"))
    try:
        eventually(lambda: worker.marker.exists())
        eventually(lambda: any("сохранено" in text for text in posted))
        time.sleep(0.04)
        assert not seen and not worker.join_idle(0)
        assert sum("сохранено" in text for text in posted) == 1
        saved = json.loads(worker.queue_path.read_text(encoding="utf-8"))
        assert [job["brief"] for job in saved["jobs"]] == ["первая", "вторая"]
    finally:
        worker.close()
    resumed = EditorialWorker(successful_runner(tmp_path, seen), posted.append, root=tmp_path,
                              today=lambda: DAY, poll_seconds=0.01)
    try:
        assert resumed.join_idle(3)
        assert seen == ["первая", "вторая"]
        assert not resumed.marker.exists()
        assert json.loads(resumed.queue_path.read_text(encoding="utf-8")) == {
            "jobs": [], "done": ["21:0", "21:1"]}
    finally:
        resumed.close()


def test_confirmed_limit_keeps_job_and_never_posts_stale_summary(tmp_path):
    day = tmp_path / "runs" / DAY
    day.mkdir(parents=True)
    (day / "summary.md").write_text("УСТАРЕВШИЙ ИТОГ", encoding="utf-8")
    calls, posted, errors = [], [], []
    available = [True]
    success = successful_runner(tmp_path, calls)

    def run(cmd, **kwargs):
        if not errors:
            calls.append("limit")
            return SimpleNamespace(returncode=1, stdout=json.dumps({"is_error": True,
                "result": "You've hit your weekly limit · resets Oct 25, 6pm (Europe/Moscow)"}), stderr="")
        return success(cmd, **kwargs)

    def report(error):
        errors.append(error)
        available[0] = False

    worker = EditorialWorker(run, posted.append, root=tmp_path, today=lambda: DAY,
                             claude_available=lambda: available[0], on_provider_error=report,
                             poll_seconds=0.01, retry_delay=0.01)
    worker.submit(action("продолжить", "22:0"))
    try:
        eventually(lambda: errors)
        time.sleep(0.05)
        assert errors[0].kind == "usage_limit"
        assert calls == ["limit"] and worker.marker.exists()
        assert not worker.join_idle(0)
        assert not any("УСТАРЕВШИЙ ИТОГ" in text for text in posted)
        assert json.loads(worker.queue_path.read_text(encoding="utf-8"))["jobs"][0]["_job_key"] == "22:0"
        available[0] = True
        assert worker.join_idle(3)
        assert calls == ["limit", "продолжить"]
        assert "Новый итог: продолжить" in posted
        assert not any("УСТАРЕВШИЙ ИТОГ" in text for text in posted)
    finally:
        worker.close()


def test_job_keys_deduplicate_pending_and_completed_but_not_unkeyed_jobs(tmp_path):
    seen = []
    worker = EditorialWorker(successful_runner(tmp_path, seen), lambda text: None, root=tmp_path,
                             today=lambda: DAY, claude_available=lambda: False, poll_seconds=0.01)
    worker.submit(action("одна", "23:0"))
    worker.submit(action("одна", "23:0"))
    worker.submit(action("без ключа"))
    worker.submit(action("без ключа"))
    worker.close()
    assert len(json.loads(worker.queue_path.read_text(encoding="utf-8"))["jobs"]) == 3
    resumed = EditorialWorker(successful_runner(tmp_path, seen), lambda text: None, root=tmp_path,
                              today=lambda: DAY, poll_seconds=0.01)
    try:
        assert resumed.join_idle(3)
        resumed.submit(action("одна", "23:0"))
        time.sleep(0.04)
        assert seen == ["одна", "без ключа", "без ключа"]
    finally:
        resumed.close()
    another = EditorialWorker(successful_runner(tmp_path, seen), lambda text: None, root=tmp_path,
                              today=lambda: DAY, poll_seconds=0.01)
    try:
        another.submit(action("одна", "23:0"))
        time.sleep(0.04)
        assert seen == ["одна", "без ключа", "без ключа"]
    finally:
        another.close()


def test_corrupted_queue_is_not_silently_discarded(tmp_path):
    queue = tmp_path / "runs" / "chat" / "editorial-queue.json"
    queue.parent.mkdir(parents=True)
    queue.write_text('{"jobs": "broken"}', encoding="utf-8")
    with pytest.raises(ValueError, match="очередь"):
        EditorialWorker(lambda *args, **kwargs: None, lambda text: None, root=tmp_path)
    assert queue.read_text(encoding="utf-8") == '{"jobs": "broken"}'


def test_submit_does_not_accept_job_when_atomic_save_fails(tmp_path, monkeypatch):
    seen = []
    worker = EditorialWorker(successful_runner(tmp_path, seen), lambda text: None, root=tmp_path,
                             today=lambda: DAY, poll_seconds=0.01)

    def failed_save(path, value):
        raise OSError("disk full")

    monkeypatch.setattr(editorial, "write_json", failed_save)
    try:
        with pytest.raises(OSError):
            worker.submit(action("потерянная", "24:0"))
        assert worker.join_idle(0) and not seen
    finally:
        worker.close()


def test_completion_save_retry_does_not_repeat_model_call(tmp_path, monkeypatch):
    seen, failures = [], []
    real_write = editorial.write_json

    def flaky_write(path, value):
        if path.name == "editorial-queue.json" and not value["jobs"] and not failures:
            failures.append(True)
            raise OSError("temporary disk error")
        return real_write(path, value)

    monkeypatch.setattr(editorial, "write_json", flaky_write)
    worker = EditorialWorker(successful_runner(tmp_path, seen), lambda text: None, root=tmp_path,
                             today=lambda: DAY, poll_seconds=0.01)
    worker.submit(action("один вызов", "25:0"))
    try:
        assert worker.join_idle(3)
        assert failures and seen == ["один вызов"]
        assert json.loads(worker.queue_path.read_text(encoding="utf-8"))["done"] == ["25:0"]
    finally:
        worker.close()


def test_terminal_cli_failure_does_not_echo_stderr_secrets(tmp_path):
    posted = []
    worker = EditorialWorker(lambda cmd, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="secret-token"),
                             posted.append, root=tmp_path, today=lambda: DAY)
    worker.submit(action())
    try:
        assert worker.join_idle(3)
        assert any("❌" in text for text in posted)
        assert not any("secret-token" in text for text in posted)
    finally:
        worker.close()


def test_revision_does_not_redeliver_a_stale_version_after_a_noop(tmp_path):
    drafts = tmp_path / "runs" / DAY / "drafts"
    drafts.mkdir(parents=True)
    (drafts / "topic.json").write_text('{"id":"topic","text":"initial"}', encoding="utf-8")
    (drafts / "topic-v2.json").write_text('{"id":"topic-v2","text":"STALE DRAFT"}', encoding="utf-8")
    posted, delivered = [], []
    worker = EditorialWorker(lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
                             posted.append, root=tmp_path, today=lambda: DAY, submit_draft=delivered.append)
    worker.submit({"type": "revise_editorial", "draft_id": "topic", "notes": "короче", "_job_key": "26:0"})
    try:
        assert worker.join_idle(3)
        assert not delivered
        assert any("Новая версия" in text for text in posted)
        assert not any("STALE DRAFT" in text for text in posted)
    finally:
        worker.close()


def test_delivery_survives_restart_without_a_second_generation(tmp_path):
    calls, posted, attempts, delivered = [], [], [], []

    def generate(cmd, **kwargs):
        calls.append(cmd)
        day = tmp_path / "runs" / DAY
        (day / "drafts").mkdir(parents=True, exist_ok=True)
        (day / "verdicts").mkdir(exist_ok=True)
        (day / "summary.md").write_text("Готово", encoding="utf-8")
        (day / "drafts" / "new.json").write_text('{"id":"new","text":"Принятый текст"}', encoding="utf-8")
        (day / "verdicts" / "new.json").write_text('{"ok":true}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def offline(draft):
        attempts.append(draft)
        raise ConnectionError("server offline")

    worker = EditorialWorker(generate, posted.append, root=tmp_path, today=lambda: DAY,
                             submit_draft=offline, on_provider_error=lambda e: None,
                             poll_seconds=0.01, retry_delay=10)
    worker.submit(action("новый пост", "31:0"))
    try:
        eventually(lambda: attempts)
    finally:
        worker.close()
    saved = json.loads(worker.queue_path.read_text(encoding="utf-8"))
    assert saved["jobs"][0]["_delivery"]["drafts"][0]["id"] == "new"
    assert saved["done"] == [] and len(calls) == 1
    # Файл уже отредактирован кем-то; повтор должен доставить зафиксированный снимок.
    (tmp_path / "runs" / DAY / "drafts" / "new.json").write_text('{"id":"new","text":"CHANGED"}', encoding="utf-8")
    resumed = EditorialWorker(generate, posted.append, root=tmp_path, today=lambda: DAY,
                              submit_draft=delivered.append, on_provider_error=lambda e: None,
                              claude_available=lambda: False, poll_seconds=0.01)
    try:
        assert resumed.join_idle(3)
        assert len(calls) == 1 and delivered == [{"id": "new", "text": "Принятый текст"}]
        assert json.loads(resumed.queue_path.read_text(encoding="utf-8"))["done"] == ["31:0"]
    finally:
        resumed.close()


def test_partial_generation_keeps_original_baseline_on_resume(tmp_path):
    errors, delivered = [], []
    def partial(cmd, **kwargs):
        day = tmp_path / "runs" / DAY
        (day / "drafts").mkdir(parents=True, exist_ok=True)
        (day / "verdicts").mkdir(exist_ok=True)
        (day / "drafts" / "partial.json").write_text('{"id":"partial","text":"Готовая часть"}', encoding="utf-8")
        (day / "verdicts" / "partial.json").write_text('{"ok":true}', encoding="utf-8")
        return SimpleNamespace(returncode=1, stdout=json.dumps({"is_error": True, "result": "You've hit your weekly limit"}), stderr="")
    worker = EditorialWorker(partial, lambda t: None, root=tmp_path, today=lambda: DAY,
                             submit_draft=delivered.append, on_provider_error=errors.append,
                             claude_available=lambda: not errors, poll_seconds=0.01, retry_delay=10)
    worker.submit(action("два поста", "32:0"))
    try:
        eventually(lambda: errors)
    finally:
        worker.close()
    before = json.loads(worker.queue_path.read_text(encoding="utf-8"))["jobs"][0]["_generation"]["before"]
    assert before == []
    def complete(cmd, **kwargs):
        (tmp_path / "runs" / DAY / "summary.md").write_text("Итог продолжения", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    resumed = EditorialWorker(complete, lambda t: None, root=tmp_path, today=lambda: DAY,
                              submit_draft=delivered.append, on_provider_error=lambda e: None, poll_seconds=0.01)
    try:
        assert resumed.join_idle(3)
        assert delivered == [{"id": "partial", "text": "Готовая часть"}]
    finally:
        resumed.close()


def test_submit_strips_injected_delivery_stage(tmp_path):
    worker = EditorialWorker(lambda *a, **k: None, lambda t: None, root=tmp_path,
                             claude_available=lambda: False, poll_seconds=0.01)
    try:
        worker.submit({**action("тема", "33:0"), "_generation": {"day": "other", "before": []},
                       "_delivery": {"drafts": [{"id": "forged"}], "next": 0}})
        saved = json.loads(worker.queue_path.read_text(encoding="utf-8"))["jobs"][0]
        assert "_generation" not in saved and "_delivery" not in saved and saved["_job_key"] == "33:0"
    finally:
        worker.close()
