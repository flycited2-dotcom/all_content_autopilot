"""Install V8 and migrate only two reviewed, download-only Xiaomi checkpoints."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from listener.pending import write_json
from photo_auditor import progress, runtime
from tools.install_photo_audit import AGENT, atomic

RUN = ROOT / 'runs/2026-10-27/research-v8'
REMOTE_SHA = '0b48083c8c816fa0a685143f394626bb923f931c539f4a7d672d43d3918a1c7a'
DRIVER_SHA = '925f078082d25f146b616aacd413632cd67e53a5841a105dcfe1124d2c170974'
TARGETS = {
    3811: ('L55MC-SRU', '8c7bd2b52591631abd43f4ed653f039462009907cc7df76b4f2e82eeff661822'),
    3812: ('L43MB-APRU', '6623ab40a10f48aa6377806a05ef42608dfc628d95bb6b08b0a999d08126549d'),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare():
    target = AGENT / 'remote_agent.py'
    before = target.read_bytes()
    if target.is_symlink() or sha(before) != REMOTE_SHA or sha((AGENT / 'agent.py').read_bytes()) != DRIVER_SHA:
        raise RuntimeError('Reviewed installed source changed')
    spec = importlib.util.spec_from_file_location('research_v8', ROOT / 'scripts/prepare-photo-auditor-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = ROOT / 'runs/2026-10-23/autopilot/photo-agent-backup/remote_agent.py'
    after = module.patched(baseline.read_text(encoding='utf-8')).encode('utf-8')
    checkpoints = []
    database = ROOT / 'runs/photo-auditor/audit.sqlite3'
    with sqlite3.connect('file:' + database.as_posix() + '?mode=ro', uri=True) as db:
        for job_id, (model, fingerprint) in TARGETS.items():
            product_id = f'fotogen-{job_id}'
            scope = runtime._read(ROOT, job_id)
            if (scope.get('mode'), scope.get('brand'), scope.get('model'), scope.get('required')) != (
                    'research', 'Xiaomi', model, True):
                raise RuntimeError('Signed research identity changed')
            folder = progress.folder(ROOT, product_id)
            state_path = folder / 'state.json'
            state_bytes = state_path.read_bytes()
            state = json.loads(state_bytes)
            expected = {'product_id': product_id, 'brand': 'Xiaomi', 'model': model, 'revision': 0, 'phase': 'generating'}
            if state != expected or sha(state_bytes) != fingerprint:
                raise RuntimeError('Reviewed checkpoint changed')
            if {p.name for p in folder.iterdir()} != {'state.json'} or db.execute(
                    'SELECT 1 FROM audits WHERE product_id=?', (product_id,)).fetchone():
                raise RuntimeError('A saved result exists; do not reset its checkpoint')
            checkpoints.append({'job_id': job_id, 'model': model, 'before_sha256': fingerprint,
                                'path': str(state_path)})
    return before, after, {'target': str(target), 'before_sha256': REMOTE_SHA, 'after_sha256': sha(after),
                           'driver_sha256': DRIVER_SHA, 'checkpoints': checkpoints}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    before, after, plan = prepare()
    if not args.apply:
        RUN.mkdir(parents=True, exist_ok=True)
        (RUN / 'remote_agent.py').write_bytes(after)
        (RUN / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
        print(json.dumps({'installed': False, **plan}))
        return
    if json.loads((RUN / 'plan.json').read_text(encoding='utf-8')) != plan or (RUN / 'remote_agent.py').read_bytes() != after:
        raise RuntimeError('Prepared repair changed')
    backup = RUN / 'backup'
    backup.mkdir()
    (backup / 'remote_agent.py').write_bytes(before)
    for item in plan['checkpoints']:
        (backup / f"{item['job_id']}-state.json").write_bytes(Path(item['path']).read_bytes())
    atomic(Path(plan['target']), after)
    for item in plan['checkpoints']:
        progress.start_research(ROOT, f"fotogen-{item['job_id']}", 'Xiaomi', item['model'])
    write_json(RUN / 'installed.json', {'installed': True, **plan})
    print(json.dumps({'installed': True, 'same_jobs': list(TARGETS), 'checkpoint_phase': 'researching',
                      'backup': str(backup), 'after_sha256': plan['after_sha256']}))


if __name__ == '__main__':
    main()
