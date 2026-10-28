"""Durable queue with immutable snapshots, leases and bounded revisions."""
import json
import math
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from listener.pending import write_json
from photo_auditor.images import digest, image_bytes

MAX_REGENERATIONS = 2
PROVIDER_ERROR_KINDS = frozenset({'usage_limit', 'network', 'rate_limit', 'auth',
                                  'configuration', 'invalid_response'})
PROVIDER_ERROR_CODES = frozenset({
    'subscription_sharing_usage_limit_exceeded', 'subscription_sharing_usage_unavailable',
    'subscription_sharing_user_unavailable', 'subscription_sharing_user_not_eligible',
    'subscription_sharing_unsupported_capability', 'subscription_sharing_route_not_supported',
    'subscription_sharing_invalid_user', 'chatpass_v2_scope_not_authorized',
    'chatpass_v2_invalid_authorization_context', 'insufficient_quota', 'quota_exceeded',
    'rate_limit_exceeded', 'invalid_api_key', 'invalid_authentication', 'model_not_found',
})


def sanitize_provider_error(value):
    """Persist only known categories; provider responses and messages never enter the queue."""
    if (not isinstance(value, dict) or value.get('provider') != 'chatgpt' or
            not isinstance(value.get('kind'), str) or value['kind'] not in PROVIDER_ERROR_KINDS):
        return None
    code = value.get('code')
    return {'provider': 'chatgpt', 'kind': value['kind'],
            'code': code if isinstance(code, str) and code in PROVIDER_ERROR_CODES else None}


class AuditStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "audit.sqlite3"
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""CREATE TABLE IF NOT EXISTS audits (
                id TEXT PRIMARY KEY, product_id TEXT, revision INTEGER, product TEXT, sha256 TEXT,
                status TEXT, report TEXT, created REAL, lease REAL, tries INTEGER DEFAULT 0,
                retry_at REAL DEFAULT 0, notified INTEGER DEFAULT 0, provider_error TEXT,
                UNIQUE(product_id, revision))""")
            if 'provider_error' not in {row[1] for row in db.execute('PRAGMA table_info(audits)')}:
                db.execute('ALTER TABLE audits ADD COLUMN provider_error TEXT')
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)")
            db.execute('CREATE TABLE IF NOT EXISTS human_feedback ('
                       'audit_id TEXT PRIMARY KEY, verdict TEXT NOT NULL, created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS audit_history ('
                       'audit_id TEXT,attempt INTEGER,report TEXT,folder TEXT,created REAL,'
                       'PRIMARY KEY(audit_id,attempt))')
            db.execute('CREATE TABLE IF NOT EXISTS recovery_receipts ('
                       'audit_id TEXT,attempt INTEGER,result TEXT,PRIMARY KEY(audit_id,attempt))')

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
        value = dict(row)
        try:
            provider_error = sanitize_provider_error(json.loads(value['provider_error'])) if value.get('provider_error') else None
        except (ValueError, TypeError):
            provider_error = None
        return {**value, "product": json.loads(row["product"]),
                "report": json.loads(row["report"]) if row["report"] else None,
                'provider_error': provider_error}

    def get(self, audit_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM audits WHERE id=?", (audit_id,)).fetchone()
        return self._row(row) if row else None

    def latest(self, product_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM audits WHERE product_id=? ORDER BY revision DESC LIMIT 1',
                             (product_id,)).fetchone()
        return self._row(row) if row else None

    def recheck(self, audit_id, *, content_only=False):
        """Retry the same snapshot; content-only retry requires intact signed visual evidence."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM audits WHERE id=?', (audit_id,)).fetchone()
            if not row or row['status'] not in ('review', 'approved'):
                return False
            if content_only and row['status'] != 'review':
                return False
            if db.execute('SELECT 1 FROM audits WHERE product_id=? AND revision>?',
                          (row['product_id'], row['revision'])).fetchone():
                return False
            folder = self.root / audit_id
            if digest((folder / 'generated.bin').read_bytes()) != row['sha256']:
                raise ValueError('Сохранённый снимок изменился')
            if content_only:
                from photo_auditor import audit_checkpoint
                from photo_auditor.integration import AuditHold
                from photo_auditor.runtime import content_brief
                root = self.root.parent.parent
                try:
                    product = json.loads(row['product'])
                    brief = content_brief(root, product)
                    if brief is None or audit_checkpoint.load(root, product, folder, brief) is None:
                        return False
                except (AuditHold, OSError, ValueError, TypeError, KeyError):
                    return False
            attempt = db.execute('SELECT count(*) FROM audit_history WHERE audit_id=?', (audit_id,)).fetchone()[0]
            archive = folder / 'history' / uuid.uuid4().hex
            archive.mkdir(parents=True)
            for file in folder.iterdir():
                if file.is_file() and (file.name.startswith('source-') or file.name in ('report.json', 'comparison.jpg', 'visual-checkpoint.json', 'content-validation.json')):
                    shutil.copy2(file, archive / file.name)
            # Full recheck seeks fresh evidence; a content-only retry retains its signed visual result.
            if not content_only:
                (folder / 'visual-checkpoint.json').unlink(missing_ok=True)
            db.execute('INSERT INTO audit_history VALUES(?,?,?,?,?)',
                       (audit_id, attempt, row['report'], str(archive), time.time()))
            db.execute("UPDATE audits SET status='queued',report=NULL,tries=0,retry_at=0,notified=0,lease=NULL,provider_error=NULL WHERE id=?",
                       (audit_id,))
        return True

    def retry_waiting(self, audit_id):
        """Retry only the latest saved provider wait, without creating a revision or image."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM audits WHERE id=?', (audit_id,)).fetchone()
            if not row or row['status'] != 'waiting_provider':
                return False
            if db.execute('SELECT 1 FROM audits WHERE product_id=? AND revision>?',
                          (row['product_id'], row['revision'])).fetchone():
                return False
            source = self.root / audit_id / 'generated.bin'
            if digest(image_bytes(source.read_bytes())) != row['sha256']:
                raise ValueError('Сохранённый снимок изменился')
            db.execute("UPDATE audits SET status='queued',retry_at=0,notified=0,lease=NULL,provider_error=NULL WHERE id=?",
                       (audit_id,))
        return True

    def request_reference_repair(self, audit_id):
        """Explicit source-only recovery; an inconclusive card cannot buy a redraw."""
        from photo_auditor.reference_asset import from_audit
        from photo_auditor.runtime import _read
        row = self.get(audit_id)
        if not row or row['status'] != 'review' or row['revision'] >= MAX_REGENERATIONS:
            return False
        match = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
        if not match:
            return False
        root = self.root.parent.parent
        scope = _read(root, int(match[1]))
        from photo_auditor.progress import research_specs
        research = scope.get('mode') == 'research' and bool(research_specs(root, row['product_id'], row['product']['brand'], row['product']['model']))
        if (not research or
                not scope.get('required') or
                (scope.get('brand'), scope.get('model')) != (row['product']['brand'], row['product']['model']) or
                from_audit(root, row) is None):
            return False
        report = {**row['report'], 'verdict': 'regenerate', 'repair_mode': 'reference_asset',
                  'reason': 'Нужно заменить исходник на визуально проверенное фото основного товара. '
                            'Новая генерация не разрешена; скачанный снимок проверяется заново.'}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE audits SET status='regenerate',report=?,notified=0 WHERE id=? AND status='review' AND report=?",
                (json.dumps(report, ensure_ascii=False), audit_id, json.dumps(row['report'], ensure_ascii=False))).rowcount
            if changed:
                archive = self.root / audit_id / 'before-reference-repair.json'
                write_json(archive, row['report'])
                write_json(self.root / audit_id / 'report.json', report)
        return bool(changed)

    def claim(self, now=None, audit_id=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE audits SET status='queued' WHERE status='checking' AND lease<?", (now - 900,))
            query = "SELECT * FROM audits WHERE status IN ('queued','waiting_provider') AND retry_at<=?"
            params = [now]
            if audit_id is not None:
                query += " AND id=?"
                params.append(audit_id)
            # Live agents poll for an output they are holding; do not put them
            # behind a large historical recheck campaign.
            row = db.execute(query + " ORDER BY CASE WHEN EXISTS(SELECT 1 FROM meta m WHERE "
                             "m.key='consumer:'||audits.id AND CAST(m.value AS REAL)>=?) THEN 0 ELSE 1 END,created LIMIT 1",
                             [*params, now - 90]).fetchone()
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

    def consumer_waiting(self, audit_id, now=None):
        with self.connect() as db:
            if db.execute('SELECT 1 FROM audits WHERE id=?', (audit_id,)).fetchone():
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                           ('consumer:' + audit_id, str(time.time() if now is None else now)))

    def renew_lease(self, audit, now=None):
        with self.connect() as db:
            return bool(db.execute("UPDATE audits SET lease=? WHERE id=? AND status='checking' AND "
                "EXISTS(SELECT 1 FROM meta WHERE key=? AND value=?)",
                (time.time() if now is None else now, audit['id'], 'lease:' + audit['id'], audit['lease_token'])).rowcount)

    def _consistent_report(self, audit, report):
        if report.get('owner_approval'):
            from photo_auditor.owner_decisions import valid
            if valid(self.root.parent.parent,audit['product'],report):
                return report
            return {**report,'verdict':'review','reason':'Подтверждение владельца не соответствует файлу или заданию.'}
        from photo_auditor.consistency import preserve_defects
        # Same image, exact nomenclature, same official reference: keep concrete defects.
        with self.connect() as db:
            previous = [r[0] for r in db.execute('SELECT report FROM audits WHERE sha256=? AND report IS NOT NULL',
                                                (audit['sha256'],))]
            previous += [r[0] for r in db.execute('SELECT h.report FROM audit_history h JOIN audits a ON a.id=h.audit_id '
                                                 'WHERE a.sha256=? AND h.report IS NOT NULL', (audit['sha256'],))]
        same_identity = []
        for value in previous:
            old = json.loads(value)
            if all(old.get('product', {}).get(k) == audit['product'][k] for k in ('brand', 'model')):
                same_identity.append(old)
        return preserve_defects(report, same_identity)

    def reconcile_conflicts(self, audit_id):
        """Apply an already recorded official-image defect; no new model judgement."""
        row = self.get(audit_id)
        if not row or row['status'] != 'approved':
            return False
        report = self._consistent_report(row, row['report'])
        if report['verdict'] == 'approved':
            return False
        if row['revision'] >= MAX_REGENERATIONS:
            report = {**report, 'verdict': 'review', 'reason': 'Лимит исправлений исчерпан. ' + report['reason']}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT status,report FROM audits WHERE id=?', (audit_id,)).fetchone()
            if current['status'] != 'approved' or current['report'] != json.dumps(row['report'], ensure_ascii=False):
                return False
            attempt = db.execute('SELECT count(*) FROM audit_history WHERE audit_id=?', (audit_id,)).fetchone()[0]
            archive = self.root / audit_id / 'history' / uuid.uuid4().hex
            archive.mkdir(parents=True)
            shutil.copy2(self.root / audit_id / 'report.json', archive / 'report.json')
            db.execute('INSERT INTO audit_history VALUES(?,?,?,?,?)',
                       (audit_id, attempt, current['report'], str(archive), time.time()))
            write_json(self.root / audit_id / 'report.json', report)
            db.execute('UPDATE audits SET status=?,report=?,notified=0 WHERE id=?',
                       (report['verdict'], json.dumps(report, ensure_ascii=False), audit_id))
        return True

    def finish(self, audit, report):
        report = self._consistent_report(audit, report)
        verdict = report["verdict"]
        if verdict not in ("approved", "regenerate", "review"):
            raise ValueError("Неверный вердикт")
        if verdict == "regenerate" and audit["revision"] >= MAX_REGENERATIONS:
            report = {**report, "verdict": "review", "reason": "Лимит повторов исчерпан. " + report["reason"]}
        report = {**report, "generated_sha256": audit["sha256"], "product": audit["product"]}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT value FROM meta WHERE key=?", ("lease:" + audit["id"],)).fetchone()
            current = db.execute("SELECT status FROM audits WHERE id=?", (audit['id'],)).fetchone()
            if not lease or lease[0] != audit["lease_token"] or not current or current[0] != 'checking':
                return False
            write_json(self.root / audit["id"] / "report.json", report)
            db.execute("UPDATE audits SET status=?,report=?,notified=0,provider_error=NULL WHERE id=? AND status='checking'",
                       (report["verdict"], json.dumps(report, ensure_ascii=False), audit["id"]))
        return True

    def defer(self, audit, reason, now=None, retry_at=None, provider_wait=False, provider_error=None):
        now = time.time() if now is None else now
        if audit["tries"] >= 3 and not provider_wait:
            return self.finish(audit, {"verdict": "review", "reason": reason,
                "checks": [], "sources": [], "dimensions_note": "Проверка не завершена."})
        diagnostic = sanitize_provider_error(provider_error) if provider_wait else None
        valid_retry = type(retry_at) in (int, float) and math.isfinite(retry_at)
        retry_at = max(now + 60, retry_at if valid_retry else now + 300)
        with self.connect() as db:
            db.execute("UPDATE audits SET status=?,retry_at=?,provider_error=? WHERE id=? AND status='checking' AND "
                       "EXISTS(SELECT 1 FROM meta WHERE key=? AND value=?)",
                       ('waiting_provider' if provider_wait else 'queued', retry_at,
                        json.dumps(diagnostic) if diagnostic else None,
                        audit["id"], "lease:" + audit["id"], audit["lease_token"]))

    def waiting_notifications(self):
        with self.connect() as db:
            return [self._row(row) for row in db.execute(
                "SELECT * FROM audits WHERE status='waiting_provider' AND notified=0 LIMIT 10")]

    def waiting(self, limit=10):
        """Current provider waits for status output, including already notified snapshots."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('Неверный предел ожидающих проверок')
        with self.connect() as db:
            rows = db.execute("SELECT a.* FROM audits a WHERE a.status='waiting_provider' "
                'AND NOT EXISTS(SELECT 1 FROM audits newer WHERE newer.product_id=a.product_id '
                'AND newer.revision>a.revision) ORDER BY a.created LIMIT ?', (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def recovery_results(self):
        with self.connect() as db:
            rows = db.execute('SELECT a.*,(SELECT count(*) FROM audit_history h WHERE h.audit_id=a.id) AS attempt '
                "FROM audits a WHERE a.status IN ('approved','regenerate') "
                'AND NOT EXISTS(SELECT 1 FROM audits newer WHERE newer.product_id=a.product_id AND newer.revision>a.revision) '
                'AND NOT EXISTS(SELECT 1 FROM recovery_receipts r WHERE r.audit_id=a.id AND r.attempt='
                '(SELECT count(*) FROM audit_history h WHERE h.audit_id=a.id))').fetchall()
        return [self._row(row) for row in rows]

    def recovery_delivered(self, audit_id, attempt, result):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO recovery_receipts VALUES(?,?,?)',
                       (audit_id, attempt, json.dumps(result, ensure_ascii=False)))

    def status(self):
        with self.connect() as db:
            return {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM audits GROUP BY status")}

    def pending_notifications(self):
        with self.connect() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM audits WHERE report IS NOT NULL AND notified=0 ORDER BY created LIMIT 10")]

    def notified(self, audit_id):
        with self.connect() as db:
            db.execute("UPDATE audits SET notified=1 WHERE id=?", (audit_id,))

    def feedback(self, audit_id, verdict):
        if verdict not in ('match', 'mismatch', 'unclear'):
            raise ValueError('Неверная оценка')
        with self.connect() as db:
            row = db.execute('SELECT report FROM audits WHERE id=?', (audit_id,)).fetchone()
            if not row or row['report'] is None:
                return False
            db.execute('INSERT INTO human_feedback VALUES(?,?,?) ON CONFLICT(audit_id) '
                       'DO UPDATE SET verdict=excluded.verdict,created=excluded.created',
                       (audit_id, verdict, time.time()))
        return True

    def owner_approve(self, audit_id, actor_id, callback_id, *, publish=False):
        """Owner acceptance is separate from the preserved automatic judgement."""
        from photo_auditor import runtime, owner_decisions
        root=self.root.parent.parent
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            raw=db.execute('SELECT * FROM audits WHERE id=?',(audit_id,)).fetchone()
            if not raw:
                raise ValueError('Проверка не найдена')
            row=self._row(raw)
            if db.execute('SELECT 1 FROM audits WHERE product_id=? AND revision>?',
                          (row['product_id'],row['revision'])).fetchone():
                raise ValueError('Это старая фотография. Откройте последнюю ревизию в аудиторе.')
            if row['status'] not in ('review','regenerate','approved') or not row['report']:
                raise ValueError('Эта фотография ещё проверяется. Дождитесь отчёта.')
            folder=self.root/audit_id
            if digest(image_bytes((folder/'generated.bin').read_bytes()))!=row['sha256']:
                raise ValueError('Фотография изменилась; подтверждение остановлено.')
            brief=runtime.content_brief(root,row['product'])
            if type(publish) is not bool or publish and brief is None:
                raise ValueError('Публиковать можно готовую карточку, а не исходное фото этапа поиска.')
            already_requested=bool(row['report'].get('owner_approval',{}).get('payload',{}).get('publish_requested'))
            if row['status']=='approved' and runtime.approval_covers_brief(root,row['product'],row['report']) and (not publish or already_requested):
                if publish:
                    previous=db.execute('SELECT value FROM meta WHERE key=?',('owner-publish:'+audit_id,)).fetchone()
                    prior=json.loads(previous[0]) if previous else {}
                    if prior.get('status')!='pending' and not prior.get('result',{}).get('published'):
                        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                                   ('owner-publish:'+audit_id,json.dumps({'audit_id':audit_id,'status':'pending'})))
                return {'approved':True,'already_approved':True,'product':row['product']}
            signed=owner_decisions.sign(root,row,actor_id,callback_id,publish=publish)
            report={**row['report'],'verdict':'approved','generated_sha256':row['sha256'],
                    'product':row['product'],'owner_approval':signed,
                    'reason':'Владелец подтвердил сохранённый результат. Автоматическое заключение сохранено в истории.'}
            if brief is not None:
                report['content_review']={'verdict':'approved','policy_version':runtime.CONTENT_POLICY,
                    'brief_sha256':runtime.brief_digest(brief),'reviewer':'owner',
                    'reason':'Карточка принята владельцем.',
                    'automatic_review':row['report'].get('content_review')}
            attempt=db.execute('SELECT count(*) FROM audit_history WHERE audit_id=?',(audit_id,)).fetchone()[0]
            archive=folder/'history'/uuid.uuid4().hex;archive.mkdir(parents=True)
            write_json(archive/'report.json',row['report'])
            db.execute('INSERT INTO audit_history VALUES(?,?,?,?,?)',
                       (audit_id,attempt,raw['report'],str(archive),time.time()))
            write_json(folder/'report.json',report)
            db.execute("UPDATE audits SET status='approved',report=?,lease=NULL,retry_at=0,provider_error=NULL,notified=1 WHERE id=?",
                       (json.dumps(report,ensure_ascii=False),audit_id))
            if publish:
                db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',
                           ('owner-publish:'+audit_id,json.dumps({'audit_id':audit_id,'status':'pending'})))
        return {'approved':True,'already_approved':False,'product':row['product']}

    def pending_publications(self):
        with self.connect() as db:
            rows=db.execute("SELECT value FROM meta WHERE key LIKE 'owner-publish:%'").fetchall()
        return [value for row in rows if (value:=json.loads(row[0])).get('status')=='pending']

    def publication_finished(self, audit_id, result):
        with self.connect() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?',
                       (json.dumps({'audit_id':audit_id,'status':'done','result':result},ensure_ascii=False),'owner-publish:'+audit_id))

    def offset(self, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES('telegram_offset',?)", (str(value),))
            row = db.execute("SELECT value FROM meta WHERE key='telegram_offset'").fetchone()
        return int(row[0]) if row else None
