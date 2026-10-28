"""Review, then resume only current Xiaomi source-download job 3817 after CDP recovery."""
import argparse
import base64
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from listener.pending import write_json
from photo_auditor import runtime
from tools.install_photo_audit import AGENT

RUN = ROOT / 'runs/2026-10-27/content-manager-v9'
HOST = 'sprintbox-itp'
FACTORY_DB = '/opt/content-factory/state/content_factory.db'
QUEUE_DB = '/root/ritualb2b/queue.db'
JOB_ID = 3817
MODEL = 'L43MB-APRU'
ITEM_KEY = 'excel|xiaomi|mi tv a pro 2026 l43mb-apru qled 4k smarttv android'
ITEM_MODEL = 'MI TV A PRO 2026 L43MB-APRU QLED 4K SmartTV Android'
DRIVER_SHA = '925f078082d25f146b616aacd413632cd67e53a5841a105dcfe1124d2c170974'
ERROR = ('BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9334\n'
         'Call log:\n  - <ws preparing> retrieving websocket url from http://127.0.0.1:9334')


def snapshot(db):
    jobs = [dict(row) for row in db.execute('SELECT * FROM photo_queue.jobs WHERE id=?', (JOB_ID,))]
    items = [dict(row) for row in db.execute('SELECT * FROM excel_items WHERE key=? '
        'OR research_job=? OR card_job=? ORDER BY key', (ITEM_KEY, JOB_ID, JOB_ID))]
    return {'jobs': jobs, 'items': items}


def validate_snapshot(value):
    if (not isinstance(value, dict) or set(value) != {'jobs', 'items'} or
            len(value['jobs']) != 1 or len(value['items']) != 1):
        raise RuntimeError('The sole current Xiaomi job and factory item are required')
    job, item = value['jobs'][0], value['items'][0]
    if ((job['id'], job['mode'], job['brand'], job['model'], job['status'], job['error_text'],
            job['output_filename'], job['claimed_by'], job['assigned_account']) !=
            (JOB_ID, 'research', 'Xiaomi', MODEL, 'failed', ERROR, None, 'desktop-a2', 'acc2')):
        raise RuntimeError('Job 3817 no longer matches the reviewed source-download CDP failure')
    if ((item['key'], item['brand'], item['model'], item['status'], item['research_job'],
            item['card_job'], item['error']) !=
            (ITEM_KEY, 'Xiaomi', ITEM_MODEL, 'failed', JOB_ID, None, 'research: ' + ERROR)):
        raise RuntimeError('Current Xiaomi item was superseded or advanced; no job may be resumed')


def require_unchanged(expected, current):
    validate_snapshot(expected)
    validate_snapshot(current)
    if current != expected:
        raise RuntimeError('Server rows changed after review; no job was resumed')


REMOTE = r'''
from datetime import datetime, timezone
import os
factory, queue = Path(FACTORY_DB), Path(QUEUE_DB)
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
        backup_root = Path('/opt/agents-my/backups')
        if backup_root.resolve() != backup_root:
            raise RuntimeError('Unexpected backup directory')
        backup = backup_root / ('xiaomi-3817-resume-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir(mode=0o700)
        for source, name in ((factory, 'content_factory.db'), (queue, 'queue.db')):
            with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30) as reader:
                with sqlite3.connect(backup / name) as saved:
                    reader.backup(saved)
                    if saved.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Database backup failed integrity verification')
            os.chmod(backup / name, 0o600)
        (backup / 'resume-plan.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(backup / 'resume-plan.json', 0o600)
        changed = db.execute("UPDATE photo_queue.jobs SET status='pending',claimed_by=NULL,claimed_at=NULL,"
            "error_text=NULL,updated_at=? WHERE id=? AND status='failed' AND mode='research' "
            'AND brand=? AND model=? AND error_text=? AND output_filename IS NULL',
            (datetime.now().isoformat(), JOB_ID, 'Xiaomi', MODEL, ERROR)).rowcount
        if changed != 1:
            raise RuntimeError('Job changed during recovery')
        changed = db.execute("UPDATE excel_items SET status='research',error=NULL WHERE key=? "
            "AND research_job=? AND card_job IS NULL AND status='failed' AND brand=? AND model=? AND error=?",
            (ITEM_KEY, JOB_ID, 'Xiaomi', ITEM_MODEL, 'research: ' + ERROR)).rowcount
        if changed != 1:
            raise RuntimeError('Factory item changed during recovery')
        after = snapshot(db)
        db.commit()
        result = {'resumed': [JOB_ID], 'new_jobs_created': False, 'publications_created': False,
                  'backup': str(backup), 'after': after}
        (backup / 'resume-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(backup / 'resume-result.json', 0o600)
        print(json.dumps(result, ensure_ascii=False))
finally:
    db.close()
'''


def remote_script(payload):
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('ascii')
    constants = '\n'.join(f'{name}={globals()[name]!r}' for name in
        ('FACTORY_DB', 'QUEUE_DB', 'JOB_ID', 'MODEL', 'ITEM_KEY', 'ITEM_MODEL', 'ERROR'))
    functions = '\n\n'.join(inspect.getsource(fn) for fn in (snapshot, validate_snapshot, require_unchanged))
    return ('import base64,json,sqlite3\nfrom pathlib import Path\n' + constants + '\n' + functions +
            '\npayload=json.loads(base64.b64decode(' + repr(encoded) + '))\n' + REMOTE)


def ssh(payload):
    process = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', HOST, 'python3 -'],
        input=remote_script(payload).encode('utf-8'), capture_output=True, timeout=120)
    if process.returncode:
        raise RuntimeError('Xiaomi recovery stopped: ' + process.stderr.decode('utf-8', errors='replace')[-800:])
    return json.loads(process.stdout.decode('utf-8'))


def lane_health():
    """Observe an idle poll and working CDP without opening or operating any browser tab."""
    log = AGENT / 'logs/remote_agent_desktop-a2.log'
    with log.open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 65536))
        lines = stream.read().decode('utf-8', errors='replace').splitlines()
    if not lines or not re.search(r'GET http://127\.0\.0\.1:\d+/api/next-job\?[^ ]*lane=desktop-a2[^ ]* '
                                 r'"HTTP/1\.1 204 No Content"$', lines[-1]):
        raise RuntimeError('Lane desktop-a2 is not confirmed idle; preserve its current operation')
    polled = datetime.strptime(lines[-1][:23], '%Y-%m-%d %H:%M:%S,%f')
    age = (datetime.now() - polled).total_seconds()
    if not 0 <= age <= 120:
        raise RuntimeError('Lane idle evidence is stale')
    with urlopen('http://127.0.0.1:9334/json/version', timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('Chrome CDP is unavailable')
        version = json.load(response)
    if not isinstance(version.get('Browser'), str) or not version.get('webSocketDebuggerUrl'):
        raise RuntimeError('Chrome CDP did not return a browser identity')
    return {'lane': 'desktop-a2', 'idle_at': polled.isoformat(), 'cdp_port': 9334, 'cdp_available': True}


def local_readiness():
    installed_path = RUN / 'installed.json'
    installed_data = installed_path.read_bytes()
    installed = json.loads(installed_data)
    target, driver = AGENT / 'remote_agent.py', AGENT / 'agent.py'
    sha = lambda data: hashlib.sha256(data).hexdigest()
    if (installed.get('installed') is not True or Path(installed['target']) != target or
            target.is_symlink() or driver.is_symlink() or
            sha(target.read_bytes()) != installed['after_sha256'] or
            sha((RUN / 'remote_agent.py').read_bytes()) != installed['after_sha256'] or
            installed['driver_sha256'] != DRIVER_SHA or sha(driver.read_bytes()) != DRIVER_SHA or
            'PHOTO_AUDIT_BRIDGE_V9:' not in target.read_text(encoding='utf-8-sig')):
        raise RuntimeError('Reviewed V9 bridge is not installed')
    scope = runtime._read(ROOT, JOB_ID)
    if scope != {'job_id': JOB_ID, 'mode': 'research', 'brand': 'Xiaomi', 'model': MODEL,
                 'required': True, 'editorial': False}:
        raise RuntimeError('Signed source-download scope changed')
    product_id = f'fotogen-{JOB_ID}'
    folder = ROOT / 'runs/photo-auditor/recovery' / sha(product_id.encode())[:32]
    state_path = folder / 'state.json'
    state_data = state_path.read_bytes()
    expected = {'product_id': product_id, 'brand': 'Xiaomi', 'model': MODEL, 'revision': 0,
                'phase': 'researching', 'operation': 'verified_source_download'}
    database = ROOT / 'runs/photo-auditor/audit.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        if (state_path.is_symlink() or json.loads(state_data) != expected or
                {file.name for file in folder.iterdir()} != {'state.json'} or
                db.execute('SELECT 1 FROM audits WHERE product_id=?', (product_id,)).fetchone()):
            raise RuntimeError('A result, audit, or different operation exists; do not repeat source lookup')
    return {'binding': {'installed_manifest_sha256': sha(installed_data),
                        'bridge_sha256': installed['after_sha256'], 'driver_sha256': DRIVER_SHA,
                        'state_sha256': sha(state_data)}, 'health': lane_health()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    readiness = local_readiness()
    if not args.apply:
        current = ssh({'apply': False})
        validate_snapshot(current)
        plan = {'version': 1, 'host': HOST, 'factory_db': FACTORY_DB, 'queue_db': QUEUE_DB,
                'jobs': [JOB_ID], 'snapshot': current, 'local_readiness': readiness}
        write_json(RUN / 'resume-3817-plan.json', plan)
        print(json.dumps({'apply': False, 'jobs': [JOB_ID], 'plan': str(RUN / 'resume-3817-plan.json'),
                          'health': readiness['health']}))
        return 0
    plan = json.loads((RUN / 'resume-3817-plan.json').read_text(encoding='utf-8'))
    if ({key: plan.get(key) for key in ('version', 'host', 'factory_db', 'queue_db', 'jobs')} !=
            {'version': 1, 'host': HOST, 'factory_db': FACTORY_DB, 'queue_db': QUEUE_DB, 'jobs': [JOB_ID]}):
        raise RuntimeError('The reviewed current Xiaomi recovery targets changed')
    validate_snapshot(plan['snapshot'])
    if readiness['binding'] != plan['local_readiness']['binding']:
        raise RuntimeError('V9 installation or source-download checkpoint changed after review')
    result = ssh({'apply': True, 'snapshot': plan['snapshot'], 'local_readiness': readiness})
    write_json(RUN / 'resume-3817-result.json', result)
    print(json.dumps({key: result[key] for key in ('resumed', 'new_jobs_created', 'publications_created', 'backup')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
