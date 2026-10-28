"""Restore the reviewed V7 bridge atomically, keeping a recoverable V6 source."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.install_photo_audit import AGENT, atomic

EXPECTED_V6 = '32a72e2113ef8ff5ff903db35ddbdfeca5662810588ce393cb4eb3908d8cfc5d'
RUN = ROOT / 'runs/2026-10-25/photo-generator-v7'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    target = AGENT / 'remote_agent.py'
    if target.is_symlink():
        raise RuntimeError('Unexpected source symlink')
    before = target.read_bytes()
    if hashlib.sha256(before).hexdigest() != EXPECTED_V6:
        raise RuntimeError('Installed photo agent changed; do not overwrite it')
    spec = importlib.util.spec_from_file_location('photo_v7', ROOT / 'scripts/prepare-photo-auditor-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = ROOT / 'runs/2026-10-23/autopilot/photo-agent-backup/remote_agent.py'
    after = module.patched(baseline.read_text(encoding='utf-8')).encode('utf-8')
    plan = {'target': str(target), 'before_sha256': EXPECTED_V6,
            'after_sha256': hashlib.sha256(after).hexdigest(), 'backup': str(RUN / 'backup')}
    if not args.apply:
        RUN.mkdir(parents=True, exist_ok=True)
        (RUN / 'remote_agent.py').write_bytes(after)
        (RUN / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
        print(json.dumps({'installed': False, **plan}))
        return
    if (json.loads((RUN / 'plan.json').read_text(encoding='utf-8')) != plan or
            (RUN / 'remote_agent.py').read_bytes() != after):
        raise RuntimeError('Prepared repair changed; review it again')
    if list((AGENT / 'logs/upload_outbox').rglob('*.json')):
        raise RuntimeError('Pending outbox must be delivered before switching')
    backup = RUN / 'backup'
    backup.mkdir()
    (backup / 'remote_agent.py').write_bytes(before)
    atomic(target, after)
    print(json.dumps({'installed': True, **plan}))


if __name__ == '__main__':
    main()
