import json
from pathlib import Path

import pytest

import check_draft

RULES = {
    "min_length": 20,
    "max_length": 300,
    "stop_words": ["гарантированно", "елка"],
    "forbid_prices": True,
    "require_sources": True,
    "require_visual_prompt": True,
}
URL = "https://example.ru/spec"


def _draft(text="Сплит-система охлаждает комнату до 18000 БТЕ, шум 3.5 дБ.", **over):
    draft = {
        "id": "d1",
        "channel": "tg_editorial",
        "title": "t",
        "text": text,
        "facts": [{"id": "f1", "text": "Мощность 18000 БТЕ, шум 3.5 дБ", "source_url": URL}],
        "source_urls": [URL],
        "visual_prompt": "светлая комната с кондиционером",
    }
    draft.update(over)
    return draft


def test_valid_draft_passes():
    assert check_draft.check(_draft(), RULES) == []


def test_unsupported_number_rejected():
    reasons = check_draft.check(_draft(text="Охлаждает за 7 минут комнату до 18000 БТЕ."), RULES)
    assert "число 7 в тексте не подтверждено фактом" in reasons


def test_grouped_thousands_and_decimal_comma_match_facts():
    text = "Мощность 18 000 БТЕ и 18\u00a0000 БТЕ, шум всего 3,5 дБ — тихо."
    assert check_draft.check(_draft(text=text), RULES) == []


def test_price_rejected_even_if_number_in_facts():
    facts = [{"id": "f1", "text": "Цена 25990", "source_url": URL}]
    reasons = check_draft.check(_draft(text="Купите сейчас — всего от 25 990 ₽ за штуку.", facts=facts), RULES)
    assert "цена в редакционном тексте запрещена" in reasons
    reasons = check_draft.check(_draft(text="Стоит 25990 рублей, это выгодно и надёжно.", facts=facts), RULES)
    assert "цена в редакционном тексте запрещена" in reasons


def test_stop_word_case_and_yo():
    reasons = check_draft.check(_draft(text="ГАРАНТИРОВАННО охладит, как Ёлка в лесу зимой."), RULES)
    assert "стоп-слово: гарантированно" in reasons
    assert "стоп-слово: елка" in reasons


def test_length_limits():
    assert "текст короче минимума (5 < 20)" in check_draft.check(_draft(text="Тихо."), RULES)
    long_text = "а" * 301
    assert "текст длиннее лимита (301 > 300)" in check_draft.check(_draft(text=long_text), RULES)


def test_fact_without_link_and_missing_sources():
    reasons = check_draft.check(_draft(source_urls=[]), RULES)
    assert "нет источников" in reasons
    assert "нет ссылки для факта f1" in reasons


def test_missing_visual_prompt():
    assert "нет задания для изображения" in check_draft.check(_draft(visual_prompt="  "), RULES)


@pytest.fixture
def rules_dir(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    (d / "tg_editorial.yaml").write_text(json.dumps(RULES, ensure_ascii=False), encoding="utf-8")
    return d


def _run(tmp_path, rules_dir, payload, capsys):
    path = tmp_path / "draft.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8")
    code = check_draft.main([str(path), "--rules-dir", str(rules_dir)])
    return code, json.loads(capsys.readouterr().out)


def test_main_ok_and_rejected(tmp_path, rules_dir, capsys):
    code, out = _run(tmp_path, rules_dir, _draft(), capsys)
    assert (code, out["ok"]) == (0, True)
    code, out = _run(tmp_path, rules_dir, _draft(visual_prompt=""), capsys)
    assert (code, out["ok"]) == (1, False)
    assert out["reasons"] == ["нет задания для изображения"]


@pytest.mark.parametrize("payload, fragment", [
    ("{не json", "JSON"),
    (json.dumps({"channel": "tg_editorial"}), "нет поля text"),
    (json.dumps(_draft(channel="instagram")), "нет правил для канала instagram"),
    (json.dumps(_draft(text="")), "пустой текст"),
])
def test_main_bad_input_returns_2(tmp_path, rules_dir, capsys, payload, fragment):
    code, out = _run(tmp_path, rules_dir, payload, capsys)
    assert code == 2
    assert out["ok"] is False
    assert fragment in out["error"]


def test_repo_rules_load():
    root = Path(__file__).resolve().parents[1] / "rules"
    for channel in ("tg_editorial", "vk_editorial"):
        rules = check_draft.load_rules(root, channel)
        assert set(check_draft.REQUIRED_RULE_KEYS) <= set(rules)


# --- финальное ревью: находки 3–7 ---

def test_list_markers_and_urls_do_not_count_as_numbers():
    text = ("Что важно знать:\n1. Мощность 18000 БТЕ.\n2) Шум 3.5 дБ.\n"
            "Источники: https://eur-lex.europa.eu/d2ff43b5-12f1/Regulation_626_2011")
    assert check_draft.check(_draft(text=text), RULES) == []


@pytest.mark.parametrize("text", [
    "Купите сейчас: всего от 25 990 р. за комплект, тихий и надёжный.",
    "Комплект стоит 18000 Р — тихий, надёжный, с обогревом зимой.",
    "Рублёвый эквивалент 18000 — тихий, надёжный, с обогревом зимой.",
])
def test_price_variants_rejected(text):
    facts = [{"id": "f1", "text": "25990 18000", "source_url": URL}]
    assert "цена в редакционном тексте запрещена" in check_draft.check(_draft(text=text, facts=facts), RULES)


def test_stop_words_match_word_start_inflections_and_spaced_percent():
    rules = dict(RULES, stop_words=["рассрочк", "кредит", "100%"])
    reasons = check_draft.check(_draft(text="Берите в рассрочку, эффект 100 % — монтажник аккредитованный."), rules)
    assert "стоп-слово: рассрочк" in reasons
    assert "стоп-слово: 100%" in reasons
    assert "стоп-слово: кредит" not in reasons


def test_fact_not_in_research_rejected():
    research = [{"id": "f1", "text": "Мощность 18000 БТЕ, шум 3.5 дБ", "source_url": URL}]
    invented = {"id": "f9", "text": "Охлаждает за 7 минут", "source_url": URL}
    draft = _draft(text="Охлаждает за 7 минут до 18000 БТЕ, шум 3.5 дБ.", facts=research + [invented])
    assert check_draft.check(draft, RULES, research) == ["факт f9 не найден в исследовании"]
    assert check_draft.check(_draft(), RULES, research) == []


def test_main_research_option(tmp_path, rules_dir, capsys):
    research = tmp_path / "research.json"
    research.write_text(json.dumps({"topic": "t", "facts": []}, ensure_ascii=False), encoding="utf-8")
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(_draft(), ensure_ascii=False), encoding="utf-8")
    code = check_draft.main([str(path), "--rules-dir", str(rules_dir), "--research", str(research)])
    assert code == 1
    assert "факт f1 не найден в исследовании" in json.loads(capsys.readouterr().out)["reasons"]


def test_main_bad_fact_types_and_encoding_return_2(tmp_path, rules_dir, capsys):
    bad = _draft(facts=[{"id": "f1", "text": None, "source_url": URL}])
    code, out = _run(tmp_path, rules_dir, bad, capsys)
    assert code == 2 and "строк" in out["error"]
    path = tmp_path / "cp1251.json"
    path.write_bytes(json.dumps(_draft(), ensure_ascii=False).encode("cp1251"))
    code = check_draft.main([str(path), "--rules-dir", str(rules_dir)])
    assert code == 2 and "UTF-8" in json.loads(capsys.readouterr().out)["error"]


def test_bom_draft_is_accepted(tmp_path, rules_dir, capsys):
    path = tmp_path / "bom.json"
    path.write_text(json.dumps(_draft(), ensure_ascii=False), encoding="utf-8-sig")
    assert check_draft.main([str(path), "--rules-dir", str(rules_dir)]) == 0
