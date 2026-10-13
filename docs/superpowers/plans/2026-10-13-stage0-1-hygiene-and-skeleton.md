# Этапы 0–1: наведение порядка и каркас Agents_MY — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать открытые пароли, свести код контент-завода к одному репозиторию, совпадающему с сервером, и поднять в `Agents_MY` оркестратор с агентами, который по утреннему брифу готовит и проверяет черновики TG/ВК **локально, ничего не публикуя**.

**Architecture:** Этап 0 — read-only снимок `/opt` с VPS, сравнение деревьев своим скриптом `tools/tree_diff.py`, перенос серверной правды в ветку канона `GitHub/content-factory`. Этап 1 — проект Claude Code в `Agents_MY`: `CLAUDE.md`, субагенты в `.claude/agents/`, команда `/morning-run`, правила каналов в `rules/*.yaml` и детерминированный валидатор `tools/check_draft.py`, который вызывает агент-критик. Подключение к VPS (factory-MCP) — этап 2, отдельный план; здесь бриф читается из `inbox/brief.md`, результат пишется в `runs/<дата>/`.

**Tech Stack:** Python 3.13 (локально), PyYAML, pytest; Claude Code 2.1.x (`claude -p`, субагенты, slash-команды, `settings.json`); Git Bash + PowerShell; SSH-алиас `sprintbox-itp` (212.116.115.150, порт 2222, ключ `id_ritualb2b_admin`).

**Spec:** `docs/superpowers/specs/2026-10-13-content-factory-orchestrator-design.md`

## Global Constraints

- Товарные посты с ценами — только шаблоны; нейросеть не трогает цены и характеристики. В редакционных черновиках цены запрещены (валидатор отклоняет).
- Ничего не публикуется в обход статуса `review`; на этапе 1 публикация невозможна в принципе (инструменты публикации не разрешены).
- Любое действие на VPS — только с отдельного подтверждения владельца в чате; в этом плане на VPS выполняются **только чтение** (`ls`, `tar` в stdout).
- Секреты не печатаются в чат/логи и не коммитятся; снимки сервера — в `Agents_MY/snapshots/` (в `.gitignore`), без `.env*`, `state/`, `*.db`.
- `git push` в любом репозитории — только с подтверждения владельца.
- Канон: `C:\Users\TLT-1\Documents\GitHub\content-factory`; деплой — только из него.
- Коммиты завершаются строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Черновик с битым/пустым JSON или без обязательного поля → код выхода 2 и понятное сообщение, не traceback (тест в Task 6).
2. Числа с разделителем тысяч («18 000», неразрывный пробел) и десятичной запятой («3,5») → сопоставляются с фактом «18000» / «3.5», без ложных отказов (тест в Task 6).
3. Цена, протащенная в редакционный текст («от 25 990 ₽», «рублей»), → отказ, даже если число есть в фактах (тест в Task 6).
4. Стоп-слово в другом регистре или с «ё» («ГАРАНТИРОВАННО», «Ёлка») → находится (тест в Task 6).
5. Файлы с CRLF (Windows-чекаут) против LF (сервер) → не считаются различающимися при сравнении деревьев (тест в Task 2).

---

## File Structure

`Agents_MY` (новое):
- `pyproject.toml` — pytest, `pythonpath = ["tools"]`.
- `.gitignore` — `snapshots/`, `runs/`, `inbox/brief.md`, `__pycache__/`, `.pytest_cache/`.
- `tools/tree_diff.py` — сравнение двух деревьев (этап 0).
- `tools/check_draft.py` — детерминированная проверка черновика по правилам канала.
- `tests/test_tree_diff.py`, `tests/test_check_draft.py`.
- `rules/tg_editorial.yaml`, `rules/vk_editorial.yaml`, `rules/brand.md`.
- `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/{researcher,tg-editor,vk-editor,product-planner,critic,reporter}.md`, `.claude/commands/morning-run.md`.
- `inbox/brief.example.md`, `scripts/morning-run.ps1`.
- `docs/reports/` — отчёты сравнения (только пути, без содержимого конфигов).

`GitHub/content-factory` (ветка `consolidate/server-truth-2026-10`): файлы, которые есть только на сервере или там новее; юниты `cf-vk-*`, `cf-splithub-sync`; правка `deploy/DEPLOY.md`; `docs/REPOSITORY-SYNC-2026-10.md`.

Прочие копии: `ARCHIVED.md` в `GitHub/Content_factory`, `Codex/content-factory`, `Codex/1st_content_TG_VK_YT_INSTA_autoload`.

---

### Task 1: Убрать открытые пароли из скриптов фото-агента

**Files:**
- Modify → move: `C:\Users\TLT-1\Documents\GitHub\agent_convert_foto_rituailb2b2_V2_office\{check_db,check_vps,check_new_vps,deploy_new_vps,deploy_pull,deploy_via_cat,reset_job}.py` → `...\_archive_old_vps\`
- Create: `...\_archive_old_vps\README.md`
- Possibly modify: `...\agent_convert_foto_rituailb2b2\.gitignore`
- Temp (scratchpad, не в репозитории): `find_literal.py`, `redact.py`

**Interfaces:** Consumes: ничего. Produces: в корне фото-агента больше нет литералов паролей; список мест, где эти пароли ещё встречаются (для владельца).

- [ ] **Step 1: Найти все копии литералов (без вывода значений).** Создать в scratchpad `find_literal.py`:

```python
"""Ищет, где ещё встречаются пароли из 7 скриптов. Печатает только пути и хэши коммитов."""
import re, subprocess, sys
from pathlib import Path

SRC = Path(sys.argv[1])
ROOTS = [Path(p) for p in sys.argv[2:]]
NAMES = ["check_db.py", "check_vps.py", "check_new_vps.py", "deploy_new_vps.py",
         "deploy_pull.py", "deploy_via_cat.py", "reset_job.py"]
PAT = re.compile(r"""password\s*=\s*(["'])([^"']+)\1""", re.I)
SKIP = {".venv", "venv", "node_modules", ".git", "__pycache__", ".test-deps"}

values = set()
for name in NAMES:
    values.update(m.group(2) for m in PAT.finditer((SRC / name).read_text(encoding="utf-8")))
print(f"уникальных паролей: {len(values)}")

for root in ROOTS:
    for p in root.rglob("*"):
        if not p.is_file() or SKIP & set(p.parts) or p.stat().st_size > 5_000_000:
            continue
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(v in data for v in values):
            print("FILE", p)
    if (root / ".git").exists():
        for v in values:
            out = subprocess.run(["git", "-C", str(root), "log", "--all", "--oneline", "-S", v],
                                 capture_output=True, text=True).stdout.split()
            if out:
                print("GIT-HISTORY", root, out[0::2][:5])
```

Run:
```bash
B=/c/Users/TLT-1/Documents/GitHub
python -I "$SCRATCH/find_literal.py" "$B/agent_convert_foto_rituailb2b2_V2_office" \
  "$B/agent_convert_foto_rituailb2b2_V2_office" "$B/content-factory" "$B/Content_factory" \
  "$B/telega_agent_personal" "$B/Codex" /c/Users/TLT-1/Documents/Codex
```
Expected: `уникальных паролей: N` и строки `FILE` (минимум 7 исходных скриптов). Все `FILE` вне 7 скриптов и все `GIT-HISTORY` записать для итогового сообщения владельцу.

- [ ] **Step 2: Заменить литералы на переменную окружения.** Создать в scratchpad `redact.py`:

```python
import re, sys
from pathlib import Path

root = Path(sys.argv[1])
NAMES = ["check_db.py", "check_vps.py", "check_new_vps.py", "deploy_new_vps.py",
         "deploy_pull.py", "deploy_via_cat.py", "reset_job.py"]
PAT = re.compile(r"""(password\s*=\s*)(["'])[^"']*\2""", re.I)
for name in NAMES:
    p = root / name
    text = p.read_text(encoding="utf-8")
    new, n = PAT.subn(r'\1os.environ["OLD_VPS_ROOT_PASSWORD"]', text)
    if "import os" not in new:
        new = "import os\n" + new
    p.write_text(new, encoding="utf-8")
    print(name, "замен:", n)
```

Run: `python -I "$SCRATCH/redact.py" /c/Users/TLT-1/Documents/GitHub/agent_convert_foto_rituailb2b2_V2_office`
Expected: у каждого файла `замен:` ≥ 1.

- [ ] **Step 3: Проверить, что литералов нет и скрипты компилируются.**

```bash
cd /c/Users/TLT-1/Documents/GitHub/agent_convert_foto_rituailb2b2_V2_office
grep -nE "password\s*=\s*[\"']" check_db.py check_vps.py check_new_vps.py deploy_new_vps.py deploy_pull.py deploy_via_cat.py reset_job.py ; echo "grep exit=$?"
python -m py_compile check_db.py check_vps.py check_new_vps.py deploy_new_vps.py deploy_pull.py deploy_via_cat.py reset_job.py && echo compiled
```
Expected: `grep exit=1` (совпадений нет), `compiled`.

- [ ] **Step 4: Перенести в архив.** Серверы 213.109.202.45 и 186.246.44.204 выведены из эксплуатации (`deploy/DEPLOY.md` канона).

```bash
mkdir -p _archive_old_vps
mv check_db.py check_vps.py check_new_vps.py deploy_new_vps.py deploy_pull.py deploy_via_cat.py reset_job.py _archive_old_vps/
cat > _archive_old_vps/README.md <<'EOF'
# Архив: скрипты старых VPS (213.109.202.45, 186.246.44.204)

Серверы выведены из эксплуатации (переезд на 212.116.115.150 — 2026-07-19).
Пароли из скриптов удалены 2026-10; при необходимости запуска пароль берётся
из переменной окружения OLD_VPS_ROOT_PASSWORD. Новые скрипты — только по SSH-ключу
(алиас `sprintbox-itp` в ~/.ssh/config).
EOF
```

- [ ] **Step 5: Проверить `.env.bak-vps-migration` в репозитории фото-агента.**

```bash
cd /c/Users/TLT-1/Documents/GitHub/agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2
git ls-files | grep -E "(^|/)\.env|gdrive_(token|oauth)" ; echo "tracked-exit=$?"
git check-ignore -v .env.bak-vps-migration ; echo "ignored-exit=$?"
```
Expected: `tracked-exit=1`. Если `ignored-exit=1` (файл не игнорируется) — дописать в `.gitignore` строки `.env.*` и `!.env.example`, затем `git add .gitignore && git commit -m "chore: игнорировать резервные копии .env"` (с Co-Authored-By). Если `tracked-exit=0` — **остановиться** и сообщить владельцу: файлы с токенами в истории git, токены нужно перевыпустить.

- [ ] **Step 6: Сообщить владельцу (ручные действия только им).** Текст: сменить root-пароль на 213.109.202.45 (консоль провайдера + панель Hestia) и на 186.246.44.204, если сервер ещё жив; список `FILE`/`GIT-HISTORY` из Step 1. Корень фото-агента не в git — коммитить нечего.

---

### Task 2: Python-каркас Agents_MY и `tools/tree_diff.py`

**Files:**
- Create: `C:\Users\TLT-1\Documents\GitHub\Agents_MY\pyproject.toml`, `.gitignore`, `tools/tree_diff.py`, `tests/test_tree_diff.py`

**Interfaces:**
- Produces: `compare(left: Path, right: Path) -> TreeDiff`, `TreeDiff(only_left: list[str], only_right: list[str], differ: list[str])` (пути POSIX относительно корня, отсортированы), `render_markdown(diff, left_name, right_name) -> str`, CLI `python tools/tree_diff.py LEFT RIGHT [--left-name N] [--right-name N] [--list only-left|only-right|differ]`; коды выхода 0 / 2 (каталог не найден).

- [ ] **Step 1: Каркас проекта.**

`pyproject.toml`:
```toml
[project]
name = "agents-my"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["PyYAML>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
pythonpath = ["tools"]
testpaths = ["tests"]
```

`.gitignore`:
```
snapshots/
runs/
inbox/brief.md
__pycache__/
.pytest_cache/
```

- [ ] **Step 2: Написать падающие тесты** `tests/test_tree_diff.py`:

```python
from pathlib import Path

import tree_diff


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_compare_finds_only_left_only_right_and_differ(tmp_path):
    left, right = tmp_path / "server", tmp_path / "repo"
    _write(left, "src/a.py", b"same\n")
    _write(right, "src/a.py", b"same\n")
    _write(left, "src/splithub.py", b"x\n")
    _write(right, "docs/readme.md", b"y\n")
    _write(left, "src/b.py", b"server\n")
    _write(right, "src/b.py", b"repo\n")

    diff = tree_diff.compare(left, right)

    assert diff.only_left == ["src/splithub.py"]
    assert diff.only_right == ["docs/readme.md"]
    assert diff.differ == ["src/b.py"]


def test_crlf_and_lf_are_equal(tmp_path):
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left, "a.py", b"one\ntwo\n")
    _write(right, "a.py", b"one\r\ntwo\r\n")

    assert tree_diff.compare(left, right).differ == []


def test_ignored_dirs_and_pyc_are_skipped(tmp_path):
    left, right = tmp_path / "l", tmp_path / "r"
    right.mkdir()
    _write(left, "__pycache__/a.cpython-313.pyc", b"x")
    _write(left, ".venv/lib/x.py", b"x")
    _write(left, "state/content_factory.db", b"x")
    _write(left, "src/m.pyc", b"x")

    diff = tree_diff.compare(left, right)

    assert diff.only_left == []


def test_markdown_has_three_sections(tmp_path):
    diff = tree_diff.TreeDiff(only_left=["a"], only_right=["b"], differ=["c"])

    text = tree_diff.render_markdown(diff, "server", "repo")

    assert "## Только в server (1)" in text
    assert "## Только в repo (1)" in text
    assert "## Различаются (1)" in text
    assert "- `c`" in text


def test_main_list_mode_prints_paths(tmp_path, capsys):
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left, "x.py", b"1\n")
    right.mkdir()

    code = tree_diff.main([str(left), str(right), "--list", "only-left"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "x.py"


def test_main_missing_dir_returns_2(tmp_path, capsys):
    code = tree_diff.main([str(tmp_path / "nope"), str(tmp_path)])

    assert code == 2
    assert "не найден" in capsys.readouterr().err
```

- [ ] **Step 3: Запустить — убедиться, что падают.**
Run: `cd /c/Users/TLT-1/Documents/GitHub/Agents_MY && python -m pytest tests/test_tree_diff.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tree_diff'`.

- [ ] **Step 4: Реализация** `tools/tree_diff.py`:

```python
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
```

- [ ] **Step 5: Тесты зелёные.**
Run: `python -m pytest tests/test_tree_diff.py -q`
Expected: `6 passed`.

- [ ] **Step 6: Commit.**
```bash
git add pyproject.toml .gitignore tools/tree_diff.py tests/test_tree_diff.py
git commit -m "feat: tree_diff — сравнение деревьев сервера и репозиториев

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Read-only снимок сервера

**Files:**
- Create (вне git): `Agents_MY/snapshots/2026-10-XX/{opt,units}/` (XX — дата выполнения, далее `$D`)

**Interfaces:** Produces: `snapshots/$D/opt/content-factory/`, `snapshots/$D/opt/content-factory-vk/` (если есть), `snapshots/$D/units/` — входы для Task 4.

- [ ] **Step 1: Запросить подтверждение владельца** на чтение с VPS (команды ниже ничего не меняют на сервере). Без «да» — стоп.

- [ ] **Step 2: Осмотреться.**
```bash
ssh sprintbox-itp 'ls -la /opt/content-factory /opt/content-factory-vk 2>&1; ls /etc/systemd/system | grep -E "^(cf-|avito-drive)"'
```
Expected: содержимое каталогов и список юнитов `cf-*`. Если `/opt/content-factory-vk` нет — в Step 3 убрать его из команды.

- [ ] **Step 3: Снять снимок без секретов и данных.**
```bash
D=$(date +%F); S=/c/Users/TLT-1/Documents/GitHub/Agents_MY/snapshots/$D
mkdir -p "$S/opt" "$S/units"
ssh sprintbox-itp 'cd /opt && tar -czf - --exclude=.venv --exclude=__pycache__ --exclude=state --exclude=".env*" --exclude="*.db" --exclude=tasks --exclude="*.tgz" content-factory content-factory-vk' > "$S/opt.tgz"
ssh sprintbox-itp 'cd /etc/systemd/system && tar -czf - cf-* avito-drive-archive*' > "$S/units.tgz"
tar -xzf "$S/opt.tgz" -C "$S/opt" && tar -xzf "$S/units.tgz" -C "$S/units" && ls "$S/opt" "$S/units" | head -50
```
Expected: `content-factory` (и `content-factory-vk`) в `opt/`, юниты в `units/`.

- [ ] **Step 4: Проверить снимок на секреты (печатаются только имена файлов).**
```bash
grep -rlIE "(TOKEN|PASSWORD|SECRET|API_KEY)\s*=\s*[^\s$]" "$S" ; echo "exit=$?"
```
Expected: `exit=1`. Если найдено — удалить эти файлы из снимка (`rm`) и записать их пути в отчёт Task 4 как «секреты на сервере в файлах, не в .env».

---

### Task 4: Отчёт расхождений и перенос серверной правды в канон

**Files:**
- Create: `Agents_MY/docs/reports/2026-10-XX-server-vs-repos.md`
- Modify (ветка `consolidate/server-truth-2026-10` в `GitHub/content-factory`): серверные файлы по правилу ниже; `deploy/` (юниты); `deploy/DEPLOY.md`; Create `docs/REPOSITORY-SYNC-2026-10.md`

**Interfaces:** Consumes: `tools/tree_diff.py` (Task 2), снимок `$S` (Task 3). Produces: ветка канона, совпадающая с `/opt/content-factory` (кроме осознанно недеплоенных файлов, перечисленных в `REPOSITORY-SYNC-2026-10.md`).

- [ ] **Step 1: Базовый прогон тестов канона до изменений.**
```bash
R=/c/Users/TLT-1/Documents/GitHub/content-factory; cd "$R"
git status --short | grep -v __pycache__ ; git switch feat/aru-post-details
.venv/Scripts/python -m pytest -q 2>&1 | tail -3
```
Expected: чистое дерево; итог вида `N passed` — записать N и любые падения как «исходные».

- [ ] **Step 2: Отчёт расхождений.**
```bash
A=/c/Users/TLT-1/Documents/GitHub/Agents_MY; AUTO=/c/Users/TLT-1/Documents/Codex/1st_content_TG_VK_YT_INSTA_autoload
OUT="$A/docs/reports/$D-server-vs-repos.md"; mkdir -p "$A/docs/reports"
{ echo "# Сервер vs репозитории ($D)"; echo
  echo "# /opt/content-factory vs канон"; python "$A/tools/tree_diff.py" "$S/opt/content-factory" "$R" --left-name сервер --right-name канон
  echo "# /opt/content-factory vs autoload"; python "$A/tools/tree_diff.py" "$S/opt/content-factory" "$AUTO" --left-name сервер --right-name autoload
  echo "# /opt/content-factory-vk vs канон"; python "$A/tools/tree_diff.py" "$S/opt/content-factory-vk" "$R" --left-name сервер-vk --right-name канон
} > "$OUT"; grep -E "^## " "$OUT"
```
Expected: заголовки секций с количествами.

- [ ] **Step 3: Контрольная точка по `/opt/content-factory-vk`.** Если там есть `src/` и секция «Различаются» для `src/` в третьем сравнении не пуста — в проде работают **две разные версии кода**. Остановиться, показать владельцу эту секцию и спросить, какая версия ВК-модулей правильная. Дальнейшие шаги задачи касаются только `/opt/content-factory`, а слияние ВК-версии — по решению владельца отдельным коммитом.

- [ ] **Step 4: Ветка и решение по каждому различающемуся файлу.** Правило: файл берётся с сервера, если серверная копия изменена позже последнего коммита этого файла в каноне; иначе остаётся версия канона и файл попадает в список «ещё не задеплоено».
```bash
cd "$R"; git switch -c consolidate/server-truth-2026-10
SC="$S/opt/content-factory"
python "$A/tools/tree_diff.py" "$SC" "$R" --list differ > /tmp/differ.txt
python "$A/tools/tree_diff.py" "$SC" "$R" --list only-left > /tmp/only_server.txt
: > /tmp/take_server.txt; : > /tmp/keep_repo.txt
while read -r f; do
  smt=$(date -r "$SC/$f" +%s); rmt=$(git log -1 --format=%ct -- "$f")
  if [ -z "$rmt" ] || [ "$smt" -gt "$rmt" ]; then echo "$f" >> /tmp/take_server.txt; else echo "$f" >> /tmp/keep_repo.txt; fi
done < /tmp/differ.txt
wc -l /tmp/only_server.txt /tmp/take_server.txt /tmp/keep_repo.txt
```
Expected: три числа. Ожидаемо в `only_server.txt` есть `src/content_factory/ingest/splithub_catalog.py` и `splithub_sync.py` (или под другим путём — свериться с `agent_convert_foto_rituailb2b2_V2_office/splithub_deploy/files/`).

- [ ] **Step 5: Проверить серверные файлы на секреты перед копированием.**
```bash
cat /tmp/only_server.txt /tmp/take_server.txt | while read -r f; do grep -lIiE "(token|password|secret)\s*[:=]\s*['\"][^'\"]{8,}" "$SC/$f"; done ; echo "exit=$?"
```
Expected: пустой вывод. Найденные файлы не копировать, сообщить владельцу.

- [ ] **Step 6: Перенести файлы, которые есть только на сервере, и закоммитить.**
```bash
while read -r f; do mkdir -p "$(dirname "$f")"; cp -p "$SC/$f" "$f"; done < /tmp/only_server.txt
[ -s /tmp/only_server.txt ] && git add -A -- $(cat /tmp/only_server.txt) && git commit -m "sync: файлы, существовавшие только на сервере (SplitHub и др.)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Перенести серверные версии, которые новее, и закоммитить.**
```bash
while read -r f; do cp -p "$SC/$f" "$f"; done < /tmp/take_server.txt
[ -s /tmp/take_server.txt ] && git add -- $(cat /tmp/take_server.txt) && git commit -m "sync: серверные версии файлов, изменённые позже репозитория

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 8: Юниты systemd с сервера, которых нет в `deploy/`.**
```bash
for u in "$S"/units/*; do n=$(basename "$u"); [ -e "deploy/$n" ] || cp -rp "$u" "deploy/$n"; done
git status --short deploy/ && git add deploy/ && git commit -m "deploy: юниты cf-vk-*, cf-splithub-sync и drop-in'ы с сервера

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
Expected: появились `deploy/cf-vk-*.service|timer`, `deploy/cf-splithub-sync.*`.

- [ ] **Step 9: Тесты после слияния.**
Run: `.venv/Scripts/python -m pytest -q 2>&1 | tail -5`
Expected: не меньше N passed из Step 1 и никаких новых падений. Если появились новые падения — **не править тесты**: записать их в `REPOSITORY-SYNC-2026-10.md` (раздел «Тесты, расходящиеся с продом») и сообщить владельцу; продакшен-поведение важнее устаревшего теста, решение за владельцем.

- [ ] **Step 10: Документация синхронизации и деплоя.** Создать `docs/REPOSITORY-SYNC-2026-10.md`:
```markdown
# Синхронизация с сервером — 2026-10

Источник: read-only снимок /opt/content-factory (дата снимка — в имени отчёта Agents_MY/docs/reports/).
Правило: серверный файл берётся, если изменён позже последнего коммита этого файла в репозитории.

## Взято с сервера (только там было)
<вставить содержимое /tmp/only_server.txt списком>

## Взято с сервера (новее)
<вставить содержимое /tmp/take_server.txt списком>

## Ещё не задеплоено (в репозитории новее сервера)
<вставить содержимое /tmp/keep_repo.txt списком>

## Тесты
До: N passed. После: M passed. Новые падения: <список или «нет»>.
```
(Угловые скобки заменить реальными списками при выполнении — это данные шага, не заглушки плана.)

В `deploy/DEPLOY.md` заменить строку `SSH: \`ssh -i ~/.ssh/id_ritualb2b_claude root@212.116.115.150\`.` на:
```markdown
SSH: `ssh sprintbox-itp` (алиас в ~/.ssh/config: порт 2222, ключ id_ritualb2b_admin).
**Деплой — только из этого репозитория** (ветка consolidate/server-truth-2026-10 → master). Остальные копии
(GitHub/Content_factory, Codex/content-factory, Codex/1st_content_TG_VK_YT_INSTA_autoload) — архив.
```
```bash
git add docs/REPOSITORY-SYNC-2026-10.md deploy/DEPLOY.md && git commit -m "docs: итог синхронизации с сервером, единый источник деплоя

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 11: Закоммитить отчёт в Agents_MY.**
```bash
cd "$A" && git add docs/reports && git commit -m "docs: отчёт расхождений сервер vs репозитории

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
`git push` канона — спросить владельца.

---

### Task 5: Пометить устаревшие копии как архив

**Files:**
- Create: `ARCHIVED.md` в `C:\Users\TLT-1\Documents\GitHub\Content_factory`, `C:\Users\TLT-1\Documents\Codex\content-factory`, `C:\Users\TLT-1\Documents\Codex\1st_content_TG_VK_YT_INSTA_autoload`

**Interfaces:** Consumes: итог Task 4 (канон — единственный источник деплоя).

- [ ] **Step 1: Создать файл и закоммитить локально в каждой копии.** В `GitHub/Content_factory` дописать строку про Grok-MCP — его `.venv` используется MCP-сервером `grok-imagine-xai`, папку не трогать.
```bash
for d in /c/Users/TLT-1/Documents/GitHub/Content_factory /c/Users/TLT-1/Documents/Codex/content-factory /c/Users/TLT-1/Documents/Codex/1st_content_TG_VK_YT_INSTA_autoload; do
cat > "$d/ARCHIVED.md" <<'EOF'
# АРХИВ — не деплоить

Канонический репозиторий контент-завода: C:\Users\TLT-1\Documents\GitHub\content-factory
(синхронизирован с сервером 212.116.115.150 в 2026-10, см. docs/REPOSITORY-SYNC-2026-10.md там).
Эта копия оставлена для истории. Выкладка отсюда на /opt/content-factory сотрёт SplitHub и свежие правки.
EOF
done
echo '
Исключение: tools/grok_imagine_mcp/ используется MCP-сервером grok-imagine-xai — не удалять и не переносить.' >> /c/Users/TLT-1/Documents/GitHub/Content_factory/ARCHIVED.md
for d in /c/Users/TLT-1/Documents/GitHub/Content_factory /c/Users/TLT-1/Documents/Codex/content-factory /c/Users/TLT-1/Documents/Codex/1st_content_TG_VK_YT_INSTA_autoload; do
  git -C "$d" add ARCHIVED.md && git -C "$d" commit -q -m "docs: пометка архива — канон в GitHub/content-factory

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && echo "ok $d"
done
```
Expected: три строки `ok ...`. Незакоммиченные чужие изменения в этих копиях не трогать (коммитится только `ARCHIVED.md`).

---

### Task 6: Валидатор черновиков `tools/check_draft.py` и правила каналов

**Files:**
- Create: `Agents_MY/tools/check_draft.py`, `Agents_MY/rules/tg_editorial.yaml`, `Agents_MY/rules/vk_editorial.yaml`, `Agents_MY/tests/test_check_draft.py`

**Interfaces:**
- Produces (используют субагенты в Task 7):
  - Формат черновика (JSON-файл): `{"id": str, "channel": "tg_editorial"|"vk_editorial", "title": str, "text": str, "facts": [{"id": str, "text": str, "source_url": str}], "source_urls": [str], "visual_prompt": str}`
  - `load_draft(path: Path) -> dict` (бросает `DraftError`), `load_rules(rules_dir: Path, channel: str) -> dict` (бросает `DraftError`), `check(draft: dict, rules: dict) -> list[str]` (причины отказа; пусто = прошёл), `numbers(text: str) -> set[str]`.
  - CLI: `python tools/check_draft.py DRAFT.json [--rules-dir rules]` → stdout JSON `{"ok": bool, "channel": str, "reasons": [str]}` или `{"ok": false, "error": str}`; коды 0 прошёл / 1 отклонён / 2 ошибка входа.
  - Ключи правил: `min_length`, `max_length`, `stop_words` (список), `forbid_prices`, `require_sources`, `require_visual_prompt`.

- [ ] **Step 1: Падающие тесты** `tests/test_check_draft.py`:

```python
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
```

- [ ] **Step 2: Убедиться, что падают.**
Run: `python -m pytest tests/test_check_draft.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'check_draft'`.

- [ ] **Step 3: Реализация** `tools/check_draft.py`:

```python
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
```

- [ ] **Step 4: Правила каналов.**

`rules/tg_editorial.yaml`:
```yaml
# Редакционный (не товарный) пост в канал «СплитХаб.ру» с картинкой.
min_length: 200
max_length: 1024          # лимит подписи к фото в Telegram
stop_words: [гарантированно, "лучший в мире", "без риска", "100%", кредит, рассрочка]
forbid_prices: true       # цены — только в шаблонных товарных постах content-factory
require_sources: true
require_visual_prompt: true
```

`rules/vk_editorial.yaml`:
```yaml
# Экспертный пост в группу ВК.
min_length: 400
max_length: 3500          # как StrictCriticAgent в content-factory
stop_words: [гарантированно, "лучший в мире", "без риска", "100%", кредит, рассрочка]
forbid_prices: true
require_sources: true
require_visual_prompt: true
```

- [ ] **Step 5: Тесты зелёные.**
Run: `python -m pytest -q`
Expected: все тесты проходят (`6` из Task 2 + `14` здесь = `20 passed`).

- [ ] **Step 6: Commit.**
```bash
git add tools/check_draft.py rules/tg_editorial.yaml rules/vk_editorial.yaml tests/test_check_draft.py
git commit -m "feat: детерминированный валидатор черновиков и правила TG/ВК

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Оркестратор, субагенты и `/morning-run` (локальный режим)

**Files:**
- Create в `Agents_MY`: `CLAUDE.md`, `rules/brand.md`, `.claude/settings.json`, `.claude/agents/researcher.md`, `.claude/agents/tg-editor.md`, `.claude/agents/vk-editor.md`, `.claude/agents/product-planner.md`, `.claude/agents/critic.md`, `.claude/agents/reporter.md`, `.claude/commands/morning-run.md`, `inbox/brief.example.md`, `scripts/morning-run.ps1`

**Interfaces:**
- Consumes: формат черновика и CLI `tools/check_draft.py` (Task 6).
- Отклонение от спека (осознанно): `.mcp.json` и Telegram MCP на этапе 1 не подключаются — агентам пока нечего читать в Telegram, а claude.ai-коннектор и так доступен; `.mcp.json` появится на этапе 2 вместе с factory-MCP. Инструменты Telegram не входят в allowlist, поэтому в безголовом режиме они запрещены.
- Produces (этапы 2–3 заменят файловый ввод/вывод на factory-MCP, не меняя форматы): структура прогона
  `runs/<дата>/plan.json`, `runs/<дата>/research/<тема>.json` (`{"topic": str, "facts": [{"id","text","source_url"}]}`), `runs/<дата>/drafts/<id>.json` (формат Task 6), `runs/<дата>/verdicts/<id>.json` (`{"id": str, "ok": bool, "rules": <вывод check_draft>, "judgement": {"score": 1-5, "notes": [str]}}`), `runs/<дата>/product_tasks.json` (`[{"category": str, "brand": str|null, "count": int, "why": str}]`), `runs/<дата>/summary.md`.

- [ ] **Step 1: `CLAUDE.md`.**
```markdown
# Agents_MY — оркестратор контент-завода

Ты — оркестратор. Владелец один и занят тендерами: его время — главный ресурс.
Каналы: Telegram «СплитХаб.ру» (климатическая техника, склад Симферополь), группа ВК. Видео — позже.

## Жёсткие правила
1. Цены и характеристики товаров не придумываешь и не пишешь. Товарные посты делает content-factory по шаблонам; ты только предлагаешь задачи (категория/бренд/количество).
2. Каждое число в тексте — из факта с источником. Нет источника — нет числа.
3. Ничего не публикуешь. На этом этапе результат — файлы в runs/<дата>/. Позже — черновики в статусе review через factory-MCP.
4. Не трогаешь продакшен: VPS, репозиторий content-factory, боевые каналы.
5. Секреты не читаешь и не выводишь.

## Как устроен прогон
/morning-run <дата>: бриф (inbox/brief.md) → план → researcher → tg-editor / vk-editor / product-planner → critic → reporter.
Правила каналов: rules/*.yaml (проверяет tools/check_draft.py), голос бренда: rules/brand.md.
```

- [ ] **Step 2: `rules/brand.md`** (стартовая версия — владелец правит под себя).
```markdown
# Голос бренда «СплитХаб.ру»

- Кто мы: магазин климатической техники в Крыму, склад в Симферополе, монтаж и доставка.
- Тон: спокойный эксперт-сосед. Простые слова, короткие абзацы, без канцелярита и восклицательных цепочек.
- Польза прежде продажи: пост отвечает на один практический вопрос (выбор мощности, уход, экономия, сезонные советы).
- Без обещаний «гарантированно», «лучший», без давления сроками.
- Призыв в конце — мягкий: «Подберём под вашу комнату — пишите» (кнопка «📩 Заказать» добавляется системой).
- Эмодзи: 0–3 на пост, по делу.
- Повторы: не повторять тему, раскрытую в последние 14 дней (смотреть runs/*/drafts).
```

- [ ] **Step 3: `.claude/settings.json`** — в безголовом режиме всё, что не разрешено, автоматически запрещено; MCP-инструменты публикации сюда не входят.
```json
{
  "permissions": {
    "allow": [
      "Read",
      "Glob",
      "Grep",
      "Agent",
      "WebSearch",
      "WebFetch",
      "Write(/runs/**)",
      "Edit(/runs/**)",
      "Bash(python tools/check_draft.py:*)"
    ],
    "deny": [
      "Read(**/.env*)",
      "Write(/rules/**)",
      "Write(/tools/**)",
      "Bash(ssh:*)",
      "Bash(git push:*)"
    ]
  }
}
```

- [ ] **Step 4: Субагенты.**

`.claude/agents/researcher.md`:
```markdown
---
name: researcher
description: Собирает проверяемые факты с источниками по теме поста. Вызывать перед любым редактором.
tools: WebSearch, WebFetch, Read, Write
model: sonnet
---
Ты исследователь. На вход — тема и путь к папке прогона.
1. Найди 3–7 фактов по теме из надёжных источников: сайты производителей, ГОСТ/СП, Росстандарт, крупные профильные издания. Не форумы, не маркетплейсы.
2. Каждый факт — одно утверждение, цифры дословно как в источнике, с прямой ссылкой на страницу.
3. Цены не собирай.
4. Запиши `runs/<дата>/research/<тема-латиницей>.json`:
   {"topic": "...", "facts": [{"id": "f1", "text": "...", "source_url": "https://..."}]}
5. Если надёжных фактов меньше 3 — запиши файл с тем, что нашёл, и в ответе прямо скажи «недостаточно фактов».
Ответ оркестратору: путь к файлу и число фактов.
```

`.claude/agents/tg-editor.md`:
```markdown
---
name: tg-editor
description: Пишет редакционный (не товарный) пост для Telegram-канала «СплитХаб.ру» по готовому исследованию.
tools: Read, Write
model: sonnet
---
Ты редактор Telegram-канала. Прочитай rules/brand.md, rules/tg_editorial.yaml и файл исследования.
- Текст 200–1024 символа (это подпись к картинке).
- Каждое число — только из фактов исследования, в той же записи. Никаких цен, «руб», «₽».
- Структура: цепляющая первая строка → 2–4 коротких абзаца пользы → мягкий призыв.
- visual_prompt: описание тематической картинки без текста на изображении.
Запиши `runs/<дата>/drafts/<id>.json` строго в формате:
{"id": "<дата>-tg-NN", "channel": "tg_editorial", "title": "...", "text": "...", "facts": [<факты, которые использованы>], "source_urls": [<их source_url>], "visual_prompt": "..."}
Ответ: путь к черновику.
```

`.claude/agents/vk-editor.md`:
```markdown
---
name: vk-editor
description: Пишет экспертный пост для группы ВК по готовому исследованию.
tools: Read, Write
model: sonnet
---
Ты редактор группы ВК. Прочитай rules/brand.md, rules/vk_editorial.yaml и файл исследования.
- Текст 400–3500 символов; можно нумерованный список фактов «1. …».
- Каждое число — только из фактов исследования. Никаких цен.
- В конце — блок «Источники:» со ссылками.
- visual_prompt: описание тематической картинки без текста.
Запиши `runs/<дата>/drafts/<id>.json`:
{"id": "<дата>-vk-NN", "channel": "vk_editorial", "title": "...", "text": "...", "facts": [...], "source_urls": [...], "visual_prompt": "..."}
Ответ: путь к черновику.
```

`.claude/agents/product-planner.md`:
```markdown
---
name: product-planner
description: Предлагает задачи на товарные посты (категория, бренд, количество) — без цен и текстов.
tools: Read, Write
model: sonnet
---
Ты планировщик товарных постов. Товарные посты делает content-factory по шаблонам — ты только решаешь, ЧТО выставить.
Учитывай бриф, сезон (дата прогона) и разнообразие. Категории content-factory: кондиционеры (инверторные/on-off), мобильные, полупромышленные, тепловые завесы, обогреватели, тёплый пол, очистители воздуха, стабилизаторы, бытовая техника.
Запиши `runs/<дата>/product_tasks.json`: [{"category": "...", "brand": "...|null", "count": N, "why": "одна фраза"}], всего не больше 15 товаров в день.
Никаких цен, моделей «из головы» и характеристик.
Ответ: путь к файлу и краткое обоснование.
```

`.claude/agents/critic.md`:
```markdown
---
name: critic
description: Строгий контролёр. Проверяет каждый черновик правилами и по существу; выносит вердикт. Вызывать для каждого черновика.
tools: Read, Write, Bash
model: opus
---
Ты строгий контролёр качества. Твоя задача — не пропустить плохое, а не похвалить.
Для черновика `runs/<дата>/drafts/<id>.json`:
1. Запусти `python tools/check_draft.py runs/<дата>/drafts/<id>.json`. Любой код ≠ 0 — вердикт ok=false, причины из вывода.
2. Если правила пройдены — оцени по rules/brand.md (1–5): польза для покупателя климатики в Крыму, соответствие тону, ясность, нет ли повтора темы из runs/*/drafts за 14 дней, совпадают ли утверждения с фактами по смыслу (не только по числам). ok=true только при оценке ≥ 4 и отсутствии смысловых искажений.
3. Запиши `runs/<дата>/verdicts/<id>.json`:
{"id": "<id>", "ok": true|false, "rules": <JSON-вывод check_draft>, "judgement": {"score": N, "notes": ["..."]}}
Черновики не правишь. Bash используешь только для tools/check_draft.py.
Ответ: id, ok, одна строка причины.
```

`.claude/agents/reporter.md`:
```markdown
---
name: reporter
description: Сводит итог прогона в короткий отчёт для владельца.
tools: Read, Write
model: sonnet
---
Прочитай runs/<дата>/plan.json, drafts/, verdicts/, product_tasks.json.
Запиши `runs/<дата>/summary.md` (читается за минуту):
- Строка итога: «Готово N, прошли проверку M, отклонено K».
- Прошедшие: заголовок, канал, оценка — по строке.
- Отклонённые: заголовок и главная причина.
- Товарные задачи: таблица категория / бренд / кол-во.
- Что нужно от владельца (если есть) — не больше 3 пунктов.
Ответ: путь к summary.md.
```

- [ ] **Step 5: `.claude/commands/morning-run.md`.**
```markdown
---
description: Утренний прогон контент-завода (локальный режим, без публикации)
argument-hint: <дата YYYY-MM-DD>
---
Дата прогона: $ARGUMENTS. Папка: runs/$ARGUMENTS/.

1. Прочитай inbox/brief.md. Если файла нет — запиши runs/$ARGUMENTS/summary.md с текстом «Брифа нет — прогон пропущен» и закончи.
2. Составь план и запиши runs/$ARGUMENTS/plan.json:
   {"brief": "<кратко>", "topics": [{"slug": "латиница", "title": "...", "channels": ["tg_editorial", "vk_editorial"]}], "products": true|false}
   По умолчанию: 2 темы для TG, 1 для ВК, товарные задачи — если бриф не запрещает. Не больше 4 тем.
3. Для каждой темы вызови субагента researcher (темы — параллельно).
4. По каждому исследованию с ≥ 3 фактами вызови tg-editor и/или vk-editor по plan.json (параллельно). Тему с «недостаточно фактов» пропусти и отметь в плане.
5. Если products=true — вызови product-planner.
6. Для каждого черновика вызови critic. Отклонённый черновик — один раз верни редактору с notes критика на переписывание (новый id с суффиксом -r), затем снова critic. Второй отказ — окончательный.
7. Вызови reporter.
8. Последним сообщением выведи содержимое summary.md.
Ничего не публикуй и не вызывай инструменты Telegram/ВК.
```

- [ ] **Step 6: Пример брифа и скрипт запуска.**

`inbox/brief.example.md`:
```markdown
# Бриф на день
Неделя похолодания: акцент на обогрев кондиционером и тепловые завесы.
TG: 2 поста пользы. ВК: 1 экспертный пост.
Товары: инверторные кондиционеры с режимом обогрева, тепловые завесы — до 10 позиций.
Не трогать: стабилизаторы.
```

`scripts/morning-run.ps1`:
```powershell
# Утренний прогон оркестратора. Бриф: inbox/brief.md. Результат: runs/<дата>/.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$date = Get-Date -Format "yyyy-MM-dd"
$run = Join-Path $root "runs\$date"
New-Item -ItemType Directory -Force $run | Out-Null
Set-Location $root
claude -p "/morning-run $date" --output-format json |
    Out-File -Encoding utf8 (Join-Path $run "session.json")
Get-Content (Join-Path $run "summary.md") -ErrorAction SilentlyContinue
```

- [ ] **Step 7: Проверка — прогон без брифа.**
```powershell
cd C:\Users\TLT-1\Documents\GitHub\Agents_MY; Remove-Item inbox\brief.md -ErrorAction SilentlyContinue
powershell -ExecutionPolicy Bypass -File scripts\morning-run.ps1
```
Expected: на экране «Брифа нет — прогон пропущен»; `runs\<дата>\summary.md` существует.

- [ ] **Step 8: Проверка — боевой локальный прогон.**
```powershell
Copy-Item inbox\brief.example.md inbox\brief.md
powershell -ExecutionPolicy Bypass -File scripts\morning-run.ps1
python -c "import json,sys; s=json.load(open(sys.argv[1],encoding='utf-8-sig')); print('denials:', [d.get('tool_name') for d in s.get('permission_denials', [])])" runs\$(Get-Date -Format yyyy-MM-dd)\session.json
Get-ChildItem runs\$(Get-Date -Format yyyy-MM-dd) -Recurse -Name
```
Expected: есть `plan.json`, ≥ 1 файл в `research/`, ≥ 2 в `drafts/`, по вердикту на каждый черновик в `verdicts/`, `product_tasks.json`, `summary.md` с итоговой строкой. В `denials` нет ни одного `mcp__*` инструмента публикации (если есть — значит агент пытался публиковать: ужесточить промпт и повторить). Для каждого вердикта с `ok: true` вручную запустить `python tools/check_draft.py <draft>` → код 0.

- [ ] **Step 9: Commit.**
```bash
git add CLAUDE.md rules/brand.md .claude inbox/brief.example.md scripts/morning-run.ps1
git commit -m "feat: оркестратор, субагенты и /morning-run в локальном режиме

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 10: Показать владельцу** `runs/<дата>/summary.md` и 2–3 черновика: это первая проверка качества агентов до подключения к VPS (этап 2).

---

## Что дальше (отдельные планы)
- Этап 2: `factory_mcp.py` в content-factory (по образцу `tender_mcp.py`), `.mcp.json` в Agents_MY, замена `inbox/`/`runs/` на чтение брифа и отправку черновиков в `review`.
- Этап 3: `/day` в боте, Планировщик Windows, единая лента-дайджест.
- Этапы 4–5: вечерний отчёт, журнал доверия и поэтапная автономия.
