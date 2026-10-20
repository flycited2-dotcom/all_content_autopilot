from datetime import datetime

from orchestrator_bot.workers import readiness

NOW = datetime(2026, 10, 20, 13, 45)


def _w(generation=True, online=True, last_seen="2026-10-20T13:44:30", commands=None, lanes=True):
    return {"generation": generation, "agent_online": online, "last_seen": last_seen if lanes else None,
            "lanes": [{"lane": "desktop-a1", "seen_at": last_seen, "online": online}] if lanes else [],
            "commands": commands or {}}


def test_ready_when_generation_on_and_agent_online():
    ok, text = readiness(_w(), NOW)
    assert ok is True and "работает" in text


def test_generation_off_is_reported():
    ok, text = readiness(_w(generation=False), NOW)
    assert ok is False and "генерация выключена" in text


def test_agent_offline_reports_last_seen_and_manual_stop():
    ok, text = readiness(_w(online=False, last_seen="2026-10-19T23:10:58", commands={"agent_command": "stop"}), NOW)
    assert ok is False
    assert "фото-агент не на связи с 23:10 19.10" in text
    assert "остановлен вручную" in text


def test_agent_never_seen():
    ok, text = readiness(_w(online=False, lanes=False), NOW)
    assert ok is False and "ни разу не выходил на связь" in text
