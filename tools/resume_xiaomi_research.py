"""Review, then resume only the two Xiaomi source-download jobs stopped by V7."""
import argparse
import base64
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from listener.pending import write_json
from photo_auditor import runtime
from tools.install_photo_audit import AGENT

RUN = ROOT / 'runs/2026-10-27/research-v8'
HOST = 'sprintbox-itp'
FACTORY_DB = '/opt/content-factory/state/content_factory.db'
QUEUE_DB = '/root/ritualb2b/queue.db'
DRIVER_SHA = '925f078082d25f146b616aacd413632cd67e53a5841a105dcfe1124d2c170974'
BRIDGE_SHA = '6f5d83380f505c40e4f6d3edf55eaba842821463aa952edb1021cd317b5bdc48'
HOLD = 'PHOTO_AUDIT_HOLD: Не подтверждён результат начатой генерации; требуется сверить сохранённый файл'
TARGETS = {
    3811: {'model': 'L55MC-SRU',
           'item_key': 'excel|xiaomi|tv s mini led 55 2026 l55mc-sru 4k smarttv android',
           'item_model': 'TV S Mini LED 55 2026 L55MC-SRU 4K SmartTV Android'},
    3812: {'model': 'L43MB-APRU',
           'item_key': 'excel|xiaomi|mi tv a pro 2026 l43mb-apru qled 4k smarttv android',
           'item_model': 'MI TV A PRO 2026 L43MB-APRU QLED 4K SmartTV Android'},
}


def snapshot(db):
    """Full rows bind the review to all current job and item fields."""
    ids = tuple(TARGETS)
    jobs = [dict(row) for row in db.execute('SELECT * FROM photo_queue.jobs WHERE id IN (?,?) ORDER BY id', ids)]
    items = [dict(row) for row in db.execute('SELECT * FROM excel_items WHERE research_job IN (?,?) '
                                           'OR card_job IN (?,?) ORDER BY research_job,key', ids + ids)]
    return {'jobs': jobs, 'items': items}


def validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != {'jobs', 'items'}:
        raise RuntimeError('Invalid recovery snapshot')
    if len(value['jobs']) != 2 or len(value['items']) != 2:
        raise RuntimeError('Both exact failed jobs and their sole factory items are required')
    jobs = {row['id']: row for row in value['jobs']}
    items = {row['research_job']: row for row in value['items']}
    if set(jobs) != set(TARGETS) or set(items) != set(TARGETS):
        raise RuntimeError('Research jobs were replaced or reassigned')
    for job_id, identity in TARGETS.items():
        job, item = jobs[job_id], items[job_id]
        if ((job['mode'], job['brand'], job['model'], job['status'], job['error_text']) !=
                ('research', 'Xiaomi', identity['model'], 'failed', HOLD)):
            raise RuntimeError(f'Job {job_id} no longer matches the reviewed download failure')
        if ((item['key'], item['brand'], item['model'], item['status'], item['card_job'], item['error']) !=
                (identity['item_key'], 'Xiaomi', identity['item_model'], 'failed', None, 'research: ' + HOLD)):
            raise RuntimeError(f'Factory item for {job_id} was superseded or changed')


def require_unchanged(expected, current):
    validate_snapshot(expected)
    validate_snapshot(current)
    if current != expected:
        raise RuntimeError('Server rows changed after review; no jobs were resumed')


REMOTE = r'''
from datetime import datetime, timezone
import os
factory = Path(FACTORY_DB)
queue = Path(QUEUE_DB)
for source in (factory, queue):
    if not source.is_file() or source.is_symlink():
        raise RuntimeError('Expected ordinary existing databases')
mode = 'rw' if payload['apply'] else 'ro'
db = sqlite3.connect(factory.as_uri() + '?mode=' + mode, uri=True, timeout=30)
db.row_factory = sqlite3.Row
try:
    db.execute('ATTACH DATABASE ? AS photo_queue', (queue.as_uri() + '?mode=' + mode,))
    if not payload['apply']:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        current = snapshot(db)
        validate_snapshot(current)
        print(json.dumps(current, ensure_ascii=False))
    else:
        db.execute('BEGIN IMMEDIATE')
        current = snapshot(db)
        require_unchanged(payload['snapshot'], current)
        # Only after matching the reviewed rows do we create any remote artifact.
        backup_root = Path('/opt/agents-my/backups')
        if backup_root.resolve() != backup_root:
            raise RuntimeError('Unexpected backup directory')
        backup = backup_root / ('research-resume-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir(mode=0o700)
        for source, name in ((factory, 'content_factory.db'), (queue, 'queue.db')):
            # A separate reader can back up while this transaction excludes writers.
            with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30) as reader:
                with sqlite3.connect(backup / name) as saved:
                    reader.backup(saved)
                    if saved.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Database backup failed integrity verification')
            os.chmod(backup / name, 0o600)
        (backup / 'resume-plan.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(backup / 'resume-plan.json', 0o600)
        now = datetime.now().isoformat()
        for job_id, identity in TARGETS.items():
            changed = db.execute("UPDATE photo_queue.jobs SET status='pending',claimed_by=NULL,claimed_at=NULL,"
                "error_text=NULL,updated_at=? WHERE id=? AND status='failed' AND mode='research' "
                'AND brand=? AND model=? AND error_text=?',
                (now, job_id, 'Xiaomi', identity['model'], HOLD)).rowcount
            if changed != 1:
                raise RuntimeError('Job changed during recovery')
            changed = db.execute("UPDATE excel_items SET status='research',error=NULL WHERE key=? "
                "AND research_job=? AND card_job IS NULL AND status='failed' AND brand=? AND model=? AND error=?",
                (identity['item_key'], job_id, 'Xiaomi', identity['item_model'], 'research: ' + HOLD)).rowcount
            if changed != 1:
                raise RuntimeError('Factory item changed during recovery')
        after = snapshot(db)
        db.commit()
        result = {'resumed': list(TARGETS), 'new_jobs_created': False, 'publications_created': False,
                  'backup': str(backup), 'after': after}
        (backup / 'resume-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(backup / 'resume-result.json', 0o600)
        print(json.dumps(result, ensure_ascii=False))
finally:
    db.close()
'''


def remote_script(payload):
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('ascii')
    constants = (f'FACTORY_DB={FACTORY_DB!r}\nQUEUE_DB={QUEUE_DB!r}\nTARGETS={TARGETS!r}\nHOLD={HOLD!r}\n')
    functions = '\n\n'.join(inspect.getsource(fn) for fn in (snapshot, validate_snapshot, require_unchanged))
    return ('import base64,json,sqlite3\nfrom pathlib import Path\n' + constants + functions +
            '\npayload=json.loads(base64.b64decode(' + repr(encoded) + '))\n' + REMOTE)


def ssh(payload):
    process = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', HOST, 'python3 -'],
                             input=remote_script(payload).encode('utf-8'), capture_output=True, timeout=120)
    if process.returncode:
        # Server output contains only our bounded diagnostic; no environment is read.
        raise RuntimeError('Research recovery stopped: ' + process.stderr.decode('utf-8', errors='replace')[-800:])
    return json.loads(process.stdout.decode('utf-8'))


def local_readiness():
    """Do not release jobs into an old bridge or an unknown paid operation."""
    installed_path = RUN / 'installed.json'
    installed_data = installed_path.read_bytes()
    installed = json.loads(installed_data)
    target = AGENT / 'remote_agent.py'
    driver = AGENT / 'agent.py'
    sha = lambda data: hashlib.sha256(data).hexdigest()
    if (installed.get('installed') is not True or Path(installed['target']) != target or
            target.is_symlink() or driver.is_symlink() or
            installed['after_sha256'] != BRIDGE_SHA or
            sha(target.read_bytes()) != installed['after_sha256'] or
            sha((RUN / 'remote_agent.py').read_bytes()) != installed['after_sha256'] or
            installed['driver_sha256'] != DRIVER_SHA or sha(driver.read_bytes()) != DRIVER_SHA or
            'PHOTO_AUDIT_BRIDGE_V8:' not in target.read_text(encoding='utf-8-sig')):
        raise RuntimeError('Reviewed V8 bridge is not installed')
    states = {}
    database = ROOT / 'runs/photo-auditor/audit.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        for job_id, identity in TARGETS.items():
            scope = runtime._read(ROOT, job_id)
            expected_scope = {'job_id': job_id, 'mode': 'research', 'brand': 'Xiaomi',
                              'model': identity['model'], 'required': True, 'editorial': False}
            if scope != expected_scope:
                raise RuntimeError('Signed research scope changed')
            product_id = f'fotogen-{job_id}'
            folder = ROOT / 'runs/photo-auditor/recovery' / sha(product_id.encode())[:32]
            path = folder / 'state.json'
            data = path.read_bytes()
            expected = {'product_id': product_id, 'brand': 'Xiaomi', 'model': identity['model'],
                        'revision': 0, 'phase': 'researching', 'operation': 'verified_source_download'}
            if (path.is_symlink() or json.loads(data) != expected or
                    {file.name for file in folder.iterdir()} != {'state.json'} or
                    db.execute('SELECT 1 FROM audits WHERE product_id=?', (product_id,)).fetchone()):
                raise RuntimeError('A checkpoint changed or a result now exists; do not resume research')
            states[str(job_id)] = sha(data)
    return {'installed_manifest_sha256': sha(installed_data), 'bridge_sha256': installed['after_sha256'],
            'driver_sha256': DRIVER_SHA, 'states_sha256': states}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        current = ssh({'apply': False})
        validate_snapshot(current)
        plan = {'version': 1, 'host': HOST, 'factory_db': FACTORY_DB, 'queue_db': QUEUE_DB,
                'jobs': list(TARGETS), 'snapshot': current, 'local_readiness': local_readiness()}
        write_json(RUN / 'resume-plan.json', plan)
        print(json.dumps({'apply': False, 'jobs': list(TARGETS), 'plan': str(RUN / 'resume-plan.json')}))
        return 0
    plan = json.loads((RUN / 'resume-plan.json').read_text(encoding='utf-8'))
    if ({key: plan.get(key) for key in ('version', 'host', 'factory_db', 'queue_db', 'jobs')} !=
            {'version': 1, 'host': HOST, 'factory_db': FACTORY_DB, 'queue_db': QUEUE_DB, 'jobs': list(TARGETS)}):
        raise RuntimeError('The reviewed recovery plan targets changed')
    validate_snapshot(plan['snapshot'])
    if local_readiness() != plan['local_readiness']:
        raise RuntimeError('Local installation or checkpoints changed after review')
    result = ssh({'apply': True, 'snapshot': plan['snapshot'], 'local_readiness': plan['local_readiness']})
    write_json(RUN / 'resume-result.json', result)
    print(json.dumps({key: result[key] for key in ('resumed', 'new_jobs_created', 'publications_created', 'backup')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
