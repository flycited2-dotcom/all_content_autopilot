"""One explicitly approved cohort, stable trial ID and read-only progress reports."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/2026-10-23/autopilot'

REMOTE = r'''
import json,sqlite3,time
from collections import defaultdict
from types import SimpleNamespace
from pathlib import Path
from content_factory import agent_cli as cf
from content_factory.orchestrator.excel_pipeline import ExcelStore
from orchestrator_bot.catalog_cli import resolve
from orchestrator_bot.store import Store
state=Store('/opt/agents-my/state/orchestrator.db')
marker='autopilot_trial:'+manifest['id']
record=state.get_meta(marker)
args=SimpleNamespace(state_db=None,prices_dir=None,queue_db=None,source=None,category='',
 count=None,quotas=None,btu=None,min_temp=None,inverter=False,heat_pump=False,full_dc=False,
 selection='manual',keys_json=None)
state_db,_,queue_db=cf._paths(args)
pipeline=ExcelStore(state_db)
if start and not record:
 groups=defaultdict(list)
 for item in manifest['items']: groups[item['category']].append(item)
 fresh=[]
 for category,items in groups.items():
  args.category=category;args.count=len(items);args.keys_json=json.dumps([i['key'] for i in items])
  rows=resolve(cf,args)['items']
  expected={i['key']:(i['brand'],i['model']) for i in items}
  assert len(rows)==len(items), 'Not all selected models are available'
  assert all(expected[row['key']]==(row['brand'],row['model']) for row in rows), 'Catalog identity changed'
  fresh.extend(rows)
 assert len(fresh)==10 and len({r['key'] for r in fresh})==10
 with state._c() as db:
  db.execute('BEGIN IMMEDIATE')
  assert db.execute('SELECT 1 FROM meta WHERE key=?',(marker,)).fetchone() is None, 'Trial already reserved'
  db.execute('INSERT INTO meta VALUES(?,?)',(marker,json.dumps({'state':'reserved','started':time.time()})))
  db.execute('COMMIT')
 accepted=pipeline.select_items([(r['key'],r['brand'],r['model'],r['name'],r['price']) for r in fresh],queue_db=queue_db)
 keys=[row[0] for row in accepted]
 result={'accepted':keys,'requested':10,'items':[r for r in fresh if r['key'] in keys]}
 log_id=state.log_action(None,'enqueue',{'type':'enqueue_products','category':'Тест автопилота',
                                      'count':10,'selection':'manual','keys':keys},result)
 state.add_batch(log_id,'Тест автопилота — 10 карточек',keys,None)
 record=json.dumps({'state':'started','accepted':keys,'started':time.time(),'log_id':log_id})
 state.set_meta(marker,record)
 from content_factory.bot import worker_control
 from content_factory.cards_pipeline import wake_agent
 worker_control.request_run()
 if queue_db: wake_agent(queue_db)
elif start and record and json.loads(record).get('state')=='reserved':
 raise RuntimeError('Interrupted trial reservation; inspect instead of retrying generation')
result={'trial':json.loads(record) if record else None,'items':[]}
for original in manifest['items']:
 item=pipeline.get(original['key'])
 result['items'].append({'key':original['key'],'brand':original['brand'],'model':original['model'],
                        'status':item.status if item else 'not_submitted',
                        'research_job':item.research_job if item else None,
                        'card_job':item.card_job if item else None})
print(json.dumps(result,ensure_ascii=False))
'''


def check_installed():
    import httpx
    from photo_auditor.credentials import load
    token = os.environ.get('ORCH_API_TOKEN')
    if not token:
        raise RuntimeError('Orchestrator connection is not configured')
    health = httpx.get('http://127.0.0.1:8790/health', headers={'X-Orch-Token': token},
                       timeout=10, trust_env=False)
    health.raise_for_status()
    if 'product_selection' not in health.json().get('capabilities', []):
        raise RuntimeError('New orchestrator is not installed')
    config = load(ROOT)
    audit = httpx.get('http://127.0.0.1:8793/status', headers={'X-Audit-Token': config['api_token']},
                      timeout=10, trust_env=False)
    audit.raise_for_status()
    agent_config = ROOT.parent / 'agent_convert_foto_rituailb2b2_V2_office/agent_convert_foto_rituailb2b2/photo-audit.json'
    settings = json.loads(agent_config.read_text(encoding='utf-8-sig'))
    if settings.get('enabled') is not True or Path(settings.get('project_root', '')).resolve() != ROOT:
        raise RuntimeError('Photo agent audit is not enabled')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((RUN / 'sample-manifest.json').read_text(encoding='utf-8'))
    if not args.start and not args.report:
        print(json.dumps({'submitted': False, 'count': len(manifest['items']),
                          'items': [{k: row[k] for k in ('brand', 'model', 'category')} for row in manifest['items']]},
                         ensure_ascii=False))
        return
    if args.start:
        check_installed()
    encoded = base64.b64encode(json.dumps(manifest, ensure_ascii=False).encode()).decode()
    code = ('import base64,json\nmanifest=json.loads(base64.b64decode(' + repr(encoded) + '))\n'
            'start=' + repr(args.start) + '\n' + REMOTE)
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'sprintbox-itp',
        'cd /opt/content-factory && PYTHONPATH=src:/opt/agents-my .venv/bin/python -'],
        input=code.encode(), capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('Trial was not confirmed; do not resubmit automatically. ' +
                           result.stderr.decode('utf-8', errors='replace')[-1200:])
    value = json.loads(result.stdout.decode('utf-8'))
    (RUN / 'trial-state.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    counts = {}
    for item in value['items']:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    print(json.dumps({'trial': value['trial'], 'counts': counts}, ensure_ascii=False))


if __name__ == '__main__':
    main()
