"""Recompute deterministic false holds from two real saved editorial passes."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from listener.pending import write_json
from photo_auditor import audit_checkpoint,runtime
from photo_auditor.content_review import checked_content,_source_map,_decision,content_inventory
from photo_auditor.images import digest,image_bytes
from photo_auditor.store import AuditStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audit_id');parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();store=AuditStore(ROOT/'runs/photo-auditor')
    row=store.get(args.audit_id)
    if (not row or row['status']!='review' or store.latest(row['product_id'])['id']!=row['id']):
        raise RuntimeError('Only the current inconclusive saved inspection is eligible')
    report=row['report'];editorial=report.get('content_review',{})
    brief=runtime.content_brief(ROOT,row['product']);folder=store.root/row['id']
    visual=audit_checkpoint.load(ROOT,row['product'],folder,brief)
    if (not visual or report['sources']!=visual['sources'] or
            digest(image_bytes((folder/'generated.bin').read_bytes()))!=row['sha256'] or
            editorial.get('rules_version')!='content-manager-2' or
            editorial.get('brief_sha256')!=runtime.brief_digest(brief) or
            editorial.get('verdict')!='review' or len(editorial.get('passes',[]))!=2):
        raise RuntimeError('Missing unchanged visual evidence, brief, or two real editorial passes')
    sources=_source_map(report['sources'],row['product'])
    passes=[checked_content(json.dumps(p,ensure_ascii=False),sources,brief) for p in editorial['passes']]
    if any(_decision(p,sources)[0]!='approved' for p in passes):
        raise RuntimeError('A saved pass still has an unresolved claim or defect')
    inventories=[content_inventory(p,brief) for p in passes]
    if inventories[0]!=inventories[1]:
        raise RuntimeError('The two original passes disagree about the visible text')
    approved={**report,'verdict':'approved','reason':'Товар и содержание карточки проверены. '
              'Пересчитана программная сверка двух сохранённых независимых проходов.',
              'content_review':{**editorial,**passes[-1],'passes':passes,'verdict':'approved',
                'reason':'Оба сохранённых прохода и программная сверка подтверждают содержание.',
                'deterministic_revalidation':True}}
    if not runtime.approval_covers_brief(ROOT,row['product'],approved):
        raise RuntimeError('Result does not cover the signed brief')
    plan={'id':row['id'],'product_id':row['product_id'],'revision':row['revision'],
          'sha256':row['sha256'],'real_saved_editorial_passes':2,'new_model_calls':0,'new_image_generations':0}
    if not args.apply:
        print(json.dumps({'eligible':True,**plan}));return
    saved=ROOT/'runs/2026-10-27/numeric-revalidation'/datetime.now().strftime('%H%M%S-%f')
    saved.mkdir(parents=True)
    with sqlite3.connect(store.path) as db,sqlite3.connect(saved/'audit.sqlite3') as backup:db.backup(backup)
    write_json(saved/'before.json',row)
    if not store.recheck(row['id'],content_only=True):raise RuntimeError('Saved audit changed')
    claimed=store.claim(audit_id=row['id'])
    if not claimed:raise RuntimeError('Another real worker has already claimed the saved image; leave it to finish')
    store.finish(claimed,approved)
    write_json(saved/'result.json',plan)
    print(json.dumps({'approved':True,**plan}))


if __name__=='__main__':main()
