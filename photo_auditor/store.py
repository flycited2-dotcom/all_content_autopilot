"""Durable queue with immutable snapshots, leases and bounded revisions."""
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from listener.pending import write_json
from photo_auditor.images import digest, image_bytes

MAX_REGENERATIONS = 2


class AuditStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "audit.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS audits (
                id TEXT PRIMARY KEY, product_id TEXT, revision INTEGER, product TEXT, sha256 TEXT,
                status TEXT, report TEXT, created REAL, lease REAL, tries INTEGER DEFAULT 0,
                retry_at REAL DEFAULT 0, notified INTEGER DEFAULT 0,
                UNIQUE(product_id, revision))""")
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, product, generated, supplier=None):
        if not isinstance(product, dict) or set(product) != {"product_id", "revision", "brand", "model"}:
            raise ValueError("Нужны product_id, revision, brand, model")
        if not isinstance(product["product_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", product["product_id"]):
            raise ValueError("Неверный id товара")
        if type(product["revision"]) is not int or not 0 <= product["revision"] <= MAX_REGENERATIONS:
            raise ValueError("Исчерпан лимит повторных генераций")
        if any(not isinstance(product[k], str) or not 0 < len(product[k].strip()) <= 180 for k in ("brand", "model")):
            raise ValueError("Нужны точные бренд и модель")
        image_bytes(generated)
        if supplier is not None:
            image_bytes(supplier)
        fingerprint = digest(generated)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM audits WHERE product_id=? AND revision=?",
                             (product["product_id"], product["revision"])).fetchone()
            if old:
                if old["sha256"] != fingerprint or json.loads(old["product"]) != product:
                    raise ValueError("Эта ревизия уже связана с другим снимком/товаром")
                return self._row(old)
            if product["revision"]:
                previous = db.execute("SELECT * FROM audits WHERE product_id=? AND revision=?",
                                      (product["product_id"], product["revision"] - 1)).fetchone()
                if not previous or previous["status"] != "regenerate":
                    raise ValueError("Повторная генерация не запрошена контролёром")
                prior = json.loads(previous["product"])
                if any(prior[k] != product[k] for k in ("brand", "model")):
                    raise ValueError("Подмена номенклатуры при повторе")
            audit_id = uuid.uuid4().hex
            folder = self.root / audit_id
            folder.mkdir()
            (folder / "generated.bin").write_bytes(generated)
            if supplier is not None:
                (folder / "supplier.bin").write_bytes(supplier)
            write_json(folder / "request.json", {"product": product, "sha256": fingerprint})
            db.execute("INSERT INTO audits(id,product_id,revision,product,sha256,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       (audit_id, product["product_id"], product["revision"], json.dumps(product, ensure_ascii=False), fingerprint, time.time()))
        return self.get(audit_id)

    @staticmethod
    def _row(row):
        return {**dict(row), "product": json.loads(row["product"]), "report": json.loads(row["report"]) if row["report"] else None}

    def get(self, audit_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM audits WHERE id=?", (audit_id,)).fetchone()
        return self._row(row) if row else None

    def claim(self, now=None, audit_id=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE audits SET status='queued' WHERE status='checking' AND lease<?", (now - 900,))
            query = "SELECT * FROM audits WHERE status='queued' AND retry_at<=?"
            params = [now]
            if audit_id is not None:
                query += " AND id=?"
                params.append(audit_id)
            row = db.execute(query + " ORDER BY created LIMIT 1", params).fetchone()
            if row:
                lease = uuid.uuid4().hex
                # generation token prevents a late worker overwriting a recovered lease
                db.execute("UPDATE audits SET status='checking',lease=?,tries=tries+1 WHERE id=?", (now, row["id"]))
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("lease:" + row["id"], lease))
        if not row:
            return None
        result = self.get(row["id"])
        result["lease_token"] = lease
        return result

    def finish(self, audit, report):
        verdict = report["verdict"]
        if verdict not in ("approved", "regenerate", "review"):
            raise ValueError("Неверный вердикт")
        if verdict == "regenerate" and audit["revision"] >= MAX_REGENERATIONS:
            report = {**report, "verdict": "review", "reason": "Лимит повторов исчерпан. " + report["reason"]}
        report = {**report, "generated_sha256": audit["sha256"], "product": audit["product"]}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT value FROM meta WHERE key=?", ("lease:" + audit["id"],)).fetchone()
            if not lease or lease[0] != audit["lease_token"]:
                return False
            write_json(self.root / audit["id"] / "report.json", report)
            db.execute("UPDATE audits SET status=?,report=? WHERE id=? AND status='checking'",
                       (report["verdict"], json.dumps(report, ensure_ascii=False), audit["id"]))
        return True

    def defer(self, audit, reason, now=None, retry_at=None):
        now = time.time() if now is None else now
        if audit["tries"] >= 3:
            return self.finish(audit, {"verdict": "review", "reason": reason,
                "checks": [], "sources": [], "dimensions_note": "Проверка не завершена."})
        with self.connect() as db:
            db.execute("UPDATE audits SET status='queued',retry_at=? WHERE id=? AND status='checking' AND "
                       "EXISTS(SELECT 1 FROM meta WHERE key=? AND value=?)",
                       (max(now + 60, retry_at or now + 300), audit["id"], "lease:" + audit["id"], audit["lease_token"]))

    def status(self):
        with self.connect() as db:
            return {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM audits GROUP BY status")}

    def pending_notifications(self):
        with self.connect() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM audits WHERE report IS NOT NULL AND notified=0 ORDER BY created LIMIT 10")]

    def notified(self, audit_id):
        with self.connect() as db:
            db.execute("UPDATE audits SET notified=1 WHERE id=?", (audit_id,))

    def offset(self, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES('telegram_offset',?)", (str(value),))
            row = db.execute("SELECT value FROM meta WHERE key='telegram_offset'").fetchone()
        return int(row[0]) if row else None
