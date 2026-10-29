"""Install the reviewed orchestrator package; snapshots and hash checks precede restart."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'runs/2026-10-23/autopilot/server-manifest.json'

REMOTE = r'''
import hashlib,json,os,pathlib,re,shutil,sqlite3,subprocess,time,urllib.request,urllib.error
from datetime import datetime,timezone
base=pathlib.Path('/opt/agents-my')
stage=pathlib.Path(manifest['staging'])
live=base/'orchestrator_bot'
assert base.resolve()==base and stage.resolve()==stage and live.resolve()==live
assert stage.parent==base/'.deploy' and re.fullmatch(r'autopilot-[0-9]{8}(?:-[0-9]{6})?',stage.name)
def hashes(folder):
 return {'orchestrator_bot/'+p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.py')}
assert hashes(live)==manifest['before'], 'Live source changed; deployment stopped'
assert hashes(stage/'orchestrator_bot')==manifest['after'], 'Staged source changed; deployment stopped'
assert shutil.disk_usage(base).free>512*1024*1024, 'Insufficient free space'
backup=base/'backups'/('autopilot-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
backup.mkdir(parents=True,mode=0o700)
os.chmod(backup,0o700)
db=sqlite3.connect((base/'state/orchestrator.db').as_uri()+'?mode=ro',uri=True)
assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok', 'Database integrity check failed'
target=sqlite3.connect(backup/'orchestrator.db')
db.backup(target); target.close(); db.close()
os.chmod(backup/'orchestrator.db',0o600)
subprocess.run(['systemctl','stop','orchestrator-bot.service'],check=True)
moved=False
try:
 os.replace(live,backup/'orchestrator_bot'); moved=True
 os.replace(stage/'orchestrator_bot',live)
 subprocess.run(['systemctl','start','orchestrator-bot.service'],check=True)
 ready=False
 for _ in range(90):
  try:
   urllib.request.urlopen('http://127.0.0.1:8790/next',timeout=1)
  except urllib.error.HTTPError as error:
   if error.code==401: ready=True; break
  except (urllib.error.URLError,TimeoutError): pass
  time.sleep(.5)
 assert ready, 'Orchestrator HTTP service did not become ready'
 assert subprocess.run(['systemctl','is-active','orchestrator-bot.service'],capture_output=True,text=True).stdout.strip()=='active'
except Exception:
 subprocess.run(['systemctl','stop','orchestrator-bot.service'],check=False)
 if moved:
  if live.exists(): os.replace(live,backup/'failed-package')
  os.replace(backup/'orchestrator_bot',live)
 subprocess.run(['systemctl','start','orchestrator-bot.service'],check=False)
 raise
result={'installed':True,'backup':str(backup),'service':'active','database_backup':True,'changed':manifest['changed']}
(backup/'deployment.json').write_text(json.dumps(result),encoding='utf-8')
print(json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not args.apply:
        print(json.dumps({'apply': False, 'changed': manifest['changed'], 'staging': manifest['staging']}))
        return
    payload = base64.b64encode(json.dumps(manifest).encode()).decode()
    script = 'import base64,json\nmanifest=json.loads(base64.b64decode(' + repr(payload) + '))\n' + REMOTE
    process = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'sprintbox-itp', 'python3 -'],
                             input=script.encode(), capture_output=True, timeout=240)
    if process.returncode:
        raise RuntimeError('Deployment failed; original service restoration was attempted. ' +
                           process.stderr.decode('utf-8', errors='replace')[-1000:])
    output = process.stdout.decode('utf-8')
    (manifest_path.parent / 'server-deployed.json').write_text(output, encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
