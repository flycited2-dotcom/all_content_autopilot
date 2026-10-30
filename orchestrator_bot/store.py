"""SQLite-хранилище оркестратора: очередь сообщений, журнал действий, вопросы, мета."""
from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import time
from pathlib import Path

CLAIM_TIMEOUT = 600          # слушатель забрал и пропал — через 10 минут сообщение снова в очереди
ALBUM_QUIET_SECONDS = 2
ALBUM_MAX_IMAGES = 8
ALBUM_MAX_BYTES = 20 * 1024 * 1024
ALBUM_IMAGE_MAX_BYTES = 10 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, tg_message_id INTEGER, kind TEXT, text TEXT,
    status TEXT NOT NULL DEFAULT 'new', created REAL, claimed_at REAL,
    intent_done INTEGER NOT NULL DEFAULT 0, response TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_tg ON messages(tg_message_id) WHERE tg_message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS message_aliases (
    tg_message_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY, msg_id INTEGER, kind TEXT, payload TEXT, result TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS asks (
    id INTEGER PRIMARY KEY, msg_id INTEGER, action TEXT, options TEXT, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS batches (
    log_id INTEGER PRIMARY KEY, category TEXT, keys TEXT, publish_at REAL,
    status TEXT NOT NULL DEFAULT 'open', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS drafts (
    n INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, base TEXT, channel TEXT, title TEXT, text TEXT,
    visual_prompt TEXT, image_job INTEGER, image_path TEXT, status TEXT NOT NULL DEFAULT 'new',
    control_message_id INTEGER, result TEXT, created REAL, updated REAL);
"""
DRAFT_FIELDS = ("image_job", "image_path", "status", "control_message_id", "result", "visual_prompt", "final_text",
                "publish_at")
_MIGRATIONS = ("ALTER TABLE drafts ADD COLUMN final_text TEXT", "ALTER TABLE drafts ADD COLUMN publish_at REAL",
               "ALTER TABLE messages ADD COLUMN attachments TEXT",
               "ALTER TABLE messages ADD COLUMN media_group_id TEXT")


class Store:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as db:
            db.executescript(_SCHEMA)
            for ddl in _MIGRATIONS:
                try:
                    db.execute(ddl)
                except sqlite3.OperationalError:
                    pass                             # колонка уже есть

    def _c(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return _Closing(db)

    # ── очередь ──
    def add_message(self, tg_message_id, kind, text, now=None, attachments=None) -> int | None:
        """None — это сообщение Telegram уже в очереди (повторная доставка после перезапуска)."""
        with self._c() as db:
            cur = db.execute("INSERT OR IGNORE INTO messages(tg_message_id, kind, text, created, attachments) VALUES(?,?,?,?,?)",
                             (tg_message_id, kind, text, now or time.time(),
                              json.dumps(attachments, ensure_ascii=False) if attachments else None))
            return cur.lastrowid if cur.rowcount == 1 else None

    def has_tg_message(self, tg_message_id) -> bool:
        with self._c() as db:
            return db.execute("SELECT 1 FROM messages WHERE tg_message_id=? UNION ALL "
                              "SELECT 1 FROM message_aliases WHERE tg_message_id=? LIMIT 1",
                              (tg_message_id, tg_message_id)).fetchone() is not None

    def add_photo_message(self, tg_message_id, text, attachment, media_group_id=None, now=None):
        """Durably collect a Telegram album; (id, created) or None on redelivery."""
        if not media_group_id:
            msg_id = self.add_message(tg_message_id, "photo", text, now=now, attachments=[attachment])
            return (msg_id, True) if msg_id is not None else None
        now = time.time() if now is None else now
        group = str(media_group_id)
        with self._c() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("SELECT 1 FROM messages WHERE tg_message_id=? UNION ALL "
                                   "SELECT 1 FROM message_aliases WHERE tg_message_id=? LIMIT 1",
                                   (tg_message_id, tg_message_id)).fetchone()
            if duplicate:
                db.execute("COMMIT")
                return None
            row = db.execute("SELECT * FROM messages WHERE media_group_id=? "
                             "AND status='collecting' AND intent_done=0 ORDER BY id DESC LIMIT 1",
                             (group,)).fetchone()
            attachments = json.loads(row["attachments"]) if row else []
            attachments.append(attachment)
            _validate_album(attachments)
            encoded = json.dumps(attachments, ensure_ascii=False)
            if row:
                old_text = row["text"] or ""
                if old_text == "Фотография поставщика":
                    combined = text
                elif text and text != "Фотография поставщика" and text not in old_text.split("\n"):
                    combined = old_text + "\n" + text
                else:
                    combined = old_text
                if len(combined) > 10000:
                    raise ValueError("Слишком длинные подписи к альбому — максимум 10000 символов")
                msg_id = row["id"]
                db.execute("UPDATE messages SET attachments=?,text=?,status='collecting',claimed_at=? WHERE id=?",
                           (encoded, combined, now, msg_id))
            else:
                msg_id = db.execute("INSERT INTO messages(tg_message_id,kind,text,status,created,claimed_at,"
                                    "attachments,media_group_id) VALUES(?,'photo',?,'collecting',?,?,?,?)",
                                    (tg_message_id, text, now, now, encoded, group)).lastrowid
            db.execute("INSERT INTO message_aliases(tg_message_id,message_id) VALUES(?,?)", (tg_message_id, msg_id))
            db.execute("COMMIT")
        return msg_id, row is None

    def get_message(self, msg_id) -> dict | None:
        """Read the original durable message when validating a model action."""
        with self._c() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if not row:
            return None
        message = {k: row[k] for k in ("id", "tg_message_id", "kind", "text", "created")}
        if row["attachments"]:
            message["attachments"] = json.loads(row["attachments"])
        if row["media_group_id"]:
            message["media_group_id"] = row["media_group_id"]
        return message

    def recent_photo_attachments(self, before_id, limit=3, max_age=1800) -> list[dict]:
        """Recent photos can precede an instruction; never use later or stale images."""
        current = self.get_message(before_id)
        if current is None or limit <= 0 or max_age <= 0:
            return []
        with self._c() as db:
            rows = db.execute("SELECT id,attachments FROM messages WHERE id < ? AND kind='photo' "
                              "AND attachments IS NOT NULL AND created >= ? AND created <= ? "
                              "ORDER BY id DESC LIMIT ?",
                              (before_id, current["created"] - max_age, current["created"], limit)).fetchall()
        groups, count, size = [], 0, 0
        for row in rows:
            group = [{**attachment, "source_message_id": row["id"]}
                     for attachment in json.loads(row["attachments"])]
            group_size = sum(len(a.get("data", "")) * 3 // 4 for a in group)
            if count + len(group) > min(limit, 8) or size + group_size > 20 * 1024 * 1024:
                break  # Keep whole recent albums; never silently cut an album in half.
            groups.append(group)
            count += len(group)
            size += group_size
        return [attachment for group in reversed(groups) for attachment in group]

    def save_response(self, msg_id, response: dict):
        with self._c() as db:
            db.execute("UPDATE messages SET response=? WHERE id=?", (json.dumps(response, ensure_ascii=False), msg_id))

    def get_response(self, msg_id) -> dict | None:
        with self._c() as db:
            row = db.execute("SELECT response FROM messages WHERE id=?", (msg_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def record_exchange(self, text: str, reply: str):
        """Ответ кнопкой, исполненный без модели; сразу завершён, не попадает в очередь."""
        with self._c() as db:
            db.execute("INSERT INTO messages(kind,text,status,created,intent_done,response) VALUES(?,?,'done',?,1,?)",
                       ("callback", text, time.time(), json.dumps({"reply": reply}, ensure_ascii=False)))

    def dialogue_before(self, msg_id: int, limit=6) -> list[dict]:
        """История только до текущей команды, включая фактические ответы сервера."""
        with self._c() as db:
            rows = db.execute("SELECT text,response FROM messages WHERE id < ? AND status='done' "
                              "AND response IS NOT NULL ORDER BY id DESC LIMIT ?", (msg_id, limit)).fetchall()
        history = []
        for row in reversed(rows):
            reply = json.loads(row["response"]).get("reply")
            if not isinstance(reply, str):
                continue  # старые версии API сохраняли только ok/editorial
            history.extend([{"role": "user", "content": (row["text"] or "")[:4000]},
                            {"role": "assistant", "content": reply[:8000]}])
        return history

    def set_transcript(self, msg_id, text):
        with self._c() as db:
            db.execute("UPDATE messages SET text=? WHERE id=?", (text, msg_id))

    def claim_next(self, now=None) -> dict | None:
        now = now or time.time()
        with self._c() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE messages SET status='new' WHERE status='claimed' AND claimed_at < ?",
                       (now - CLAIM_TIMEOUT,))
            db.execute("UPDATE messages SET status='new' WHERE status='collecting' AND claimed_at <= ?",
                       (now - ALBUM_QUIET_SECONDS,))
            row = db.execute("SELECT * FROM messages WHERE status IN ('new','collecting') ORDER BY id LIMIT 1").fetchone()
            if row and row["status"] == "collecting":
                row = None  # Keep a following instruction behind its unfinished album.
            if row:
                db.execute("UPDATE messages SET status='claimed', claimed_at=? WHERE id=?", (now, row["id"]))
            db.execute("COMMIT")
        if not row:
            return None
        message = {k: row[k] for k in ("id", "tg_message_id", "kind", "text", "created")}
        if row["attachments"]:
            message["attachments"] = json.loads(row["attachments"])
        if row["media_group_id"]:
            message["media_group_id"] = row["media_group_id"]
        return message

    def mark_done(self, msg_id):
        with self._c() as db:
            db.execute("UPDATE messages SET status='done' WHERE id=?", (msg_id,))

    def count_new(self) -> int:
        with self._c() as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE status IN ('new','claimed','collecting')").fetchone()[0]

    def intent_seen(self, msg_id) -> bool:
        with self._c() as db:
            row = db.execute("SELECT intent_done FROM messages WHERE id=?", (msg_id,)).fetchone()
        return bool(row and row[0])

    def mark_intent(self, msg_id) -> bool:
        """Атомарно отметить намерение; False — если оно уже было исполнено."""
        with self._c() as db:
            cur = db.execute("UPDATE messages SET intent_done=1 WHERE id=? AND intent_done=0", (msg_id,))
        return cur.rowcount == 1

    # ── журнал действий ──
    def log_action(self, msg_id, kind, payload: dict, result: dict, now=None) -> int:
        with self._c() as db:
            return db.execute("INSERT INTO actions_log(msg_id, kind, payload, result, ts) VALUES(?,?,?,?,?)",
                              (msg_id, kind, json.dumps(payload, ensure_ascii=False),
                               json.dumps(result, ensure_ascii=False), now or time.time())).lastrowid

    def get_log(self, log_id) -> dict | None:
        with self._c() as db:
            row = db.execute("SELECT * FROM actions_log WHERE id=?", (log_id,)).fetchone()
        return _log_row(row) if row else None

    def actions_since(self, ts) -> list[dict]:
        with self._c() as db:
            rows = db.execute("SELECT * FROM actions_log WHERE ts >= ? ORDER BY id", (ts,)).fetchall()
        return [_log_row(r) for r in rows]

    def find_supplier_selection(self, msg_id, action, manifest_path) -> dict | None:
        with self._c() as db:
            rows = db.execute("SELECT * FROM actions_log WHERE msg_id=? AND kind='enqueue' ORDER BY id DESC",
                              (msg_id,)).fetchall()
        for row in rows:
            entry = _log_row(row)
            if entry["payload"] == action and entry["result"].get("manifest_path") == manifest_path:
                return entry
        return None

    # ── вопросы с кнопками ──
    def save_ask(self, msg_id, action: dict, options: list[str]) -> int:
        with self._c() as db:
            for row in db.execute('SELECT id,action,options FROM asks WHERE msg_id=? ORDER BY id DESC', (msg_id,)):
                if json.loads(row['action']) == action and json.loads(row['options']) == options:
                    return row['id']
            return db.execute("INSERT INTO asks(msg_id, action, options) VALUES(?,?,?)",
                              (msg_id, json.dumps(action, ensure_ascii=False),
                               json.dumps(options, ensure_ascii=False))).lastrowid

    def get_ask(self, ask_id) -> dict | None:
        with self._c() as db:
            row = db.execute("SELECT * FROM asks WHERE id=?", (ask_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "msg_id": row["msg_id"], "action": json.loads(row["action"]),
                "options": json.loads(row["options"]), "used": bool(row["used"])}

    def use_ask(self, ask_id) -> bool:
        """Атомарно отметить ответ на вопрос; False — кнопку уже нажимали."""
        with self._c() as db:
            cur = db.execute("UPDATE asks SET used=1 WHERE id=? AND used=0", (ask_id,))
        return cur.rowcount == 1

    # ── мета и heartbeat ──
    def set_meta(self, key, value: str):
        with self._c() as db:
            db.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    def get_meta(self, key) -> str | None:
        with self._c() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def heartbeat(self, now=None):
        self.set_meta("heartbeat", str(now or time.time()))

    def last_heartbeat(self) -> float | None:
        value = self.get_meta("heartbeat")
        return float(value) if value else None


    # ── партии товаров: одна задача — одна публикация в Telegram пачкой ──
    def add_batch(self, log_id: int, category: str, keys: list[str], publish_at: float | None, now=None):
        now = now or time.time()
        with self._c() as db:
            db.execute("INSERT OR IGNORE INTO batches(log_id, category, keys, publish_at, created, updated) "
                       "VALUES(?,?,?,?,?,?)", (log_id, category, json.dumps(keys, ensure_ascii=False),
                                               publish_at, now, now))

    def batches(self, status: str) -> list[dict]:
        with self._c() as db:
            rows = db.execute("SELECT * FROM batches WHERE status=? ORDER BY log_id", (status,)).fetchall()
        return [{**dict(r), "keys": json.loads(r["keys"])} for r in rows]

    def get_batch(self, log_id: int) -> dict | None:
        with self._c() as db:
            r = db.execute("SELECT * FROM batches WHERE log_id=?", (log_id,)).fetchone()
        return {**dict(r), "keys": json.loads(r["keys"])} if r else None

    def move_batch(self, log_id: int, src: tuple, dst: str, now=None, *, expected_updated=None) -> bool:
        marks = ",".join("?" for _ in src)
        revision = " AND updated=?" if expected_updated is not None else ""
        params = (dst, now or time.time(), log_id, *src)
        if expected_updated is not None:
            params += (expected_updated,)
        with self._c() as db:
            cur = db.execute(f"UPDATE batches SET status=?, updated=? WHERE log_id=? AND status IN ({marks})" + revision,
                             params)
        return cur.rowcount == 1

    def reopen_recovered_batches(self, key: str, now=None, *, recovery_id=None) -> int:
        """Refresh an undecided batch once per recovered audit; keep owner decisions."""
        if recovery_id is not None and (not isinstance(recovery_id, str) or not recovery_id):
            raise ValueError('Нужен идентификатор восстановления')
        marker = 'batch-photo-recovery:' + json.dumps([key, recovery_id]) if recovery_id is not None else None
        changed = 0
        with self._c() as db:
            db.execute('BEGIN IMMEDIATE')
            if marker and db.execute('SELECT 1 FROM meta WHERE key=?', (marker,)).fetchone():
                return 0
            states = ('failed', 'ready', 'open') if marker else ('failed', 'ready')
            marks = ','.join('?' for _ in states)
            rows = db.execute(f'SELECT log_id,keys,updated FROM batches WHERE status IN ({marks})', states).fetchall()
            for row in rows:
                if key in json.loads(row['keys']):
                    # Advancing an already-open batch also invalidates a concurrent stale poll.
                    updated = max(now or time.time(), row['updated'] + 0.000001)
                    changed += db.execute(f"UPDATE batches SET status='open',updated=? WHERE log_id=? AND status IN ({marks})",
                                          (updated, row['log_id'], *states)).rowcount
            if marker:
                db.execute('INSERT INTO meta(key,value) VALUES(?,?)', (marker, str(now or time.time())))
            db.commit()
        return changed

    # ── черновики оркестратора (этап 3) ──
    def add_draft(self, draft: dict, base: str, now=None) -> bool:
        """False — эта версия уже пришла (повтор от слушателя)."""
        now = now or time.time()
        with self._c() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO drafts(id, base, channel, title, text, visual_prompt, created, updated) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (draft["id"], base, draft["channel"], draft.get("title", ""), draft["text"],
                 draft.get("visual_prompt", ""), now, now))
        return cur.rowcount == 1

    def get_draft(self, key) -> dict | None:
        """key — id черновика (str) или номер строки n (int, для кнопок: callback_data ≤ 64 байт)."""
        column = "n" if isinstance(key, int) else "id"
        with self._c() as db:
            row = db.execute(f"SELECT * FROM drafts WHERE {column}=?", (key,)).fetchone()
        return dict(row) if row else None

    def update_draft(self, n: int, now=None, **fields):
        bad = set(fields) - set(DRAFT_FIELDS)
        if bad:
            raise ValueError(f"поля черновика: {bad}")
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._c() as db:
            db.execute(f"UPDATE drafts SET {sets}, updated=? WHERE n=?", (*fields.values(), now or time.time(), n))

    def move_draft(self, n: int, src: tuple, dst: str, now=None) -> bool:
        """Атомарный переход статуса: False — черновик уже не в src (повторное нажатие)."""
        marks = ",".join("?" for _ in src)
        with self._c() as db:
            cur = db.execute(f"UPDATE drafts SET status=?, updated=? WHERE n=? AND status IN ({marks})",
                             (dst, now or time.time(), n, *src))
        return cur.rowcount == 1

    def drafts_with_status(self, status) -> list[dict]:
        with self._c() as db:
            return [dict(r) for r in db.execute("SELECT * FROM drafts WHERE status=? ORDER BY n", (status,))]

    def drafts_summary(self, since: float) -> dict:
        """Для статуса и вечерней сводки: сколько ждут ✅, рисуются, и что опубликовано с since."""
        with self._c() as db:
            counts = dict(db.execute("SELECT status, COUNT(*) FROM drafts GROUP BY status").fetchall())
            done = [r[0] for r in db.execute(
                "SELECT result FROM drafts WHERE status='published' AND updated>=? ORDER BY updated", (since,))]
        return {"ready": counts.get("ready", 0), "visual": counts.get("visual", 0), "published": done}

    def sibling_prompt(self, base: str, exclude_id: str, marker: str) -> str | None:
        """Последнее описание картинки другой версии того же поста, в котором есть пожелания владельца."""
        with self._c() as db:
            row = db.execute("SELECT visual_prompt FROM drafts WHERE base=? AND id<>? AND visual_prompt LIKE ? "
                             "ORDER BY updated DESC LIMIT 1", (base, exclude_id, f"%{marker}%")).fetchone()
        return row[0] if row else None

    def supersede_versions(self, base: str, keep_n: int, now=None) -> list[int]:
        """Остальные версии того же черновика больше не публикуются."""
        with self._c() as db:
            rows = [r[0] for r in db.execute(
                "SELECT n FROM drafts WHERE base=? AND n<>? AND status IN ('new','visual','visual_failed','ready')",
                (base, keep_n))]
            db.executemany("UPDATE drafts SET status='superseded', updated=? WHERE n=?",
                           [(now or time.time(), n) for n in rows])
        return rows


def _validate_album(attachments):
    if len(attachments) > ALBUM_MAX_IMAGES:
        raise ValueError("В одном альбоме можно обработать максимум 8 фотографий")
    total, total_encoded = 0, 0
    for attachment in attachments:
        encoded = attachment.get("data")
        if not isinstance(encoded, str) or len(encoded) > ((ALBUM_IMAGE_MAX_BYTES + 2) // 3) * 4:
            raise ValueError("Фотография слишком большая — максимум 10 МБ")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Повреждены данные фотографии") from None
        if not data or len(data) > ALBUM_IMAGE_MAX_BYTES:
            raise ValueError("Фотография слишком большая — максимум 10 МБ")
        total += len(data)
        total_encoded += len(encoded)
    if total > ALBUM_MAX_BYTES or total_encoded > ((ALBUM_MAX_BYTES + 2 * ALBUM_MAX_IMAGES) // 3) * 4:
        raise ValueError("Общий размер фотографий в альбоме — максимум 20 МБ")


def _log_row(row) -> dict:
    return {"id": row["id"], "msg_id": row["msg_id"], "kind": row["kind"],
            "payload": json.loads(row["payload"]), "result": json.loads(row["result"]), "ts": row["ts"]}


class _Closing:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.close()
