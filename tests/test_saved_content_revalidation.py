"""A deterministic repair may reuse two real passes, never invent their approval."""
from copy import deepcopy
import sys

import pytest

from photo_auditor.engine import Auditor
from photo_auditor.store import AuditStore
from tests.test_photo_content_pipeline import pipeline, PRODUCT, Provider, Web, picture
from tools import revalidate_saved_content as repair


def saved_hold(pipeline):
    root,_=pipeline;store=AuditStore(root/'runs/photo-auditor')
    row=store.submit(PRODUCT,picture());claimed=store.claim(audit_id=row['id'])
    report=Auditor(root,Provider(),Web()).run(PRODUCT,store.root/row['id'])
    assert report['verdict']=='approved' and len(report['content_review']['passes'])==2
    report.update(verdict='review',reason='Программная числовая сверка не завершена.')
    report['content_review']['verdict']='review'
    store.finish(claimed,report)
    return root,store,store.get(row['id'])


def test_revalidation_preserves_real_passes_and_saved_image(pipeline,monkeypatch):
    root,store,row=saved_hold(pipeline)
    before=(store.root/row['id']/'generated.bin').read_bytes()
    monkeypatch.setattr(repair,'ROOT',root)
    monkeypatch.setattr(sys,'argv',['revalidate',row['id'],'--apply'])
    repair.main()
    result=store.get(row['id'])
    assert result['status']=='approved'
    assert result['revision']==row['revision'] and result['sha256']==row['sha256']
    assert result['report']['content_review']['passes']==row['report']['content_review']['passes']
    assert (store.root/row['id']/'generated.bin').read_bytes()==before
    assert len(list((root/'runs/2026-10-27/numeric-revalidation').glob('*/before.json')))==1


@pytest.mark.parametrize('change',['one_pass','unproven_number','inventory','changed_photo','newer_revision'])
def test_revalidation_rejects_incomplete_or_changed_evidence(pipeline,monkeypatch,change):
    root,store,row=saved_hold(pipeline)
    report=deepcopy(row['report']);content=report['content_review']
    if change=='one_pass':content['passes'].pop()
    elif change=='unproven_number':
        p=content['passes'][0];p['visible_text'][1]=p['visible_text'][1].replace('65','75')
        p['claims'][1]['text']=p['claims'][1]['text'].replace('65','75')
    elif change=='inventory':
        # Both responses can be individually valid yet disagree about the copy.
        p=content['passes'][1];p['visible_text'].append('Удобный')
        p['claims'].append({'id':3,'text':'Удобный','kind':'other','status':'not_claim',
                           'confidence':.99,'reason':'Оценочный заголовок.','fix':'','evidence':[]})
    if change in ('one_pass','unproven_number','inventory'):
        with store.connect() as db:
            import json
            db.execute('UPDATE audits SET report=? WHERE id=?',(json.dumps(report),row['id']))
    elif change=='changed_photo':(store.root/row['id']/'generated.bin').write_bytes(b'changed')
    else:
        with store.connect() as db:db.execute("UPDATE audits SET status='regenerate' WHERE id=?",(row['id'],))
        store.submit({**PRODUCT,'revision':1},picture())
        with store.connect() as db:db.execute("UPDATE audits SET status='review' WHERE id=?",(row['id'],))
    monkeypatch.setattr(repair,'ROOT',root)
    monkeypatch.setattr(sys,'argv',['revalidate',row['id'],'--apply'])
    with pytest.raises((RuntimeError,ValueError)):repair.main()
    assert store.get(row['id'])['status']=='review'
