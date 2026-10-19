"""Детерминированная проверка черновика по правилам канала.

Запуск: python tools/check_draft.py DRAFT.json [--rules-dir rules]
Вывод: JSON-вердикт в stdout. Код: 0 — прошёл, 1 — отклонён, 2 — ошибка входа.
Логика «каждое число подтверждено фактом» перенесена из StrictCriticAgent
(content-factory/src/content_factory/agents/editorial.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REQUIRED_RULE_KEYS = ("min_length", "max_length", "stop_words", "forbid_prices",
                      "require_sources", "require_visual_prompt")
_GROUPED = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))")
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)?")
_PRICE = re.compile(r"₽|\bруб(?:л[а-я]*)?\b", re.IGNORECASE)


class DraftError(ValueError):
    pass


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def numbers(text: str) -> set[str]:
    return {value.replace(",", ".") for value in _NUMBER.findall(_GROUPED.sub("", text))}


def load_draft(path: Path) -> dict:
    try:
        draft = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DraftError(f"черновик не JSON: {exc}") from exc
    if not isinstance(draft, dict):
        raise DraftError("черновик должен быть JSON-объектом")
    for key, kind in (("channel", str), ("text", str), ("facts", list),
                      ("source_urls", list), ("visual_prompt", str)):
        if key not in draft:
            raise DraftError(f"нет поля {key}")
        if not isinstance(draft[key], kind):
            raise DraftError(f"поле {key} неверного типа")
    if not draft["text"].strip():
        raise DraftError("пустой текст")
    for fact in draft["facts"]:
        if not isinstance(fact, dict) or not {"id", "text", "source_url"} <= fact.keys():
            raise DraftError("факт должен содержать id, text, source_url")
    return draft


def load_rules(rules_dir: Path, channel: str) -> dict:
    path = Path(rules_dir) / f"{channel}.yaml"
    if not path.is_file():
        raise DraftError(f"нет правил для канала {channel}")
    rules = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [key for key in REQUIRED_RULE_KEYS if key not in rules]
    if missing:
        raise DraftError(f"в правилах {channel} нет ключей: {', '.join(missing)}")
    return rules


def check(draft: dict, rules: dict) -> list[str]:
    text = draft["text"]
    reasons = []
    if len(text) < rules["min_length"]:
        reasons.append(f"текст короче минимума ({len(text)} < {rules['min_length']})")
    if len(text) > rules["max_length"]:
        reasons.append(f"текст длиннее лимита ({len(text)} > {rules['max_length']})")
    lowered = _normalize(text)
    for word in rules["stop_words"]:
        if _normalize(word) in lowered:
            reasons.append(f"стоп-слово: {word}")
    if rules["forbid_prices"] and _PRICE.search(text):
        reasons.append("цена в редакционном тексте запрещена")
    supported = set().union(*(numbers(fact["text"]) for fact in draft["facts"]))
    for number in sorted(numbers(text) - supported):
        reasons.append(f"число {number} в тексте не подтверждено фактом")
    if rules["require_sources"]:
        if not draft["source_urls"]:
            reasons.append("нет источников")
        for fact in draft["facts"]:
            if fact["source_url"] not in draft["source_urls"]:
                reasons.append(f"нет ссылки для факта {fact['id']}")
    if rules["require_visual_prompt"] and not draft["visual_prompt"].strip():
        reasons.append("нет задания для изображения")
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path)
    parser.add_argument("--rules-dir", type=Path, default=Path(__file__).resolve().parents[1] / "rules")
    args = parser.parse_args(argv)
    try:
        draft = load_draft(args.draft)
        rules = load_rules(args.rules_dir, draft["channel"])
    except (DraftError, OSError, yaml.YAMLError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    reasons = check(draft, rules)
    print(json.dumps({"ok": not reasons, "channel": draft["channel"], "reasons": reasons},
                     ensure_ascii=False))
    return 0 if not reasons else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
