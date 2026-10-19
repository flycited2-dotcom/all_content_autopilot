"""Сравнение двух деревьев каталогов: только слева, только справа, различаются.

Запуск: python tools/tree_diff.py LEFT RIGHT [--left-name server] [--right-name repo]
        [--list only-left|only-right|differ]
Концы строк (CRLF/LF) не считаются различием: Windows-чекаут против сервера.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

IGNORE_DIRS = {"__pycache__", ".venv", ".git", ".pytest_cache", ".ruff_cache", "state"}
IGNORE_SUFFIXES = {".pyc"}


@dataclass
class TreeDiff:
    only_left: list[str]
    only_right: list[str]
    differ: list[str]


def _files(root: Path) -> dict[str, Path]:
    found = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if path.is_file() and not IGNORE_DIRS & set(rel.parts) and path.suffix not in IGNORE_SUFFIXES:
            found[rel.as_posix()] = path
    return found


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def compare(left: Path, right: Path) -> TreeDiff:
    a, b = _files(left), _files(right)
    common = sorted(a.keys() & b.keys())
    return TreeDiff(
        only_left=sorted(a.keys() - b.keys()),
        only_right=sorted(b.keys() - a.keys()),
        differ=[rel for rel in common if _digest(a[rel]) != _digest(b[rel])],
    )


def render_markdown(diff: TreeDiff, left_name: str, right_name: str) -> str:
    lines = []
    for title, items in ((f"Только в {left_name}", diff.only_left),
                         (f"Только в {right_name}", diff.only_right),
                         ("Различаются", diff.differ)):
        lines.append(f"## {title} ({len(items)})")
        lines.extend(f"- `{item}`" for item in items)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--left-name", default="left")
    parser.add_argument("--right-name", default="right")
    parser.add_argument("--list", choices=["only-left", "only-right", "differ"])
    args = parser.parse_args(argv)
    for root in (args.left, args.right):
        if not root.is_dir():
            print(f"каталог не найден: {root}", file=sys.stderr)
            return 2
    diff = compare(args.left, args.right)
    if args.list:
        items = {"only-left": diff.only_left, "only-right": diff.only_right,
                 "differ": diff.differ}[args.list]
        print("\n".join(items))
    else:
        print(render_markdown(diff, args.left_name, args.right_name))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
