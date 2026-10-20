import re
from pathlib import Path

from orchestrator_bot.report import build_report

NOW = 1_800_000_000.0
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _log(kind, payload, result, i):
    return {"id": i, "msg_id": 1, "kind": kind, "payload": payload, "result": result, "ts": NOW - 100}


def test_report_counts_enqueued_minus_cancelled():
    actions = [
        _log("enqueue", {"category": "стабилизаторы"}, {"accepted": ["a", "b", "c"]}, 1),
        _log("enqueue", {"category": "холодильники"}, {"accepted": ["d", "e"]}, 2),
        _log("cancel", {"log_id": 2, "keys": ["d", "e"]}, {"cancelled": 2}, 3),
    ]
    text = build_report({"by_status": {"preview": 3}, "published_since": 1}, actions, NOW - 10, NOW)
    assert "Поставлено задач: 3" in text
    assert "стабилизаторы — 3" in text
    assert "Отменено: 2" in text
    assert "Опубликовано: 1" in text
    assert "ПК: на связи" in text


def test_report_empty_day():
    assert "Сегодня задач не было" in build_report({"by_status": {}}, [], NOW - 10, NOW)


def test_report_marks_pc_offline():
    assert "ПК: не был на связи с" in build_report({"by_status": {}}, [], NOW - 3 * 3600, NOW)


def test_report_lists_editorial_runs():
    actions = [_log("run_editorial", {"brief": "тепловые насосы"}, {}, 1)]
    assert "тепловые насосы" in build_report({"by_status": {}}, actions, NOW, NOW)


def test_units_have_no_direct_shell_scripts():
    units = list(DEPLOY.glob("*.service"))
    assert units, "нет юнитов в deploy/"
    for unit in units:
        for command in re.findall(r"^Exec\w+=[-@:+!]*(\S+)", unit.read_text(encoding="utf-8"), re.M):
            assert not command.endswith(".sh"), f"{unit.name}: {command}"


def test_bot_unit_has_resource_limits():
    unit = (DEPLOY / "orchestrator-bot.service").read_text(encoding="utf-8")
    for key in ("MemoryMax=", "OOMScoreAdjust=", "Nice="):
        assert key in unit


def test_logging_hides_http_request_urls_with_token():
    import logging
    from orchestrator_bot.__main__ import configure_logging
    configure_logging()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
