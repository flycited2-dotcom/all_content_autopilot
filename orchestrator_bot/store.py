"""SQLite-хранилище оркестратора: очередь сообщений, журнал действий, вопросы, мета."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

CLAIM_TIMEOUT = 600          # слушатель забрал и пропал — через 10 минут сообщение снова в очереди

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, tg_message_id INTEGER, kind TEXT, text TEXT,
    status TEXT NOT NULL DEFAULT 'new', created REAL, claimed_at REAL,
    intent_done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY, msg_id INTEGER, kind TEXT, payload TEXT, result TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS asks (
    id INTEGER PRIMARY KEY, msg_id INTEGER, action TEXT, options TEXT, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as db:
            db.executescript(_SCHEMA)

    def _c(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return _Closing(db)

    # ── очередь ──
    def add_message(self, tg_message_id, kind, text, now=None) -> int:
        with self._c() as db:
            return db.execute("INSERT INTO messages(tg_message_id, kind, text, created) VALUES(?,?,?,?)",
                              (tg_message_id, kind, text, now or time.time())).lastrowid

    def set_transcript(self, msg_id, text):
        with self._c() as db:
            db.execute("UPDATE messages SET text=? WHERE id=?", (text, msg_id))

    def claim_next(self, now=None) -> dict | None:
        now = now or time.time()
        with self._c() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE messages SET status='new' WHERE status='claimed' AND claimed_at < ?",
                       (now - CLAIM_TIMEOUT,))
            row = db.execute("SELECT * FROM messages WHERE status='new' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE messages SET status='claimed', claimed_at=? WHERE id=?", (now, row["id"]))
            db.execute("COMMIT")
        return {k: row[k] for k in ("id", "tg_message_id", "kind", "text", "created")} if row else None

    def mark_done(self, msg_id):
        with self._c() as db:
            db.execute("UPDATE messages SET status='done' WHERE id=?", (msg_id,))

    def count_new(self) -> int:
        with self._c() as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE status IN ('new','claimed')").fetchone()[0]

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

    # ── вопросы с кнопками ──
    def save_ask(self, msg_id, action: dict, options: list[str]) -> int:
        with self._c() as db:
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
