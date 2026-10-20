# Этап 2: Telegram-оркестратор и голос — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Отдельный Telegram-бот, которому владелец голосом или текстом раздаёт задачи на день. Бот ставит товарные задачи в content-factory тем же кодом, что `/make`, запускает и правит редакционные черновики, молчит днём и присылает сводку в 21:00.

**Architecture:** На VPS работает сервис `orchestrator_bot`. Он принимает сообщения только от владельца, расшифровывает голос (faster-whisper small с подсказкой из каталога) и складывает сообщения в SQLite-очередь. Наружу отдаёт локальный HTTP API, а действия выполняет через `content_factory.agent_cli` — новый модуль content-factory поверх `select_from_price` / `ExcelStore.select_items` / `make_cancel_excel_fn`. На ПК `listener` забирает очередь через SSH-туннель, на каждое сообщение вызывает `claude -p "/chat-turn …"` в песочнице этапа 1 и получает JSON-намерение. VPS проверяет намерение, применяет правила сомнения и выполняет его.

**Tech Stack:** Python 3.12 (VPS) / 3.13 (ПК), httpx, faster-whisper (CPU int8), ffmpeg, SQLite, stdlib `http.server`, pytest; Claude Code `claude -p`; systemd; Планировщик Windows.

**Spec:** `docs/superpowers/specs/2026-10-20-stage2-telegram-orchestrator-design.md` (родитель: `2026-10-13-content-factory-orchestrator-design.md`).

## Global Constraints

- Нейросеть не пишет в базу и не публикует: только JSON-намерение; исполняет VPS по белому списку типов `enqueue_products`, `cancel`, `run_editorial`, `revise_editorial`, `ask`.
- Никаких зашитых списков категорий, тем или брендов. Подсказка для распознавания и контекст для Claude строятся из текущего каталога (`agent_cli hint`).
- Автономия: уверенная задача ставится сразу, ответ «Поставил: … [Отменить]». Сомнение — вопрос с кнопками. Порог «подозрительно много» — 40 позиций (`ORCH_DOUBT_THRESHOLD`, по умолчанию 40).
- Под голосовым сразу «🎙 Расшифровываю…», затем «🎙 Услышал: …». Ошибка расшифровки — понятное сообщение, а не тишина.
- Днём бот пишет только в ответ или при сбое. Сводка — в 21:00 по Москве.
- Без ПК: heartbeat слушателя старше 120 с → «Принял, разберу, когда ПК будет на связи» (один раз на сообщение); `/status` и сводка работают без ПК.
- Сессия Claude — одна на день, новая с 04:00 по местному времени ПК.
- Публикация не меняется: товарные посты идут через канал проверки @Sendpr1ce_bot; редакционные — только предпросмотр.
- Токен бота — только в `/opt/agents-my/.env` (вводит владелец); в git и в чат не попадает. Владелец — Telegram user id `1264067528` (`ORCH_OWNER_CHAT_ID`).
- Любое действие на VPS (деплой, systemd, authorized_keys) — только с подтверждения владельца в момент выполнения.
- Код VPS совместим с Python 3.12. Новые зависимости VPS — только `httpx`, `faster-whisper`, `numpy` (свободно 7,2 ГБ диска, ≈ 2,4 ГБ памяти).
- Коммиты завершаются строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Сообщение или кнопка не от владельца → молча игнорируется, ничего не исполняется (тест в Task 4).
2. Слушатель забрал сообщение и упал → через 10 минут сообщение возвращается в очередь, а не теряется (тест в Task 3).
3. Повторная отправка того же намерения (сетевой ретрай слушателя) → действия выполняются один раз (тест в Task 3).
4. Голосовое не расшифровалось или расшифровка пустая → владелец видит «не разобрал, повторите или напишите текстом», в Claude ничего не уходит (тест в Task 4).
5. Долгий редакционный прогон (≈ 15–20 мин) не блокирует ответы в чате (тест в Task 8).

---

## File Structure

`content-factory` (ветка `feat/stage2-agent-cli` от `feat/aru-post-details`):
- `src/content_factory/agent_cli.py` — JSON CLI: `hint`, `resolve`, `enqueue`, `cancel`, `status`.
- `tests/test_agent_cli.py`.

`Agents_MY` (ветка `stage-2`):
- `orchestrator_bot/config.py` — настройки из окружения.
- `orchestrator_bot/store.py` — SQLite: очередь сообщений, журнал действий, ожидающие вопросы, heartbeat.
- `orchestrator_bot/intents.py` — разбор и проверка намерения, правила сомнения, тексты и кнопки ответа (чистые функции).
- `orchestrator_bot/factory.py` — вызовы `agent_cli` через subprocess.
- `orchestrator_bot/actions.py` — исполнение исходов, общее для API и кнопок.
- `orchestrator_bot/telegram_api.py` — тонкий клиент Bot API.
- `orchestrator_bot/transcribe.py` — faster-whisper + ffmpeg.
- `orchestrator_bot/bot.py` — обработка обновлений Telegram.
- `orchestrator_bot/api.py` — локальный HTTP API для слушателя.
- `orchestrator_bot/report.py` — вечерняя сводка.
- `orchestrator_bot/__main__.py` — `python -m orchestrator_bot run|report`.
- `listener/client.py`, `listener/session.py`, `listener/brain.py`, `listener/editorial.py`, `listener/tunnel.py`, `listener/__main__.py`.
- `.claude/commands/chat-turn.md`, `.claude/commands/revise-draft.md`.
- `deploy/orchestrator-bot.service`, `deploy/orchestrator-report.service`, `deploy/orchestrator-report.timer`, `deploy/INSTALL.md`.
- `scripts/listener.ps1`, `scripts/install-listener-task.ps1`, `requirements-vps.txt`, `requirements-pc.txt`.
- `tests/test_intents.py`, `tests/test_store_api.py`, `tests/test_bot.py`, `tests/test_transcribe.py`, `tests/test_factory_report.py`, `tests/test_listener.py`.
- `pyproject.toml`: `pythonpath = ["tools", "."]`.

---

### Task 1: `agent_cli` в content-factory

**Files:**
- Create: `C:\Users\TLT-1\Documents\GitHub\content-factory\src\content_factory\agent_cli.py`
- Test: `C:\Users\TLT-1\Documents\GitHub\content-factory\tests\test_agent_cli.py`

**Interfaces:**
- Consumes: `ingest.excel_price.load_price_slots(prices_dir, for_telegram=True) -> list[tuple[str, list[PriceItem]]]`, `search_items(items, phrase, taken, limit, aliases)`, `select_from_price(items, category, quotas, count, taken, aliases)`, `item_key`, `extract_model`, `load_search_aliases`; `ExcelStore.select_items(rows, due_at=None, queue_db=None)`; `bot.commands.parse_due_at(text, now)`; `bot.run.make_cancel_excel_fn(state_db, queue_db)`; множество `taken` строится как в `bot/run.py: make_make_fn`. Пути `state_db`, `prices_dir`, `queue_db` определяются так же, как в `bot/run.py` при запуске бота.
- Produces: `python -m content_factory.agent_cli <cmd> …` печатает одну строку JSON, код выхода 0 при успехе и 2 при ошибке входа (`{"error": "…"}`).
  - `hint` → `{"brands": [str], "sources": [str]}`: бренды по убыванию числа позиций, не больше 60; `sources` — метки слотов.
  - `resolve --category TEXT [--count N] [--quotas JSON] [--source SLOT]` → `{"total_matches": int, "items": [{"key","brand","model","name","price","source"}]}`, без побочных эффектов. `count` не задан — значит «все совпадения».
  - `enqueue` (те же аргументы) `[--due TEXT]` → `{"requested": int, "accepted": [key], "items": [...], "due_at": float|null}`.
  - `cancel --keys K1,K2` → `{"cancelled": int, "text": str}`.
  - `status [--since UNIX]` → `{"by_status": {status: int}, "published_since": int}`.

- [ ] **Step 1: Написать падающие тесты** `tests/test_agent_cli.py`. Фикстура `prices` — xlsx как в `tests/test_bot_run.py::test_make_fn_selects_and_stores`: Beko X100 30000, Candy Y200 25000 в разделе «Холодильники». Каждый тест вызывает `agent_cli.main([...])` и читает JSON из capsys.
  - `test_hint_lists_brands_from_current_prices` → `set(out["brands"]) == {"Beko", "Candy"}`, `out["sources"] == ["manual"]`.
  - `test_resolve_has_no_side_effects` → `total_matches == 2`; `ExcelStore(state).by_status("new") == []`.
  - `test_resolve_count_limits_items_but_not_total` (`--count 1`) → `len(items) == 1`, `total_matches == 2`.
  - `test_enqueue_selects_with_due_time` (`--due "завтра 12:00"`) → `set(out["accepted"]) == {"excel|beko|x100", "excel|candy|y200"}`, `out["due_at"]` равен `parse_due_at("завтра 12:00", now)`.
  - `test_enqueue_twice_accepts_nothing_second_time` → второй вызов: `accepted == []`.
  - `test_cancel_marks_items_cancelled` → после enqueue и cancel: `cancelled == 2`, `by_status("new") == []`.
  - `test_unknown_command_or_bad_quotas_exit_2` → `main(["nope"]) == 2`; `main(["resolve", "--category", "x", "--quotas", "{bad"]) == 2`, в выводе есть `"error"`.
- [ ] **Step 2: Запустить — упадут.** `cd C:\Users\TLT-1\Documents\GitHub\content-factory; .venv\Scripts\python -m pytest tests/test_agent_cli.py -q` → `ModuleNotFoundError: content_factory.agent_cli`.
- [ ] **Step 3: Реализовать** `main(argv: list[str] | None = None) -> int` и подкоманды argparse. Логика выбора — вызовы перечисленных функций без копирования их кода. `total_matches` — длина `search_items(…, limit=1000)`. `status.published_since` — из `PublishState`; если там нет времени записи, считать только `by_status`, а ключ `published_since` не выводить.
- [ ] **Step 4: Тесты зелёные.** Тот же pytest → `7 passed`. Весь набор: `.venv\Scripts\python -m pytest -q` → `865 passed`.
- [ ] **Step 5: Коммит** в ветку `feat/stage2-agent-cli`: `feat: agent_cli — JSON-интерфейс постановки задач для оркестратора`.

---

### Task 2: Намерения и правила сомнения (`orchestrator_bot/intents.py`)

**Files:**
- Create: `orchestrator_bot/__init__.py` (пустой), `orchestrator_bot/intents.py`
- Modify: `pyproject.toml` (`pythonpath = ["tools", "."]`)
- Test: `tests/test_intents.py`

**Interfaces:**
- Produces:
  - `class IntentError(ValueError)`.
  - `parse_intent(raw: str) -> dict` — первый JSON-объект в тексте (Claude может обернуть его в ```json). Нет объекта или не JSON → `IntentError`.
  - `normalize(intent: dict) -> tuple[str, list[dict]]` — возвращает `(reply, actions)`. Действия неизвестного типа отбрасываются. У `enqueue_products` обязательна непустая `category`; `count` — `int | None`; `quotas` — `dict[str, int | None]`; `day` — `"сегодня" | "завтра" | "YYYY-MM-DD" | None`; `at` — `"ЧЧ:ММ" | None`.
  - `due_text(action: dict) -> str | None` — `"завтра 12:00"`, `"сегодня 18:30"`, `"21.10 12:00"` или `None` (формат `parse_due_at`).
  - `@dataclass Outcome(kind: str, action: dict, text: str, options: list[str] = [], matches: int = 0)`, где `kind ∈ {"enqueue", "ask", "cancel", "run_editorial", "revise_editorial", "not_found"}`.
  - `plan(actions: list[dict], resolve: Callable[[dict], dict], threshold: int = 40) -> list[Outcome]` — правила сомнения: `total_matches == 0` → `not_found`; `count is None and total_matches > threshold` → `ask` с вариантами `[f"Первые {threshold}", f"Все {total_matches}", "Отмена"]`; иначе `enqueue`. Действие `ask` от Claude проходит как есть.
  - `render(reply: str, results: list[tuple[Outcome, str]]) -> tuple[str, list[list[dict]]]` — текст ответа (reply + строка на каждый результат) и inline-клавиатура. Кнопку «Отменить» получает каждый исполненный `enqueue` (callback `c:<log_id>`); каждый вариант `ask` даёт callback `a:<ask_id>:<n>`. `log_id` и `ask_id` подставляет вызывающий через текст результата — см. Task 3, `actions.execute`.

- [ ] **Step 1: Падающие тесты** `tests/test_intents.py`:
  - `test_parse_intent_accepts_fenced_json` → `parse_intent('```json\n{"reply":"ok","actions":[]}\n```')["reply"] == "ok"`.
  - `test_parse_intent_rejects_text` → `IntentError` для `"не понял"`.
  - `test_normalize_drops_unknown_and_invalid` → действие `{"type":"publish"}` и `enqueue_products` без category отброшены.
  - `test_due_text` → `due_text({"day":"завтра","at":"12:00"}) == "завтра 12:00"`; `due_text({"at":"18:30"}) == "сегодня 18:30"`; `due_text({}) is None`.
  - `test_plan_not_found_when_zero_matches` → `kind == "not_found"`.
  - `test_plan_asks_when_all_and_too_many` (`count=None`, `total_matches=57`) → `kind == "ask"`, `options == ["Первые 40", "Все 57", "Отмена"]`.
  - `test_plan_enqueues_explicit_count_even_if_many` (`count=10`, `total_matches=200`) → `kind == "enqueue"`.
  - `test_plan_passes_claude_ask_through`.
- [ ] **Step 2:** `python -m pytest tests/test_intents.py -q` → `ModuleNotFoundError: orchestrator_bot`.
- [ ] **Step 3:** Реализовать функции с указанными сигнатурами.
- [ ] **Step 4:** `python -m pytest -q` → все тесты проходят (29 прежних + 8 новых).
- [ ] **Step 5: Коммит** (ветка `stage-2`): `feat: разбор намерений и правила сомнения оркестратора`.

---

### Task 3: Хранилище, исполнение действий и локальный API

**Files:**
- Create: `orchestrator_bot/config.py`, `orchestrator_bot/store.py`, `orchestrator_bot/actions.py`, `orchestrator_bot/api.py`
- Test: `tests/test_store_api.py`

**Interfaces:**
- Consumes: `intents.normalize/plan/render/Outcome` (Task 2); объект `factory` с методами `hint() -> dict`, `resolve(action) -> dict`, `enqueue(action) -> dict`, `cancel(keys: list[str]) -> dict`, `status(since: float) -> dict` (реализация — Task 5, в тестах — фейк).
- Produces:
  - `config.Settings` (dataclass) и `Settings.from_env()`: `bot_token` (`ORCH_BOT_TOKEN`), `owner_chat_id` (`ORCH_OWNER_CHAT_ID`, int), `api_token` (`ORCH_API_TOKEN`), `db_path` (`ORCH_DB`, по умолчанию `/opt/agents-my/state/orchestrator.db`), `api_port` (`ORCH_API_PORT`, 8790), `cf_root` (`/opt/content-factory`), `cf_python` (`/opt/content-factory/.venv/bin/python`), `doubt_threshold` (40), `whisper_model` (`small`).
  - `store.Store(db_path)`:
    - `add_message(tg_message_id, kind, text) -> int`;
    - `set_transcript(msg_id, text)`;
    - `claim_next(now) -> dict | None` — самое старое `new` → `claimed`; заодно возвращает в `new` сообщения, которые висят в `claimed` дольше 600 с;
    - `mark_done(msg_id)`;
    - `heartbeat(now)`, `last_heartbeat() -> float | None`;
    - `log_action(msg_id, kind, payload: dict, result: dict) -> int`, `get_log(log_id) -> dict`, `actions_since(ts) -> list[dict]`;
    - `save_ask(msg_id, action, options) -> int`, `get_ask(ask_id) -> dict | None`;
    - `intent_seen(msg_id) -> bool`, `mark_intent(msg_id)` — идемпотентность;
    - `set_meta(key, value: str)`, `get_meta(key) -> str | None`, `count_new() -> int`.
  - `actions.execute(outcome, factory, store, msg_id) -> str`: исполняет `enqueue` (`factory.enqueue`, `log_action`, в текст добавляет маркер `[log:<id>]`), `cancel` (`factory.cancel` по ключам из `get_log`), `ask` (`save_ask`, маркер `[ask:<id>]`), `not_found` (только текст). `run_editorial` и `revise_editorial` возвращают «запущено на ПК» — их исполняет слушатель (Task 8).
  - `api.make_server(store, factory, send, token, port) -> ThreadingHTTPServer` на `127.0.0.1`, где `send(text, keyboard) -> None` отправляет в Telegram. Заголовок `X-Orch-Token` обязателен, иначе 401. Эндпоинты:
    - `POST /heartbeat`;
    - `GET /next` → `{"message": dict|null, "context": {"hint": factory.hint(), "today": store.actions_since(<начало дня>)}}`;
    - `POST /intent {"message_id", "intent"}` → если `intent_seen` — `{"ok": true, "duplicate": true}` без исполнения; иначе normalize → plan → execute → render → send; ответ `{"ok": true, "editorial": [действия run/revise для слушателя]}`;
    - `POST /reply {"text", "keyboard"?}` → send.

- [ ] **Step 1: Падающие тесты** `tests/test_store_api.py` (Store на `tmp_path`, сервер на порту 0, фейковые factory и send):
  - `test_claim_returns_oldest_and_requeues_stale` → после `claim_next(now)` сообщение в `claimed`; `claim_next(now + 601)` отдаёт его снова (Review Focus 2).
  - `test_api_rejects_missing_token` → 401.
  - `test_intent_enqueue_sends_reply_with_cancel_button` → фейковый `resolve` даёт `total_matches=2`; в `send` текст содержит «Поставил», кнопка имеет callback `c:<id>`; `factory.enqueue` вызван один раз.
  - `test_duplicate_intent_executes_once` → второй POST того же `message_id` даёт `duplicate: true`, `factory.enqueue` вызван ровно 1 раз (Review Focus 3).
  - `test_intent_with_doubt_saves_ask_with_buttons` (`count=None`, 57 совпадений) → кнопки `a:<ask_id>:0..2`, `factory.enqueue` не вызван.
  - `test_next_includes_catalog_hint` → `context.hint == factory.hint()`.
  - `test_editorial_actions_returned_to_listener` → `run_editorial` в ответе `/intent` попадает в `editorial`.
- [ ] **Step 2:** `python -m pytest tests/test_store_api.py -q` → `ModuleNotFoundError`.
- [ ] **Step 3:** Реализовать модули с указанными интерфейсами (stdlib `sqlite3`, `http.server`, `json`).
- [ ] **Step 4:** `python -m pytest -q` → все тесты проходят.
- [ ] **Step 5: Коммит:** `feat: очередь, исполнение действий и локальный API оркестратора`.

---

### Task 4: Telegram-бот (`telegram_api.py`, `bot.py`)

**Files:**
- Create: `orchestrator_bot/telegram_api.py`, `orchestrator_bot/bot.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `Store`, `actions.execute`, `intents.render` (Task 3/2); `transcriber(ogg: bytes, hint: str) -> str` (Task 5); `factory.hint()`.
- Produces:
  - `telegram_api.TelegramApi(token, http: httpx.Client)`: `get_updates(offset, timeout=25) -> list[dict]`, `send(chat_id, text, keyboard=None) -> int` (message_id), `edit(chat_id, message_id, text, keyboard=None)`, `download_voice(file_id) -> bytes`, `answer_callback(callback_id, text="")`.
  - `bot.Deps(store, tg, transcriber, factory, owner_chat_id, clock, heartbeat_stale_after=120)`.
  - `bot.handle_update(update: dict, deps: Deps) -> None`:
    - чужой `chat.id` или `from.id` → игнор;
    - текст → `add_message(kind="text")`;
    - голос → `send("🎙 Расшифровываю…")` → `transcriber(ogg, build_hint(factory.hint()))` → `edit("🎙 Услышал: …")` → `add_message(kind="voice")`;
    - пустая расшифровка или исключение → `edit("🎙 Не разобрал голосовое — повторите или напишите текстом")`, в очередь ничего не кладётся;
    - после постановки в очередь: если heartbeat старше `heartbeat_stale_after` или его нет → `send("Принял, разберу, когда ПК будет на связи")`;
    - команды без ПК: `/status` → текст из `factory.status(начало дня)` и heartbeat; `/report` → `report.build_report(...)` (Task 6);
    - `/start` и `/menu` → запасное кнопочное меню: «📋 Статус» (`m:status`), «🧾 Сводка» (`m:report`) и url-кнопка «🛠 Мастер задач» → `https://t.me/Sendpr1ce_bot`. Мастер `/task` не дублируется (анти-цель спека);
    - `bot.offline_alert(deps, now)` вызывается в цикле: если в очереди есть `new`-сообщения, а heartbeat старше 12 ч, отправляет «⚠️ ПК не на связи больше 12 часов, сообщений в очереди: N». Не чаще раза в 12 ч (время последней тревоги хранится в Store: `set_meta`/`get_meta`);
    - callback `c:<log_id>` → `factory.cancel(keys)` и ответ «Отменено: N»; `a:<ask_id>:<n>` → выбранный вариант исполняется через `actions.execute` («Первые 40» → enqueue с `count=40`, «Все N» → enqueue с `count=N`, «Отмена» → ничего). Повторное нажатие той же кнопки ничего не исполняет.
  - `bot.build_hint(hint: dict) -> str` — «Задачи контент-завода. Бренды и поставщики: <brands через запятую>. Источники: <sources>.» Без зашитых брендов.
  - `bot.run_forever(deps)` — цикл long-poll с offset.

- [ ] **Step 1: Падающие тесты** `tests/test_bot.py` (фейковый tg пишет вызовы в список, transcriber — лямбда):
  - `test_ignores_strangers` → сообщение и callback от `chat.id=999` ничего не добавляют и не отправляют (Review Focus 1).
  - `test_voice_echo_then_queue` → отправлено «🎙 Расшифровываю…», затем отредактировано в «🎙 Услышал: поставь стабилизаторы»; в очереди одно сообщение `kind="voice"`.
  - `test_failed_or_empty_transcript_not_queued` (исключение, затем `""`) → текст «Не разобрал»; очередь пуста (Review Focus 4).
  - `test_offline_notice_when_heartbeat_stale` → «Принял, разберу, когда ПК будет на связи»; при свежем heartbeat такого сообщения нет.
  - `test_cancel_callback_cancels_logged_keys` → `factory.cancel` вызван с ключами из журнала.
  - `test_ask_callback_first_n_enqueues_n_once` → «Первые 40» даёт enqueue с `count=40`; повторное нажатие не вызывает enqueue.
  - `test_build_hint_uses_catalog_only` → в результате есть бренды из переданного `hint` и нет других слов-брендов.
  - `test_menu_has_status_report_and_task_wizard_link` → три кнопки, у третьей `url == "https://t.me/Sendpr1ce_bot"`.
  - `test_offline_alert_once_per_12h_only_with_pending` → без очереди тревоги нет; с очередью и heartbeat 13 ч назад одна тревога; повторный вызов через час — тревоги нет.
- [ ] **Step 2:** `python -m pytest tests/test_bot.py -q` → `ModuleNotFoundError`.
- [ ] **Step 3:** Реализовать (`httpx` для Bot API; callback_data ≤ 64 байт обеспечивается короткими id).
- [ ] **Step 4:** `python -m pytest -q` → все тесты проходят.
- [ ] **Step 5: Коммит:** `feat: Telegram-бот оркестратора — приём, голос, кнопки, режим без ПК`.

---

### Task 5: Расшифровка и вызовы content-factory (`transcribe.py`, `factory.py`)

**Files:**
- Create: `orchestrator_bot/transcribe.py`, `orchestrator_bot/factory.py`, `requirements-vps.txt` (`httpx`, `faster-whisper`, `numpy`)
- Test: `tests/test_transcribe.py`

**Interfaces:**
- Produces:
  - `transcribe.decode_ogg(data: bytes) -> numpy.ndarray` — ffmpeg `-f s16le -ac 1 -ar 16000` → float32 (как в проверке 2026-10-20: `av` в faster-whisper несовместим, поэтому декодируем сами).
  - `transcribe.Transcriber(model_size="small", model_factory=None)`; `__call__(ogg: bytes, hint: str) -> str` — `language="ru"`, `vad_filter=True`, `beam_size=5`, `initial_prompt=hint`; модель загружается лениво при первом вызове.
  - `factory.Factory(cf_python, cf_root, runner=subprocess.run)` с методами из Task 3. Каждый метод запускает `[cf_python, "-m", "content_factory.agent_cli", ...]` с `cwd=cf_root`, таймаутом 120 с и разбирает JSON. Ненулевой код или не-JSON → `FactoryError(stderr_tail)`. `enqueue` передаёт `--due intents.due_text(action)`, если он не `None`.

- [ ] **Step 1: Падающие тесты** `tests/test_transcribe.py`:
  - `test_transcriber_passes_hint_and_language` → фейковая модель записала `initial_prompt == hint`, `language == "ru"`; результат — склейка сегментов через пробел.
  - `test_transcriber_loads_model_once` → два вызова, `model_factory` вызван один раз.
  - `test_decode_ogg_uses_ffmpeg` (пропускается, если `ffmpeg` нет в PATH) → на 1 с тишины из `ffmpeg -f lavfi -i anullsrc` массив длиной ≈ 16000.
  - `test_factory_builds_cli_args_and_parses_json` → фейковый runner получил `[..., "agent_cli", "enqueue", "--category", "стабилизаторы", "--due", "сегодня 12:00"]` и вернул JSON.
  - `test_factory_raises_on_nonzero_exit` → `FactoryError`.
- [ ] **Step 2:** `python -m pytest tests/test_transcribe.py -q` → `ModuleNotFoundError`.
- [ ] **Step 3:** Реализовать. `faster_whisper` импортировать внутри `model_factory` по умолчанию — тогда тесты на ПК не требуют пакета.
- [ ] **Step 4:** `python -m pytest -q` → все тесты проходят.
- [ ] **Step 5: Коммит:** `feat: расшифровка голоса с подсказкой из каталога и вызовы agent_cli`.

---

### Task 6: Вечерняя сводка и запуск сервиса (`report.py`, `__main__.py`, deploy-файлы)

**Files:**
- Create: `orchestrator_bot/report.py`, `orchestrator_bot/__main__.py`, `deploy/orchestrator-bot.service`, `deploy/orchestrator-report.service`, `deploy/orchestrator-report.timer`
- Test: `tests/test_factory_report.py`

**Interfaces:**
- Produces:
  - `report.build_report(status: dict, actions: list[dict], last_heartbeat: float | None, now: float) -> str`. Секции: «📋 Итог дня», поставлено задач (число и позиции из `actions` с kind `enqueue`, минус отменённые), статусы конвейера из `status["by_status"]`, опубликовано (если есть `published_since`), редакционные черновики (actions `run_editorial`/`revise_editorial`), «ПК: на связи / не был на связи с ЧЧ:ММ». Пустой день → «Сегодня задач не было».
  - `python -m orchestrator_bot run` — `Settings.from_env()`, Store, Factory, Transcriber, API-сервер в потоке, `bot.run_forever`.
  - `python -m orchestrator_bot report` — собирает сводку и отправляет владельцу.
  - Юниты: `orchestrator-bot.service` (`WorkingDirectory=/opt/agents-my`, `EnvironmentFile=/opt/agents-my/.env`, `ExecStart=/opt/agents-my/.venv/bin/python -m orchestrator_bot run`, `Restart=always`); `orchestrator-report.timer` (`OnCalendar=*-*-* 21:00:00 Europe/Moscow`) → `orchestrator-report.service` (oneshot `… -m orchestrator_bot report`). Скрипты в `ExecStart` не используются (урок `tests/test_deploy_units.py`).

- [ ] **Step 1: Падающие тесты** `tests/test_factory_report.py`:
  - `test_report_counts_enqueued_minus_cancelled`;
  - `test_report_empty_day` → «Сегодня задач не было»;
  - `test_report_marks_pc_offline` (heartbeat 3 часа назад) → «ПК: не был на связи с»;
  - `test_units_have_no_direct_shell_scripts` → в `ExecStart` юнитов `deploy/*.service` нет путей, оканчивающихся на `.sh`.
- [ ] **Step 2:** `python -m pytest tests/test_factory_report.py -q` → падает.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** `python -m pytest -q` → все тесты проходят.
- [ ] **Step 5: Коммит:** `feat: вечерняя сводка и юниты сервиса оркестратора`.

---

### Task 7: Команды Claude для чата (`chat-turn`, `revise-draft`)

**Files:**
- Create: `.claude/commands/chat-turn.md`, `.claude/commands/revise-draft.md`
- Modify: `CLAUDE.md` (раздел «Чат-режим»)

**Interfaces:**
- Produces:
  - `/chat-turn <путь к JSON>`. Вход `{"message": {"id", "kind", "text"}, "context": {"hint", "today"}}`. Выход — **только** JSON-намерение по схеме спека (типы из Global Constraints). Правила для модели:
    - сопоставлять слова владельца только с `context.hint` и не выдумывать бренды или категории;
    - «все» — значит `count: null`;
    - время «до обеда» — `at: "12:00"`, «после обеда» — `at: "15:00"`, «вечер» — `at: "18:00"`, если владелец не назвал другое;
    - сомнение по смыслу — действие `ask`;
    - вопрос о статусе — `reply` по `context.today`, без действий;
    - редакционные просьбы — `run_editorial`; правка ответом на черновик — `revise_editorial` с `draft_id` из текста цитаты;
    - цен и характеристик не писать.
  - `/revise-draft <дата> <draft_id> <путь к notes>`: tg-editor или vk-editor переписывает `runs/<дата>/drafts/<draft_id>.json` с пометками в новый `<draft_id>-v2.json` (дальше `-v3` и т. д.), затем critic проверяет; в конце выводится путь к новому черновику и вердикт.

- [ ] **Step 1: Проверочный прогон** (вместо unit-теста; нужен ПК и лимит подписки). Создать `runs/chat/test-1.json` с текстом «до обеда поставь все стабилизаторы ресанта, после обеда 10 холодильников beko» и `hint` с брендами `["Ресанта", "Beko", "Indesit"]`. Запустить:
  `claude -p "/chat-turn runs/chat/test-1.json" --setting-sources project --settings scripts/headless-settings.json --strict-mcp-config --output-format json`
  Ожидается: `result` разбирается `intents.parse_intent`; два `enqueue_products` — стабилизаторы с `quotas {"ресанта": null}` или в `category`, `count: null`, `at: "12:00"`; холодильники с `count: 10`, `quotas {"beko": 10}`, `at: "15:00"`; `permission_denials` пуст.
- [ ] **Step 2:** Второй прогон с текстом «что сегодня поставлено?» и непустым `today` → `actions == []`, в `reply` перечислены позиции.
- [ ] **Step 3: Коммит:** `feat: команды Claude для чат-режима и правки черновика`.

---

### Task 8: Слушатель на ПК (`listener/`)

**Files:**
- Create: `listener/__init__.py`, `listener/client.py`, `listener/session.py`, `listener/brain.py`, `listener/editorial.py`, `listener/tunnel.py`, `listener/__main__.py`, `scripts/listener.ps1`, `scripts/install-listener-task.ps1`, `requirements-pc.txt` (`httpx`)
- Test: `tests/test_listener.py`

**Interfaces:**
- Consumes: HTTP API (Task 3), `intents.parse_intent` (Task 2), команды из Task 7, `/morning-run` (этап 1).
- Produces:
  - `session.day_session(now: datetime, state_path: Path) -> tuple[str, bool]` — uuid сессии и флаг «новая». Граница дня — 04:00 местного времени.
  - `brain.run_turn(payload: dict, session: tuple[str, bool], runner=subprocess.run, root=Path(".")) -> dict`: пишет `runs/chat/<id>.json`, запускает claude с флагами песочницы плюс `--session-id U` (новая) или `--resume U`, разбирает `result`. При сбое возвращает `{"reply": "Не смог разобрать — повторите, пожалуйста", "actions": []}`.
  - `editorial.EditorialWorker(runner, post)` — один фоновый поток и очередь заданий. `submit(action)` возвращается сразу.
    - `run_editorial` пишет `inbox/brief.md` и запускает `/morning-run <дата>`, затем отправляет `summary.md` и тексты принятых черновиков через `post(text, keyboard)`.
    - `revise_editorial` запускает `/revise-draft` и отправляет новый черновик.
    - Одновременно выполняется не больше одного задания.
  - `client.ApiClient(base_url, token, http)` — `next()`, `intent(message_id, intent)`, `reply(text, keyboard=None)`, `heartbeat()`.
  - `tunnel.Tunnel(args: list[str])` — `ensure()` перезапускает `ssh -N -L 8790:127.0.0.1:8790 …`, если процесс умер.
  - `python -m listener` — цикл: heartbeat раз в 30 с; `next()` каждые 3 с; на сообщение `run_turn` → `intent()` → редакционные действия из ответа уходят в `EditorialWorker.submit`.
  - `scripts/install-listener-task.ps1` регистрирует задачу Планировщика «AgentsMY Listener» при входе пользователя: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\listener.ps1`.

- [ ] **Step 1: Падающие тесты** `tests/test_listener.py`:
  - `test_day_session_resets_at_4am` → 03:59 и 04:01 одного дня дают разные uuid; 04:01 и 23:00 — один и тот же; второй вызов в тот же день даёт `is_new=False`.
  - `test_run_turn_uses_resume_for_existing_session` → в аргументах runner есть `--resume <uuid>` и флаги песочницы `--setting-sources project`, `--settings scripts/headless-settings.json`, `--strict-mcp-config`.
  - `test_run_turn_falls_back_on_garbage` → runner вернул `result: "не json"` → reply «Не смог разобрать…».
  - `test_editorial_does_not_block_chat` → фейковый runner для `/morning-run` ждёт `threading.Event`; во время ожидания цикл слушателя обрабатывает чат-сообщение и вызывает `intent()` (Review Focus 5).
  - `test_editorial_posts_summary_and_drafts` → `post` получил текст из `summary.md` и по одному сообщению на каждый принятый черновик.
- [ ] **Step 2:** `python -m pytest tests/test_listener.py -q` → `ModuleNotFoundError`.
- [ ] **Step 3:** Реализовать.
- [ ] **Step 4:** `python -m pytest -q` → все тесты проходят.
- [ ] **Step 5: Коммит:** `feat: слушатель на ПК — чат через Claude, фоновые редакционные прогоны, туннель`.

---

### Task 9: Деплой и приёмка (VPS и ПК, по шагам с подтверждением владельца)

**Files:**
- Create: `deploy/INSTALL.md` (команды этой задачи, без секретов)

**Interfaces:**
- Consumes: всё выше. Для content-factory — `deploy/DEPLOY.md` §1–4.

- [ ] **Step 1: Владелец создаёт бота.** В @BotFather: `/newbot`, имя «СплитХаб · оркестратор», username на выбор владельца (например, `splithub_orchestrator_bot`). Затем закрепить чат с ботом.
- [ ] **Step 2 (VPS, подтверждение): выкладка канона content-factory.**
  - Бэкап: `ssh sprintbox-itp 'cd /opt/content-factory && tar -czf backups/pre-stage2-$(date +%F).tgz src deploy config'`.
  - Выкладка `feat/stage2-agent-cli` по `DEPLOY.md` §1 (tar через stdin, без `state/` и `.env`).
  - `systemctl restart cf-bot`.
  - Проверки: дымовые команды §4 и `ssh sprintbox-itp '/opt/content-factory/.venv/bin/python -m content_factory.agent_cli hint'` → JSON с брендами.
  - Откат — распаковка бэкапа и `systemctl restart cf-bot`.
- [ ] **Step 3 (VPS, подтверждение): установка сервиса.**
  - `/opt/agents-my`: код через tar, `python3 -m venv .venv`, `.venv/bin/pip install -r requirements-vps.txt`, `mkdir -p state`.
  - В `.env`: `ORCH_OWNER_CHAT_ID=1264067528`, `ORCH_API_TOKEN=<случайный, генерирует openssl rand -hex 24 на сервере>`.
  - Токен бота владелец вводит сам: `ssh -t sprintbox-itp 'read -rsp "Токен бота: " T && echo && echo "ORCH_BOT_TOKEN=$T" >> /opt/agents-my/.env && chmod 600 /opt/agents-my/.env'`.
- [ ] **Step 4 (VPS, подтверждение): ключ туннеля.**
  - На ПК: `ssh-keygen -t ed25519 -f ~/.ssh/id_agents_tunnel -N "" -C agents-tunnel`.
  - На сервер в `/root/.ssh/authorized_keys` — строка `restrict,port-forwarding,permitopen="127.0.0.1:8790",command="/bin/false" <pubkey>`.
  - Проверка: `ssh -i ~/.ssh/id_agents_tunnel -p 2222 root@212.116.115.150 true` отказывает в shell, а `ssh … -N -L 8790:127.0.0.1:8790` держит туннель.
- [ ] **Step 5 (VPS, подтверждение): юниты.** Скопировать три юнита, `systemctl daemon-reload`, `systemctl enable --now orchestrator-bot.service orchestrator-report.timer`, проверить `journalctl -u orchestrator-bot -n 30`.
- [ ] **Step 6 (ПК):** `pip install -r requirements-pc.txt`, в `scripts/listener.ps1` указать путь к ключу туннеля и `ORCH_API_TOKEN` (берётся из пользовательской переменной окружения Windows, не из файла в git), запустить `scripts/install-listener-task.ps1`.
- [ ] **Step 7: Приёмка с владельцем.**
  1. Текст «что сегодня поставлено?» → ответ без действий.
  2. Голосовое «до обеда поставь 2 холодильника beko» → «🎙 Расшифровываю…» → «🎙 Услышал: …» → «Поставил: … [Отменить]». Позиции появились в `/excel` @Sendpr1ce_bot. «Отменить» снимает их.
  3. Голосовое «поставь все стабилизаторы» при > 40 совпадениях → вопрос с кнопками.
  4. Остановить слушатель → сообщение → «Принял, разберу, когда ПК будет на связи». Запустить слушатель → сообщение обработано.
  5. «Сделай один экспертный пост для ВК про тепловые насосы» → чат продолжает отвечать, через 15–20 мин приходят итог и черновик. Голосовой ответ на черновик → новая версия.
  6. В 21:00 приходит сводка.
- [ ] **Step 8: Коммит** `deploy/INSTALL.md` и итог приёмки в `docs/reports/2026-10-XX-stage2-acceptance.md`.
