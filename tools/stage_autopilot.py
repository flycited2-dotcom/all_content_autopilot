"""Stage the owned package and retain the exact live source before deployment."""
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    output = ROOT / 'runs' / datetime.now().date().isoformat() / ('deploy-' + stamp)
    output.mkdir(parents=True)
    remote = '/opt/agents-my/.deploy/autopilot-' + stamp
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'sprintbox-itp']
    snapshot = subprocess.run(ssh + ['cd /opt/agents-my && tar -cf - --exclude=__pycache__ orchestrator_bot'],
                              capture_output=True, check=True, timeout=30).stdout
    (output / 'orchestrator-before.tar').write_bytes(snapshot)
    before = {}
    with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
        for item in archive:
            if item.isfile() and item.name.endswith('.py'):
                before[item.name] = hashlib.sha256(archive.extractfile(item).read()).hexdigest()
    bundle, after = io.BytesIO(), {}
    with tarfile.open(fileobj=bundle, mode='w') as archive:
        for source in sorted((ROOT / 'orchestrator_bot').glob('*.py')):
            name = 'orchestrator_bot/' + source.name
            after[name] = hashlib.sha256(source.read_bytes()).hexdigest()
            archive.add(source, arcname=name)
    subprocess.run(ssh + ['mkdir -p ' + remote + ' && tar -xf - -C ' + remote],
                   input=bundle.getvalue(), capture_output=True, check=True, timeout=30)
    subprocess.run(ssh + ['python3 -m compileall -q ' + remote + '/orchestrator_bot'],
                   capture_output=True, check=True, timeout=30)
    manifest = {'staging': remote, 'before': before, 'after': after,
                'changed': [n for n in after if before.get(n) != after[n]],
                'removed': [n for n in before if n not in after]}
    path = output / 'server-manifest.json'
    path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({'manifest': str(path), 'changed': manifest['changed'], 'removed': manifest['removed']}))


if __name__ == '__main__':
    main()
