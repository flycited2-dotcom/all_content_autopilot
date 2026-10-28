"""A real owner click must resume the exact file, without faking automatic checks."""
import asyncio
import copy
import json

import httpx
import pytest

from photo_auditor import integration, runtime, service
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, product, report


OWNER=123


@pytest.fixture
def held(tmp_path,monkeypatch):
    monkeypatch.setattr(runtime,'load',lambda _: {'api_token':'owner-decision-test'})
    monkeypatch.setattr(integration,'load',lambda _: {'api_token':'owner-decision-test'})
    monkeypatch.delenv('ORCH_API_TOKEN',raising=False)
    runtime.register_job(tmp_path,123,'kbt','Midea','ABC-12-RU','Исходное задание')
    store=AuditStore(tmp_path/'runs/photo-auditor')
    row=store.submit(product(),picture())
    store.finish(store.claim(audit_id=row['id']),report('review'))
    return tmp_path,store,store.get(row['id'])


class Telegram:
    def __init__(self,expired=False): self.events=[];self.expired=expired
    def answer_callback(self,*args):
        self.events.append(('answer',args))
        if self.expired:raise RuntimeError('expired callback')
    def send(self,*args):self.events.append(('send',args))
    def clear_buttons(self,*args):self.events.append(('clear',args))


def click(row,operation='approve',actor=OWNER):
    return {'callback_query':{'id':'real-test-click','from':{'id':actor},
        'message':{'chat':{'id':OWNER},'message_id':7},'data':f"qa:{operation}:{row['id']}"}}


@pytest.mark.parametrize('operation',['approve','match'])
def test_click_releases_saved_card_and_preserves_original_automatic_report(held,operation):
    root,store,row=held;tg=Telegram()
    service.handle_update(click(row,operation),store,tg,OWNER)
    accepted=store.get(row['id'])
    assert accepted['status']=='approved'
    assert accepted['sha256']==row['sha256'] and accepted['revision']==0
    assert accepted['report']['checks']==row['report']['checks']
    assert accepted['report']['content_review']['reviewer']=='owner'
    assert accepted['report']['owner_approval']['payload']['actor_id']==OWNER
    assert runtime.approval_covers_brief(root,row['product'],accepted['report'])
    with store.connect() as db:
        previous=json.loads(db.execute('SELECT report FROM audit_history WHERE audit_id=?',(row['id'],)).fetchone()[0])
        assert previous==row['report']
    assert tg.events[0][0]=='answer' and any(e[0]=='send' and 'Подтверждено' in e[1][1] for e in tg.events)


def test_click_through_recovery_and_upload_permit_without_rendering(held):
    root,store,row=held
    service.handle_update(click(row),store,Telegram(),OWNER)
    deliveries=[]
    service.recover_pipeline(store,lambda value: deliveries.append(value) or {'handled':True,'resumed':True})
    service.recover_pipeline(store,lambda value: pytest.fail('Duplicate server recovery'))
    assert len(deliveries)==1 and deliveries[0]['status']=='approved'
    image=store.root/row['id']/'generated.bin'
    def handle(request):
        return httpx.Response(200,json=service.public_result(store.get(row['id'])))
    async def run():
        async def forbidden(_):pytest.fail('Owner approval cannot buy another render')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await integration.checked_output(root,'fotogen-123','Midea','ABC-12-RU',
                image,None,forbidden,client=client)
    assert asyncio.run(run())==image
    runtime.guard_upload(root,123,image)


def test_repeated_click_is_idempotent(held):
    _,store,row=held
    service.handle_update(click(row),store,Telegram(),OWNER)
    service.handle_update(click(row),store,Telegram(),OWNER)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM audit_history').fetchone()[0]==1


def test_non_owner_click_does_nothing(held):
    _,store,row=held;tg=Telegram()
    service.handle_update(click(row,actor=999),store,tg,OWNER)
    assert store.get(row['id'])['status']=='review' and not tg.events


def test_expired_callback_ack_does_not_lose_owner_decision(held):
    _,store,row=held;tg=Telegram(expired=True)
    service.handle_update(click(row),store,tg,OWNER)
    assert store.get(row['id'])['status']=='approved'
    assert any(e[0]=='send' for e in tg.events)


@pytest.mark.parametrize('change',['missing','corrupt','different_valid_photo'])
def test_missing_or_changed_image_cannot_be_confirmed(held,change):
    _,store,row=held;image=store.root/row['id']/'generated.bin'
    if change=='missing':image.unlink()
    else:image.write_bytes(b'broken' if change=='corrupt' else picture('black'))
    tg=Telegram();service.handle_update(click(row),store,tg,OWNER)
    assert store.get(row['id'])['status']=='review'
    assert any(e[0]=='send' and 'не применено' in e[1][1] for e in tg.events)


def test_old_revision_click_cannot_approve_the_new_file(held):
    _,store,row=held
    with store.connect() as db:db.execute("UPDATE audits SET status='regenerate' WHERE id=?",(row['id'],))
    new=store.submit(product(1),picture('black'))
    store.finish(store.claim(audit_id=new['id']),report('review'));store.notified(new['id'])
    tg=Telegram();service.handle_update(click(row),store,tg,OWNER)
    assert store.get(new['id'])['status']=='review'
    assert store.get(new['id'])['notified']==0
    assert any(e[0]=='send' and 'старая фотография' in e[1][1] for e in tg.events)


@pytest.mark.parametrize('change',['signature','product','photo','brief','forged_reviewer'])
def test_owner_decision_cannot_move_to_other_content(held,change):
    root,store,row=held;store.owner_approve(row['id'],OWNER,'owner-click')
    accepted=copy.deepcopy(store.get(row['id'])['report'])
    if change=='signature':accepted['owner_approval']['signature']='0'*64
    elif change=='product':accepted['owner_approval']['payload']['product']['model']='OTHER'
    elif change=='photo':accepted['generated_sha256']='0'*64
    elif change=='forged_reviewer':accepted.pop('owner_approval')
    else:
        path=root/'runs/photo-auditor/job-scope/123.brief.json'
        signed=json.loads(path.read_text(encoding='utf-8'));signed['payload']['specs']='Новое задание'
        signed['signature']=runtime._signature(root,{'job_id':123,'brief':signed['payload']})
        path.write_text(json.dumps(signed),encoding='utf-8')
    assert not runtime.approval_covers_brief(root,row['product'],accepted)


def test_recheck_revokes_owner_approval_for_the_same_file(held):
    root,store,row=held;store.owner_approve(row['id'],OWNER,'owner-click')
    assert store.recheck(row['id'])
    assert store.get(row['id'])['report'] is None
    assert not integration._current_approval(root,row['product_id'],row['sha256'])


def test_recheck_button_queues_existing_photo(held):
    _,store,row=held;tg=Telegram()
    service.handle_update(click(row,'recheck'),store,tg,OWNER)
    after=store.get(row['id'])
    assert after['status']=='queued' and after['revision']==row['revision'] and after['sha256']==row['sha256']
    assert any(e[0]=='send' and 'повторную проверку' in e[1][1] for e in tg.events)


def test_explicit_publication_waits_for_delivery_and_retries_without_duplicates(held):
    _,store,row=held;tg=Telegram()
    service.handle_update(click(row,'publish'),store,tg,OWNER)
    assert store.get(row['id'])['report']['owner_approval']['payload']['publish_requested'] is True
    assert len(store.pending_publications())==1
    calls=[]
    def deliver(payload):
        calls.append(payload)
        return {'handled':False} if len(calls)==1 else {'handled':True,'published':True}
    service.recover_publications(store,deliver,tg=tg,owner=OWNER)
    assert len(store.pending_publications())==1
    service.recover_publications(store,deliver,tg=tg,owner=OWNER)
    service.recover_publications(store,lambda _:pytest.fail('Duplicate publication'))
    assert not store.pending_publications() and len(calls)==2
    assert sum(e[0]=='send' and 'Опубликовано в канале' in e[1][1] for e in tg.events)==1


def test_accept_without_publication_does_not_enqueue_publication(held):
    _,store,row=held
    service.handle_update(click(row),store,Telegram(),OWNER)
    assert not store.pending_publications()
    service.recover_publications(store,lambda _:pytest.fail('No publication was requested'))


def test_forged_publication_request_cannot_turn_acceptance_into_permission_to_publish(held):
    _,store,row=held;service.handle_update(click(row),store,Telegram(),OWNER)
    with store.connect() as db:
        db.execute('INSERT INTO meta VALUES(?,?)',('owner-publish:'+row['id'],json.dumps({'audit_id':row['id'],'status':'pending'})))
    service.recover_publications(store,lambda _:pytest.fail('No signed publication request'))
    assert not store.pending_publications()


def test_rechecked_owner_approval_cannot_publish_another_evaluation(held):
    _,store,row=held;service.handle_update(click(row,'publish'),store,Telegram(),OWNER)
    assert store.recheck(row['id'])
    service.recover_publications(store,lambda _:pytest.fail('Approval was revoked'))
    assert not store.pending_publications()


def test_telegram_polling_is_not_blocked_by_slow_recovery_or_reports(held,monkeypatch):
    import threading
    _,store,row=held;stop=threading.Event()
    class PollingTelegram(Telegram):
        def get_updates(self,*args,**kwargs):stop.set();return [{'update_id':1,**click(row)}]
    monkeypatch.setattr(service,'recover_pipeline',lambda *a,**k:pytest.fail('Slow maintenance in callback loop'))
    monkeypatch.setattr(service,'notify',lambda *a,**k:pytest.fail('Photo delivery in callback loop'))
    service.run_bot(store,PollingTelegram(),OWNER,stop)
    assert store.get(row['id'])['status']=='approved' and store.offset()==2


def test_repeat_publication_click_retries_failed_send_but_does_not_resend_published_card(held):
    _,store,row=held;tg=Telegram()
    service.handle_update(click(row,'publish'),store,tg,OWNER)
    service.recover_publications(store,lambda _: {'handled':True,'published':False},tg=tg,owner=OWNER)
    assert not store.pending_publications()
    service.handle_update(click(row,'publish'),store,tg,OWNER)
    assert len(store.pending_publications())==1
    service.recover_publications(store,lambda _: {'handled':True,'published':True},tg=tg,owner=OWNER)
    service.handle_update(click(row,'publish'),store,tg,OWNER)
    assert not store.pending_publications()
