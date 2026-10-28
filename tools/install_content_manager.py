"""Install the reviewed V9 bridge without touching browser profiles or jobs."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.install_photo_audit import AGENT, atomic

BEFORE = '6f5d83380f505c40e4f6d3edf55eaba842821463aa952edb1021cd317b5bdc48'
DRIVER = '925f078082d25f146b616aacd413632cd67e53a5841a105dcfe1124d2c170974'
RUN = ROOT / 'runs/2026-10-27/content-manager-v9'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    target = AGENT / 'remote_agent.py'
    driver = AGENT / 'agent.py'
    sha = lambda b: hashlib.sha256(b).hexdigest()
    if target.is_symlink() or driver.is_symlink() or sha(target.read_bytes()) != BEFORE or sha(driver.read_bytes()) != DRIVER:
        raise RuntimeError('Reviewed photo-agent source changed')
    before = target.read_bytes()
    baseline = ROOT / 'runs/2026-10-23/autopilot/photo-agent-backup/remote_agent.py'
    source = baseline.read_text(encoding='utf-8')
    spec = importlib.util.spec_from_file_location('manager_patch', ROOT / 'scripts/prepare-photo-auditor-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    after = module.patched(source).encode('utf-8')
    compile(after, str(target), 'exec')
    plan = {'target': str(target), 'before_sha256': BEFORE, 'after_sha256': sha(after),
            'driver_sha256': DRIVER, 'backup': str(RUN / 'backup')}
    if not args.apply:
        RUN.mkdir(parents=True, exist_ok=True)
        (RUN / 'remote_agent.py').write_bytes(after)
        (RUN / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
        print(json.dumps({'installed': False, **plan}))
        return
    if (json.loads((RUN / 'plan.json').read_text(encoding='utf-8')) != plan or
            (RUN / 'remote_agent.py').read_bytes() != after):
        raise RuntimeError('Prepared patch changed after review')
    if list((AGENT / 'logs/upload_outbox').rglob('*.json')):
        raise RuntimeError('A pending upload must complete before installation')
    backup = RUN / 'backup'
    backup.mkdir()
    (backup / 'remote_agent.py').write_bytes(before)
    (backup / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    atomic(target, after)
    (RUN / 'installed.json').write_text(json.dumps({'installed': True, **plan}, indent=2), encoding='utf-8')
    print(json.dumps({'installed': True, **plan}))


if __name__ == '__main__':
    main()
