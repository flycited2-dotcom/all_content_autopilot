"""Bind the original brief and recheck saved Xiaomi card 3818 without a new image or job."""
import argparse
import base64
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from listener.pending import write_json
from photo_auditor import presentation, runtime
from photo_auditor.images import image_bytes
from photo_auditor.store import AuditStore
from tools.install_photo_audit import AGENT

RUN = ROOT / 'runs/2026-10-27/content-manager-v9'
HOST = 'sprintbox-itp'
JOB_ID = 3818
AUDIT_ID = '159ba88fa72a42ddaea5f3de55dd52ca'
PRODUCT_ID = 'fotogen-3818'
MODE = 'kbt'
BRAND = 'Xiaomi'
MODEL = 'TV S Mini LED 55 2026 L55MC-SRU 4K SmartTV Android'
ITEM_KEY = 'excel|xiaomi|tv s mini led 55 2026 l55mc-sru 4k smarttv android'
ERROR = 'PHOTO_AUDIT_HOLD: Фото остановлено для ручной проверки: 159ba88fa72a'
BRIDGE_SHA = '94c859cd41e2b78c60020c7777c702a309f362f62abe8576bcc0522efe890fe8'
DRIVER_SHA = '925f078082d25f146b616aacd413632cd67e53a5841a105dcfe1124d2c170974'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_server(value):
    if (not isinstance(value, dict) or set(value) != {'jobs', 'items'} or
            len(value['jobs']) != 1 or len(value['items']) != 1):
        raise RuntimeError('The current card job must have one factory item')
    job, item = value['jobs'][0], value['items'][0]
    if ((job['id'], job['mode'], job['brand'], job['model'], job['status'], job['error_text'],
            job['output_filename']) != (JOB_ID, MODE, BRAND, MODEL, 'failed', ERROR, None)):
        raise RuntimeError('Card 3818 no longer matches the reviewed audit hold')
    if not isinstance(job['specs'], str) or not 0 < len(job['specs']) <= 30000:
        raise RuntimeError('Original server generation specifications are missing')
    if ((item['key'], item['brand'], item['model'], item['status'], item['research_job'],
            item['card_job'], item['error']) !=
            (ITEM_KEY, BRAND, MODEL, 'failed', 3811, JOB_ID, 'card: ' + ERROR)):
        raise RuntimeError('Factory item advanced or no longer points to card 3818')


def server_snapshot():
    constants = '\n'.join(f'{key}={globals()[key]!r}' for key in
        ('JOB_ID', 'MODE', 'BRAND', 'MODEL', 'ITEM_KEY', 'ERROR'))
    source = ('import sqlite3,json\n' + constants + '\n' + inspect.getsource(validate_server) + r'''
factory = sqlite3.connect('file:/opt/content-factory/state/content_factory.db?mode=ro', uri=True, timeout=20)
factory.row_factory = sqlite3.Row
factory.execute('ATTACH DATABASE ? AS photo_queue', ('file:/root/ritualb2b/queue.db?mode=ro',))
factory.execute('PRAGMA query_only=ON')
factory.execute('BEGIN')
value = {'jobs': [dict(row) for row in factory.execute('SELECT * FROM photo_queue.jobs WHERE id=?', (JOB_ID,))],
         'items': [dict(row) for row in factory.execute('SELECT * FROM excel_items WHERE key=? OR card_job=? OR research_job=? ORDER BY key', (ITEM_KEY, JOB_ID, JOB_ID))]}
validate_server(value)
print(json.dumps(value, ensure_ascii=False))
factory.close()
''')
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', HOST, 'python3 -'],
        input=source.encode('utf-8'), capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Card recheck stopped: ' + result.stderr.decode('utf-8', errors='replace')[-800:])
    value = json.loads(result.stdout.decode('utf-8'))
    validate_server(value)
    return value


def generator_context(job):
    """Load the same local mode configuration and render_prompt used by the installed bridge."""
    path = AGENT / 'config.py'
    if path.is_symlink():
        raise RuntimeError('Generator configuration must be an ordinary file')
    module_name = '_reviewed_photo_generator_config'
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    config = module.get_mode(job['mode'])
    if config.key != MODE or not config.enabled:
        raise RuntimeError('The original generator mode is unavailable')
    prompt = config.render_prompt(job['specs'], brand=job['brand'], model=job['model'])
    if not isinstance(prompt, str) or not 0 < len(prompt) <= 60000:
        raise RuntimeError('The original rendered generation prompt is invalid')
    references = []
    for path in config.reference_files:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('A generator style reference is missing or changed')
        references.append({'path': str(path), 'sha256': sha(image_bytes(path.read_bytes()))})
    if len(references) > 8 or (config.requires_reference and not references):
        raise RuntimeError('Generator style references are unavailable')
    return {'config_sha256': sha((AGENT / 'config.py').read_bytes()), 'prompt': prompt,
            'prompt_sha256': sha(prompt.encode('utf-8')), 'references': references}


def local_snapshot():
    installed_path = RUN / 'installed.json'
    installed_data = installed_path.read_bytes()
    installed = json.loads(installed_data)
    bridge, driver = AGENT / 'remote_agent.py', AGENT / 'agent.py'
    if (installed.get('installed') is not True or Path(installed['target']) != bridge or
            installed['after_sha256'] != BRIDGE_SHA or installed['driver_sha256'] != DRIVER_SHA or
            bridge.is_symlink() or driver.is_symlink() or sha(bridge.read_bytes()) != BRIDGE_SHA or
            sha((RUN / 'remote_agent.py').read_bytes()) != BRIDGE_SHA or sha(driver.read_bytes()) != DRIVER_SHA or
            'PHOTO_AUDIT_BRIDGE_V9:' not in bridge.read_text(encoding='utf-8-sig')):
        raise RuntimeError('Reviewed V9 bridge is not installed')
    scope = runtime._read(ROOT, JOB_ID)
    if scope != {'job_id': JOB_ID, 'mode': MODE, 'brand': BRAND, 'model': MODEL,
                 'required': True, 'editorial': False}:
        raise RuntimeError('The signed card scope changed')
    database = ROOT / 'runs/photo-auditor/audit.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM audits WHERE product_id=? ORDER BY revision',
                                               (PRODUCT_ID,))]
    if len(rows) != 1:
        raise RuntimeError('A newer audit revision or another saved result exists')
    row = rows[0]
    expected_product = {'product_id': PRODUCT_ID, 'brand': BRAND, 'model': MODEL, 'revision': 0}
    if ((row['id'], row['revision'], row['status']) != (AUDIT_ID, 0, 'review') or
            json.loads(row['product']) != expected_product):
        raise RuntimeError('The saved audit is not the same reviewed product snapshot')
    audit_image = database.parent / AUDIT_ID / 'generated.bin'
    if audit_image.is_symlink() or sha(image_bytes(audit_image.read_bytes())) != row['sha256']:
        raise RuntimeError('The immutable audited image changed')
    folder = database.parent / 'recovery' / sha(PRODUCT_ID.encode())[:32]
    state_data = (folder / 'state.json').read_bytes()
    state = json.loads(state_data)
    if state != {**expected_product, 'phase': 'generated', 'sha256': row['sha256'], 'suffix': '.png'}:
        raise RuntimeError('The saved checkpoint is not the completed revision 0 image')
    if sha(image_bytes((folder / 'revision-0.png').read_bytes())) != row['sha256']:
        raise RuntimeError('The completed card checkpoint image changed')
    checkpoints = {path.name: sha(path.read_bytes()) for path in folder.iterdir() if path.is_file()}
    brief_files = {}
    for name in (f'{JOB_ID}.brief.json', f'{JOB_ID}.presentation.json'):
        path = runtime._path(ROOT, JOB_ID).with_name(name)
        if path.is_symlink():
            raise RuntimeError('Brief files must be ordinary files')
        brief_files[name] = sha(path.read_bytes()) if path.exists() else None
    return {'installed_manifest_sha256': sha(installed_data), 'audit_row': row,
            'checkpoint_files': checkpoints, 'brief_files': brief_files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    current = server_snapshot()
    local = local_snapshot()
    context = generator_context(current['jobs'][0])
    if not args.apply:
        plan = {'version': 1, 'host': HOST, 'job_id': JOB_ID, 'audit_id': AUDIT_ID,
                'server': current, 'local': local, 'generator_context': context}
        write_json(RUN / 'recheck-3818-plan.json', plan)
        print(json.dumps({'apply': False, 'job_id': JOB_ID, 'audit_id': AUDIT_ID,
                          'plan': str(RUN / 'recheck-3818-plan.json')}))
        return 0
    plan = json.loads((RUN / 'recheck-3818-plan.json').read_text(encoding='utf-8'))
    if ({key: plan.get(key) for key in ('version', 'host', 'job_id', 'audit_id')} !=
            {'version': 1, 'host': HOST, 'job_id': JOB_ID, 'audit_id': AUDIT_ID} or
            current != plan['server'] or local != plan['local'] or context != plan['generator_context']):
        raise RuntimeError('Current server rows, audit, checkpoint, or original generator rules changed after review')
    job = current['jobs'][0]
    runtime.register_job(ROOT, JOB_ID, job['mode'], job['brand'], job['model'], job['specs'])
    presentation.capture(ROOT, JOB_ID, context['prompt'], [asset['path'] for asset in context['references']])
    # Recheck only this immutable revision; AuditStore performs its own locked latest-revision/hash checks.
    store = AuditStore(ROOT / 'runs/photo-auditor')
    latest = store.latest(PRODUCT_ID)
    if (not latest or latest['id'] != AUDIT_ID or latest['revision'] != 0 or
            latest['status'] != 'review' or latest['sha256'] != local['audit_row']['sha256']):
        raise RuntimeError('The audited result changed while restoring its original brief')
    if not store.recheck(AUDIT_ID):
        raise RuntimeError('The same audit could not be queued for recheck')
    result = {'rechecked': AUDIT_ID, 'job_id': JOB_ID, 'revision': 0,
              'new_jobs_created': False, 'new_images_created': False, 'publications_created': False}
    write_json(RUN / 'recheck-3818-result.json', result)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
