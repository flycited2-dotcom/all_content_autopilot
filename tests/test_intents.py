import pytest

from orchestrator_bot import intents
from orchestrator_bot.intents import IntentError, Outcome, Result


def test_parse_intent_accepts_fenced_json():
    assert intents.parse_intent('```json\n{"reply":"ok","actions":[]}\n```')["reply"] == "ok"


def test_parse_intent_rejects_text():
    with pytest.raises(IntentError):
        intents.parse_intent("не понял")


def test_normalize_drops_unknown_and_invalid():
    reply, actions = intents.normalize({"reply": "r", "actions": [
        {"type": "publish", "text": "x"},
        {"type": "enqueue_products", "category": ""},
        {"type": "enqueue_products", "category": "холодильники", "count": 2, "quotas": {"Beko": 2}},
    ]})
    assert reply == "r"
    assert actions == [{"type": "enqueue_products", "category": "холодильники", "count": 2,
                        "quotas": {"beko": 2}, "source": None, "at": None, "day": None}]


def test_due_text():
    assert intents.due_text({"day": "завтра", "at": "12:00"}) == "завтра 12:00"
    assert intents.due_text({"at": "18:30"}) == "сегодня 18:30"
    assert intents.due_text({"day": "2026-10-21", "at": "12:00"}) == "21.10 12:00"
    assert intents.due_text({}) is None


def _enqueue(count=None):
    return {"type": "enqueue_products", "category": "стабилизаторы", "count": count,
            "quotas": {}, "source": None, "at": None, "day": None}


def test_plan_not_found_when_zero_matches():
    [o] = intents.plan([_enqueue()], lambda a: {"total_matches": 0, "items": []})
    assert o.kind == "not_found"


def test_plan_asks_when_all_and_too_many():
    [o] = intents.plan([_enqueue()], lambda a: {"total_matches": 57, "items": [{}] * 57})
    assert o.kind == "ask"
    assert o.options == ["Первые 40", "Все 57", "Отмена"]
    assert o.matches == 57


def test_plan_enqueues_explicit_count_even_if_many():
    [o] = intents.plan([_enqueue(count=10)], lambda a: {"total_matches": 200, "items": []})
    assert o.kind == "enqueue"


def test_plan_passes_claude_ask_through():
    ask = {"type": "ask", "question": "Какие холодильники?", "options": ["Beko", "Indesit"]}
    [o] = intents.plan([ask], lambda a: pytest.fail("resolve не нужен"))
    assert (o.kind, o.text, o.options) == ("ask", "Какие холодильники?", ["Beko", "Indesit"])


def test_render_adds_cancel_and_ask_buttons():
    enq = Result(Outcome("enqueue", _enqueue(), "стабилизаторы"), "Поставил: 3 позиции", log_id=7)
    ask = Result(Outcome("ask", {}, "Сколько?", options=["Первые 40", "Отмена"]), "Сколько?", ask_id=3)
    text, keyboard = intents.render("Готово.", [enq, ask])
    assert text.splitlines()[0] == "Готово."
    assert "Поставил: 3 позиции" in text
    assert [b["callback_data"] for row in keyboard for b in row] == ["c:7", "a:3:0", "a:3:1"]


def test_plan_no_doubt_with_numeric_quotas():
    # «3 холодильника Beko» без count: выбирается 3, хотя категория большая — не переспрашивать
    action = dict(_enqueue(), quotas={"beko": 3})
    [o] = intents.plan([action], lambda a: {"total_matches": 120, "items": [{}] * 3})
    assert o.kind == "enqueue"


def test_normalize_rejects_bad_time_and_draft_id():
    _, actions = intents.normalize({"actions": [
        {"type": "enqueue_products", "category": "x", "at": "25:99"},
        {"type": "revise_editorial", "draft_id": "../../x", "notes": "короче"},
        {"type": "revise_editorial", "draft_id": "nasosy-v2", "notes": "короче"},
    ]})
    assert actions[0]["at"] is None
    assert [a.get("draft_id") for a in actions[1:]] == ["nasosy-v2"]
