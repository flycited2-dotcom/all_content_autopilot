"""Auditor publication must wait for, and publish only, the accepted file."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from orchestrator_bot import photo_publication
from tests.test_photo_recovery import event
from tests.test_photo_auditor import picture


@pytest.fixture
def ready(tmp_path):
    state=tmp_path/'state.db';queue=tmp_path/'queue.db';image=tmp_path/'accepted.png'
    image.write_bytes(picture())
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE excel_items(key TEXT,status TEXT,card_job INTEGER,ts REAL)')
        db.execute("INSERT INTO excel_items VALUES('original-item','preview',123,100)")
        db.execute('CREATE TABLE awaiting(key TEXT,status TEXT,card_path TEXT,ts REAL)')
        db.execute("INSERT INTO awaiting VALUES('original-item','pending',?,110)",(str(image),))
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER,mode TEXT,brand TEXT,model TEXT,status TEXT)')
        db.execute("INSERT INTO jobs VALUES(123,'kbt','Midea','ABC-12-RU','done')")
    decision={**event(),'sha256':hashlib.sha256(picture()).hexdigest()}
    return state,queue,image,decision


def test_ready_exact_file_is_eligible_but_inspection_does_not_publish(ready):
    state,queue,_,decision=ready
    result=photo_publication.inspect(state,queue,decision)
    assert result['ready'] and not result['published'] and result['key']=='original-item'
    with sqlite3.connect(state) as db:assert db.execute('SELECT status FROM awaiting').fetchone()[0]=='pending'


@pytest.mark.parametrize('mutation',[
    "UPDATE excel_items SET card_job=124",
    "UPDATE excel_items SET status='cancelled'",
    "UPDATE awaiting SET status='rejected'",
    "UPDATE awaiting SET ts=90",
])
def test_superseded_cancelled_rejected_and_previous_campaigns_cannot_publish(ready,mutation):
    state,queue,_,decision=ready
    with sqlite3.connect(state) as db:db.execute(mutation)
    assert not photo_publication.inspect(state,queue,decision).get('ready')


@pytest.mark.parametrize('mutation',["UPDATE jobs SET status='processing'","UPDATE jobs SET status='pending'"])
def test_publication_request_waits_for_photo_delivery(ready,mutation):
    state,queue,_,decision=ready
    with sqlite3.connect(queue) as db:db.execute(mutation)
    result=photo_publication.inspect(state,queue,decision)
    assert not result['handled'] and not result.get('ready')


@pytest.mark.parametrize('mutation',["UPDATE jobs SET model='OTHER'","UPDATE jobs SET mode='research'"])
def test_other_model_or_source_only_job_cannot_publish(ready,mutation):
    state,queue,_,decision=ready
    with sqlite3.connect(queue) as db:db.execute(mutation)
    with pytest.raises(ValueError):photo_publication.inspect(state,queue,decision)


def test_changed_file_does_not_publish(ready):
    state,queue,image,decision=ready;image.write_bytes(picture('black'))
    result=photo_publication.inspect(state,queue,decision)
    assert result['handled'] and not result.get('ready') and not result['published']


def test_publish_uses_single_exact_pending_card_and_lost_ack_is_idempotent(ready,monkeypatch):
    state,queue,image,decision=ready;calls=[]
    pending=SimpleNamespace(key='original-item',status='pending',card_path=str(image))
    monkeypatch.setattr(photo_publication,'awaiting_item',lambda *_:pending)
    def publish(db,items):
        calls.append(items)
        with sqlite3.connect(db) as connection:connection.execute("UPDATE awaiting SET status='published'")
        return {'published':['original-item'],'failed':[]}
    cf=SimpleNamespace(_paths=lambda _: (state,None,queue),_publish_batch=publish)
    args=SimpleNamespace(audit_json=json.dumps(decision))
    assert photo_publication.run(cf,args)['published']
    assert photo_publication.run(cf,args)['published']
    assert calls==[[pending]]


def test_new_selection_between_inspection_and_send_cannot_publish(ready,monkeypatch):
    state,queue,image,decision=ready
    def awaiting(*_):
        with sqlite3.connect(state) as db:db.execute('UPDATE excel_items SET card_job=124')
        return SimpleNamespace(status='pending',card_path=str(image))
    monkeypatch.setattr(photo_publication,'awaiting_item',awaiting)
    cf=SimpleNamespace(_paths=lambda _: (state,None,queue),_publish_batch=lambda *_:pytest.fail('Superseded'))
    assert not photo_publication.run(cf,SimpleNamespace(audit_json=json.dumps(decision)))['published']
