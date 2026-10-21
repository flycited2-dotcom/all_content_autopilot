"""Утренний прогон: раз в день с 08:00 до 20:00, отметка дня, выключатель."""
import json
from datetime import datetime

from listener import morning


class Worker:
    def __init__(self):
        self.jobs = []

    def submit(self, action):
        self.jobs.append(action)


def test_runs_once_per_day_within_window(tmp_path):
    state, worker, posted = tmp_path / "m.json", Worker(), []
    assert not morning.maybe_run(datetime(2026, 10, 22, 7, 59), state, worker, posted.append)
    assert morning.maybe_run(datetime(2026, 10, 22, 8, 0), state, worker, posted.append)
    assert not morning.maybe_run(datetime(2026, 10, 22, 12, 0), state, worker, posted.append)
    assert worker.jobs[0]["channels"] == ["tg_editorial", "vk_editorial"] and "Утренний прогон" in posted[0]
    assert morning.maybe_run(datetime(2026, 10, 23, 9, 30), state, worker, posted.append)   # ПК включили позже
    assert not morning.maybe_run(datetime(2026, 10, 24, 20, 0), state, worker, posted.append)  # вечером — нет
    assert len(worker.jobs) == 2


def test_off_switch_and_failed_notice(tmp_path):
    state, worker = tmp_path / "m.json", Worker()
    state.write_text(json.dumps({"off": True}), encoding="utf-8")
    assert not morning.maybe_run(datetime(2026, 10, 22, 9, 0), state, worker, print)
    state.write_text("{}", encoding="utf-8")

    def broken(text):
        raise ConnectionError

    assert morning.maybe_run(datetime(2026, 10, 22, 9, 0), state, worker, broken)   # прогон всё равно идёт
    assert json.loads(state.read_text(encoding="utf-8"))["day"] == "2026-10-22"
