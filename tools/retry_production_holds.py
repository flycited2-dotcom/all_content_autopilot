"""Recheck current factory holds under production rules; never revive superseded/cancelled work."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from listener.pending import write_json
from photo_auditor import presentation, runtime, progress
from photo_auditor.images import digest, image_bytes
from photo_auditor.store import AuditStore
from tools.install_photo_audit import AGENT

RUN = ROOT / 'runs/2026-10-27/production-audit-policy'


def server_rows():
    code = '''import sqlite3,json
db=sqlite3.connect('file:/opt/content-factory/state/content_factory.db?mode=ro',uri=True);db.row_factory=sqlite3.Row
db.execute('ATTACH DATABASE ? AS q',('file:/root/ritualb2b/queue.db?mode=ro',))
rows=[dict(r) for r in db.execute("SELECT j.*,e.key AS item_key,e.status AS item_status,e.brand AS item_brand,e.model AS item_model FROM q.jobs j JOIN excel_items e ON (j.mode='research' AND e.research_job=j.id) OR (j.mode!='research' AND e.card_job=j.id) WHERE j.status='failed' AND e.status='failed' AND j.error_text LIKE 'PHOTO_AUDIT_HOLD:%' ORDER BY j.id")]
print(json.dumps(rows,ensure_ascii=False))'''
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'sprintbox-itp', 'python3 -'],
                       input=code.encode(), capture_output=True, timeout=45)
    if r.returncode:
        raise RuntimeError('Could not read current factory holds')
    return {r['id']: r for r in json.loads(r.stdout)}


def local_state(store, job):
    row = store.latest(f"fotogen-{job['id']}")
    if not row or row['status'] != 'review' or not row['report']:
        return None
    if any(row['product'][k] != job[k] for k in ('brand', 'model')):
        return None
    scope = runtime._read(ROOT, job['id'])
    if not scope['required'] or scope['mode'] != job['mode'] or any(scope[k] != job[k] for k in ('brand', 'model')):
        return None
    if digest(image_bytes((store.root / row['id'] / 'generated.bin').read_bytes())) != row['sha256']:
        raise RuntimeError('Snapshot changed')
    if job['mode'] == 'research' and not progress.restore_research(ROOT, row['product_id'], job['brand'], job['model']):
        return None
    return {k: row[k] for k in ('id', 'product_id', 'product', 'revision', 'status', 'sha256', 'report')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    remote = server_rows()
    store = AuditStore(ROOT / 'runs/photo-auditor')
    if not args.apply:
        targets = []
        for job in remote.values():
            try:
                local = local_state(store, job)
            except (ValueError, OSError, RuntimeError):
                continue
            if local:
                targets.append({'job': job, 'local': local})
        RUN.mkdir(parents=True, exist_ok=True)
        write_json(RUN / 'plan.json', {'targets': targets, 'rules': 'visible-product-3 / content-manager-2'})
        print(json.dumps({'apply': False, 'same_jobs_to_recheck': [t['job']['id'] for t in targets]}))
        return
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 8793)) == 0:
            raise RuntimeError('Stop the old auditor before applying the policy migration')
    targets = json.loads((RUN / 'plan.json').read_text(encoding='utf-8'))['targets']
    for target in targets:
        job = target['job']
        if remote.get(job['id']) != job or local_state(store, job) != target['local']:
            raise RuntimeError('A reviewed job or snapshot changed; preserve it')
    backup = RUN / 'backup'
    backup.mkdir()
    with sqlite3.connect(store.path) as current, sqlite3.connect(backup / 'audit.sqlite3') as saved:
        current.backup(saved)
    spec = importlib.util.spec_from_file_location('_production_photo_config', AGENT / 'config.py')
    config = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = config
    spec.loader.exec_module(config)
    changed = []
    for target in targets:
        job, row = target['job'], target['local']
        if job['mode'] != 'research':
            runtime.register_job(ROOT, job['id'], job['mode'], job['brand'], job['model'], job['specs'])
            cfg = config.get_mode(job['mode'])
            path = presentation._path(ROOT, job['id'])
            if path.exists() and job['mode'] == 'kbt':
                (backup / path.name).write_bytes(path.read_bytes())
                path.unlink()
            if job['mode'] == 'kbt' or not path.exists():
                presentation.capture(ROOT, job['id'], cfg.render_prompt(job['specs'], brand=job['brand'], model=job['model']), cfg.reference_files)
        if not store.recheck(row['id']):
            raise RuntimeError('Could not recheck the same card')
        changed.append(job['id'])
    # The terminated process ran read-only inspections, never image generation.
    # Release only its exact unchanged image snapshots; no late worker can remain after shutdown.
    released = []
    with store.connect() as db:
        for row in db.execute("SELECT id,sha256 FROM audits WHERE status='checking'").fetchall():
            if digest(image_bytes((store.root / row['id'] / 'generated.bin').read_bytes())) != row['sha256']:
                raise RuntimeError('An in-flight audit snapshot changed')
            db.execute("UPDATE audits SET status='queued',lease=NULL,tries=max(0,tries-1),retry_at=0 WHERE id=? AND status='checking' AND sha256=?", (row['id'], row['sha256']))
            released.append(row['id'])
    write_json(RUN / 'applied.json', {'same_jobs_rechecked': changed, 'read_only_inspections_released': released,
        'new_jobs_created': False, 'publication': False})
    print(json.dumps({'same_jobs_rechecked': changed, 'read_only_inspections_released': released, 'new_jobs_created': False}))


if __name__ == '__main__':
    main()
