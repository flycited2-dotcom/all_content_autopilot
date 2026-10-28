"""Repair the two failed research jobs whose brand was explicitly present in the supplier title."""
import argparse
import base64
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from listener.pending import write_json
from orchestrator_bot.catalog_selection import resolve_identity
from photo_auditor import progress, runtime

RUN = ROOT / 'runs/2026-10-27/catalog-brand-repair'
TARGETS = {3820: 'TP433', 3826: 'TP500'}
HOLD = 'PHOTO_AUDIT_HOLD: Для товарной фотографии нужны точные бренд и модель; требуется ручной разбор'


def validate(snapshot):
    if len(snapshot['jobs']) != 2 or len(snapshot['items']) != 2:
        raise RuntimeError('Expected the two original research jobs')
    items = {r['research_job']: r for r in snapshot['items']}
    for job in snapshot['jobs']:
        item = items.get(job['id'])
        sku = TARGETS.get(job['id'])
        if not sku or not item or (job['mode'], job['model'], job['status'], job['error_text']) != ('research', sku, 'failed', HOLD):
            raise RuntimeError('The research job changed')
        if job['brand'] not in (None, '') or item['brand'] not in (None, ''):
            raise RuntimeError('Do not overwrite an assigned brand')
        if (item['status'], item['card_job'], item['model'], item['error']) != ('failed', None, 'Термопот Blackton ' + sku, 'research: ' + HOLD):
            raise RuntimeError('The current factory item changed')


def remote(payload):
    code = 'import sqlite3,json,base64\nfrom pathlib import Path\nfrom datetime import datetime,timezone\n'
    code += f'TARGETS={TARGETS!r}\nHOLD={HOLD!r}\n' + inspect.getsource(validate)
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    code += '\npayload=json.loads(base64.b64decode(' + repr(encoded) + '))\n'
    code += r'''
mode='rw' if payload['apply'] else 'ro'
db=sqlite3.connect('file:/opt/content-factory/state/content_factory.db?mode='+mode,uri=True,timeout=30);db.row_factory=sqlite3.Row
db.execute('ATTACH DATABASE ? AS q',('file:/root/ritualb2b/queue.db?mode='+mode,))
db.execute('BEGIN IMMEDIATE' if payload['apply'] else 'BEGIN')
ids=tuple(TARGETS)
current={'jobs':[dict(r) for r in db.execute('SELECT * FROM q.jobs WHERE id IN (?,?) ORDER BY id',ids)],
'items':[dict(r) for r in db.execute('SELECT * FROM excel_items WHERE research_job IN (?,?) OR card_job IN (?,?) ORDER BY research_job',ids+ids)]}
validate(current)
if not payload['apply']:
 print(json.dumps(current,ensure_ascii=False))
else:
 if current!=payload['snapshot']: raise RuntimeError('Rows changed after review')
 backup=Path('/opt/agents-my/backups')/('catalog-brand-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'));backup.mkdir(mode=0o700)
 for source,name in (('/opt/content-factory/state/content_factory.db','factory.db'),('/root/ritualb2b/queue.db','queue.db')):
  with sqlite3.connect('file:'+source+'?mode=ro',uri=True) as reader, sqlite3.connect(backup/name) as saved:
   reader.backup(saved)
   if saved.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('Invalid backup')
 for job in current['jobs']:
  job_id=job['id'];item=next(i for i in current['items'] if i['research_job']==job_id)
  db.execute("UPDATE q.jobs SET brand='Blackton',status='pending',error_text=NULL,claimed_by=NULL,claimed_at=NULL,updated_at=? WHERE id=?",(datetime.now().isoformat(),job_id))
  db.execute("UPDATE excel_items SET brand='Blackton',model=?,status='research',error=NULL WHERE key=? AND research_job=?",(job['model'],item['key'],job_id))
 db.commit()
 import sys
 sys.path.insert(0,'/opt/agents-my')
 from orchestrator_bot.store import Store
 store=Store(Path('/opt/agents-my/state/orchestrator.db'))
 reopened=sum(store.reopen_recovered_batches(item['key'],recovery_id='catalog-brand:'+str(item['research_job'])) for item in current['items'])
 result={'resumed':list(TARGETS),'backup':str(backup),'new_jobs_created':False,'reopened_batches':reopened,'publication':False}
 (backup/'result.json').write_text(json.dumps(result),encoding='utf-8')
 print(json.dumps(result))
db.close()
'''
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'sprintbox-itp', 'python3 -'],
                       input=code.encode(), capture_output=True, timeout=90)
    if r.returncode:
        raise RuntimeError('Catalog identity repair stopped: ' + r.stderr.decode('utf-8', errors='replace')[-350:])
    return json.loads(r.stdout)


def readiness(snapshot):
    values = {}
    database = (ROOT / 'runs/photo-auditor/audit.sqlite3').as_uri() + '?mode=ro'
    with sqlite3.connect(database, uri=True) as db:
        for job in snapshot['jobs']:
            job_id, model = job['id'], job['model']
            item = next(i for i in snapshot['items'] if i['research_job'] == job_id)
            fixed = resolve_identity(item, {'Blackton'})
            if (fixed['brand'], fixed['model']) != ('Blackton', model):
                raise RuntimeError('Brand and SKU are not explicit in the supplier title')
            if db.execute('SELECT 1 FROM audits WHERE product_id=?', (f'fotogen-{job_id}',)).fetchone():
                raise RuntimeError('Do not replace identity of an already audited product')
            folder = progress.folder(ROOT, f'fotogen-{job_id}')
            if folder.exists() and list(folder.iterdir()):
                raise RuntimeError('A saved operation exists; preserve its identity')
            scope = runtime._read(ROOT, job_id)
            if scope != {'job_id': job_id, 'mode': 'research', 'brand': '', 'model': model, 'required': True, 'editorial': False}:
                raise RuntimeError('Signed original scope changed')
            values[str(job_id)] = runtime._path(ROOT, job_id).read_bytes().hex()
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    snapshot = remote({'apply': False})
    validate(snapshot)
    scopes = readiness(snapshot)
    if not args.apply:
        RUN.mkdir(parents=True, exist_ok=True)
        write_json(RUN / 'plan.json', {'server': snapshot, 'scopes': scopes})
        print(json.dumps({'apply': False, 'same_jobs': list(TARGETS), 'brand_explicit_in_title': 'Blackton'}))
        return
    plan = json.loads((RUN / 'plan.json').read_text(encoding='utf-8'))
    if plan != {'server': snapshot, 'scopes': scopes}:
        raise RuntimeError('Identity repair changed since review')
    write_json(RUN / 'original-scopes.json', scopes)
    try:
        for job in snapshot['jobs']:
            runtime._path(ROOT, job['id']).unlink()
            runtime.register_job(ROOT, job['id'], 'research', 'Blackton', job['model'], job['specs'])
        result = remote({'apply': True, 'snapshot': snapshot})
    except Exception:
        # SSH failure may occur after the server committed. Keep the explicitly
        # corrected identity rather than restoring a known invalid empty brand.
        write_json(RUN / 'remote-outcome-unknown.json', {'jobs': list(TARGETS), 'verify_server_before_retry': True})
        raise
    write_json(RUN / 'applied.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
