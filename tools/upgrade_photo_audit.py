"""Upgrade the reviewed bridge atomically; running jobs and browsers are untouched."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.install_photo_audit import AGENT, atomic

BASELINE = ROOT / 'runs/2026-10-23/autopilot/photo-agent-backup/remote_agent.py'
RUN = ROOT / 'runs/2026-10-25/photo-upgrade-v6'
EXPECTED_SOURCE_SHA = '72ae77430ebe8e2c9824d726bf58ecda156520df6ce4b1684850b80acef423f9'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    target = AGENT / 'remote_agent.py'
    before = target.read_bytes()
    if target.is_symlink() or hashlib.sha256(before).hexdigest() != EXPECTED_SOURCE_SHA:
        raise RuntimeError('The installed source changed; do not overwrite it')
    spec = importlib.util.spec_from_file_location('photo_patch', ROOT / 'scripts/prepare-photo-auditor-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    after = module.patched(BASELINE.read_text(encoding='utf-8')).encode('utf-8')
    plan = {'before_sha256': EXPECTED_SOURCE_SHA, 'after_sha256': hashlib.sha256(after).hexdigest(),
            'target': str(target), 'backup': str(RUN / 'backup')}
    RUN.mkdir(parents=True, exist_ok=True)
    if not args.apply:
        (RUN / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
        (RUN / 'remote-agent-v3.py').write_bytes(after)
        print(json.dumps({'apply': False, **plan}))
        return
    if json.loads((RUN / 'plan.json').read_text(encoding='utf-8')) != plan:
        raise RuntimeError('The reviewed plan changed')
    if (RUN / 'remote-agent-v3.py').read_bytes() != after:
        raise RuntimeError('The staged bridge changed')
    if list((AGENT / 'logs/upload_outbox').glob('*.json')):
        raise RuntimeError('An upload outbox needs delivery first')
    backup = RUN / 'backup'
    backup.mkdir()  # Never overwrite an earlier installation backup.
    (backup / 'remote_agent.py').write_bytes(before)
    (backup / 'photo-audit.json').write_bytes((AGENT / 'photo-audit.json').read_bytes())
    path = ROOT / 'runs/photo-auditor/audit.sqlite3'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        with sqlite3.connect(backup / 'audit.sqlite3') as saved:
            db.backup(saved)
    atomic(target, after)
    print(json.dumps({'installed': True, **plan}))


if __name__ == '__main__':
    main()
