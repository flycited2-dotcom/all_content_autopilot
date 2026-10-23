"""Reviewed, hash-checked local photo-agent bridge. Does not manage processes."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT.parent / 'agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2'
RUN = ROOT / 'runs/2026-10-23/autopilot'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepared():
    if AGENT.resolve() != AGENT or not (AGENT / 'remote_agent.py').is_file():
        raise RuntimeError('Unexpected photo-agent location')
    if any(path.is_symlink() for path in (AGENT / 'remote_agent.py', AGENT / 'photo-audit.json')):
        raise RuntimeError('Photo-agent source and settings must be ordinary files')
    pending = list((AGENT / 'logs/upload_outbox').glob('*/*.json'))
    if pending:
        raise RuntimeError('Pending photo outbox exists; preserve and resolve before switching')
    original = (AGENT / 'remote_agent.py').read_bytes()
    spec = importlib.util.spec_from_file_location('photo_audit_patch', ROOT / 'scripts/prepare-photo-auditor-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patched = module.patched(original.decode('utf-8').replace('\r\n', '\n')).encode('utf-8')
    return original, patched


def atomic(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    original, patched = prepared()
    settings = {'enabled': True, 'project_root': str(ROOT)}
    plan_path = RUN / 'photo-agent-plan.json'
    plan = {'agent_root': str(AGENT), 'before_sha256': digest(original),
            'after_sha256': digest(patched), 'settings': settings,
            'backup': str(RUN / 'photo-agent-backup')}
    if not args.apply:
        RUN.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2), encoding='utf-8')
        print(json.dumps({'apply': False, **plan}))
        return
    expected = json.loads(plan_path.read_text(encoding='utf-8'))
    if expected != plan:
        raise RuntimeError('Photo agent changed since review; installation stopped')
    backup = Path(plan['backup'])
    if backup.exists():
        raise RuntimeError('Backup exists; do not overwrite a previous installation')
    backup.mkdir()
    (backup / 'remote_agent.py').write_bytes(original)
    config_path = AGENT / 'photo-audit.json'
    if config_path.exists():
        (backup / 'photo-audit.json').write_bytes(config_path.read_bytes())
    (backup / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    try:
        atomic(AGENT / 'remote_agent.py', patched)
        atomic(config_path, json.dumps(settings, indent=2).encode('utf-8'))
    except Exception:
        atomic(AGENT / 'remote_agent.py', original)
        if (backup / 'photo-audit.json').exists():
            atomic(config_path, (backup / 'photo-audit.json').read_bytes())
        elif config_path.exists():
            config_path.unlink()
        raise
    print(json.dumps({'installed': True, 'agent_root': str(AGENT), 'backup': str(backup),
                      'after_sha256': digest((AGENT / 'remote_agent.py').read_bytes())}))


if __name__ == '__main__':
    main()
