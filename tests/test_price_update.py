import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from orchestrator_bot import price_update
from orchestrator_bot.factory import Factory

PNG = bytes.fromhex('89504e470d0a1a0a') + b'original reference'
OLD = bytes.fromhex('89504e470d0a1a0a') + b'old immutable card'


@pytest.fixture
def pipeline(tmp_path):
    state, queue, output = tmp_path/'state.db', tmp_path/'queue.db', tmp_path/'output'
    output.mkdir(); (tmp_path/'archive').mkdir(); (tmp_path/'archive'/'source.png').write_bytes(PNG)
    (output/'previous.png').write_bytes(OLD)
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,mode TEXT,brand TEXT,model TEXT,status TEXT,specs TEXT,'
                   'output_filename TEXT,input_filename TEXT,archived_filename TEXT,failed_filename TEXT,request_key TEXT UNIQUE)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (123,'kbt','CARVER','PPG-11000 DR','done','Цена: 116811 рублей. Мощность 116811 Вт.',
                    'previous.png','source.png','source.png',None,None))
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE excel_items(key TEXT PRIMARY KEY,brand TEXT,model TEXT,price INTEGER,status TEXT,'
                   'card_job INTEGER,research_job INTEGER,card_request_key TEXT,error TEXT,tries INTEGER,ts REAL)')
        db.execute('INSERT INTO excel_items VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   ('excel|cohort|dr','CARVER','PPG-11000 DR',116811,'preview',123,None,None,None,0,1))
        db.execute('CREATE TABLE awaiting(key TEXT PRIMARY KEY,status TEXT)')
        db.execute("INSERT INTO awaiting VALUES('excel|cohort|dr','pending')")
    value = dict(request_id='a'*32,job_id=123,brand='CARVER',model='PPG-11000DR',
                 old_price='116811',new_price='116890',actor_id=7)
    return SimpleNamespace(root=tmp_path,state=state,queue=queue,output=output,value=value)


def call(p, submit, **kwargs):
    return price_update.update_price(p.state,p.queue,p.value,submit_card=submit,output_dir=p.output,now=10,**kwargs)


def saved(p):
    with sqlite3.connect(p.state) as db:
        return db.execute('SELECT status,card_job,price FROM excel_items').fetchone(),db.execute('SELECT status FROM awaiting').fetchone()[0]


def accept(p, *, fail_ack=False):
    calls=[]
    def submit(brand,model,specs,source,mode,**kwargs):
        assert saved(p)==(('correcting',123,116811),'correcting')
        assert (brand,model,mode)==('CARVER','PPG-11000 DR','kbt')
        assert Path(source).read_bytes()==PNG
        assert 'Цена: 116890 рублей' in specs
        assert 'Мощность 116811 Вт' in specs
        assert '"selling_price":116890' in specs  # authoritative structured price
        assert 'Наценку повторно не добавляй' in specs
        calls.append(kwargs['request_key'])
        with sqlite3.connect(p.queue) as db:
            db.execute('INSERT INTO jobs(id,mode,brand,model,status,specs,request_key) VALUES(124,?,?,?,?,?,?)',
                       (mode,brand,model,'pending',specs,kwargs['request_key']))
        if fail_ack:
            raise TimeoutError('queue accepted but acknowledgement was lost')
        return 124
    return submit,calls


def test_price_updates_prompt_and_pipeline_metadata_with_new_preview(pipeline):
    submit,calls=accept(pipeline)
    result=call(pipeline,submit)
    assert result['submitted'] and result['linked'] and result['job_id']==124
    assert result['old_job_id']==123 and result['published'] is False
    assert saved(pipeline)==(('card',124,116890),'correcting')
    assert calls==['owner-price-'+'a'*32]
    assert (pipeline.output/'previous.png').read_bytes()==OLD
    folder=pipeline.root/'price-updates'/('a'*32)
    assert (folder/'previous-card').read_bytes()==OLD and (folder/'source.png').read_bytes()==PNG


def test_repeated_confirmation_reuses_result_without_new_render(pipeline):
    submit,calls=accept(pipeline)
    first=call(pipeline,submit); again=call(pipeline,submit)
    assert again['duplicate'] and again['job_id']==first['job_id'] and len(calls)==1
    pipeline.value['new_price']='120000'
    with pytest.raises(ValueError,match='другими данными'):
        call(pipeline,submit)


def test_lost_acceptance_reply_finds_same_queue_job_without_submitting_again(pipeline):
    submit,calls=accept(pipeline,fail_ack=True)
    with pytest.raises(TimeoutError):call(pipeline,submit)
    assert saved(pipeline)==(('correcting',123,116811),'correcting')
    result=call(pipeline,lambda *a,**k:pytest.fail('Accepted request cannot buy another render'))
    assert result['job_id']==124 and len(calls)==1
    assert saved(pipeline)==(('card',124,116890),'correcting')


def test_lost_reply_before_acceptance_retries_same_request_key(pipeline):
    calls=[]
    def unavailable(*a,**kw):calls.append(kw['request_key']);raise TimeoutError
    with pytest.raises(TimeoutError):call(pipeline,unavailable)
    submit,accepted=accept(pipeline)
    assert call(pipeline,submit)['submitted']
    assert calls==accepted


def test_accepted_but_unlinked_retry_reports_existing_job_instead_of_buying_another(pipeline):
    submit,calls=accept(pipeline,fail_ack=True)
    with pytest.raises(TimeoutError):call(pipeline,submit)
    with sqlite3.connect(pipeline.state) as db:db.execute("UPDATE excel_items SET status='cancelled'")
    result=call(pipeline,lambda *a,**k:pytest.fail('Already accepted'))
    assert result['submitted'] and result['job_id']==124 and result['linked'] is False
    assert result['published'] is False and 'уже принято' in result['message']
    assert call(pipeline,lambda *a,**k:pytest.fail('Stored result'))['duplicate']


@pytest.mark.parametrize('statement,reason',[
    ("UPDATE jobs SET status='processing'",'не готова'),
    ("UPDATE jobs SET status='cancelled'",'не готова'),
    ("UPDATE jobs SET model='PPG-11000 DVR'",'другая модель'),
    ("UPDATE jobs SET mode='research'",'не готова')])
def test_inflight_wrong_suffix_and_research_never_submit(pipeline,statement,reason):
    with sqlite3.connect(pipeline.queue) as db:db.execute(statement)
    result=call(pipeline,lambda *a,**k:pytest.fail('No render allowed'))
    assert not result['submitted'] and reason in result['message']
    assert saved(pipeline)==(('preview',123,116811),'pending')


@pytest.mark.parametrize('statement,reason',[
    ("UPDATE awaiting SET status='published'",'опубликовано'),
    ("UPDATE awaiting SET status='rejected'",'снято'),
    ("UPDATE excel_items SET status='published'",'опубликована'),
    ("UPDATE excel_items SET price=99999",'цена изменилась'),
    ("UPDATE excel_items SET card_job=122",'заменена')])
def test_published_stale_price_and_replaced_card_are_rejected(pipeline,statement,reason):
    with sqlite3.connect(pipeline.state) as db:db.execute(statement)
    result=call(pipeline,lambda *a,**k:pytest.fail('No render allowed'))
    assert not result['submitted'] and reason in result['message']


def test_more_recent_same_sku_cohort_prevents_updating_old_preview(pipeline):
    with sqlite3.connect(pipeline.queue) as db:
        db.execute("INSERT INTO jobs(id,mode,brand,model,status) VALUES(124,'kbt','CARVER','PPG-11000DR','done')")
    result=call(pipeline,lambda *a,**k:pytest.fail('Retired preview cannot render'))
    assert not result['submitted'] and 'более новое' in result['message']


def test_different_suffix_newer_job_does_not_reject_dr(pipeline):
    with sqlite3.connect(pipeline.queue) as db:
        db.execute("INSERT INTO jobs(id,mode,brand,model,status) VALUES(122,'kbt','CARVER','PPG-11000DVR','done')")
    submit,calls=accept(pipeline)
    assert call(pipeline,submit)['submitted']


def test_no_original_reference_cannot_use_old_composite_as_input(pipeline):
    (pipeline.root/'archive/source.png').unlink()
    result=call(pipeline,lambda *a,**k:pytest.fail('Composite is not a product reference'))
    assert not result['submitted'] and 'исходного фото' in result['message']
    assert saved(pipeline)==(('preview',123,116811),'pending')


def test_second_request_cannot_overtake_reserved_price_change(pipeline):
    with pytest.raises(TimeoutError):call(pipeline,lambda *a,**k:(_ for _ in ()).throw(TimeoutError()))
    pipeline.value['request_id']='b'*32
    result=call(pipeline,lambda *a,**k:pytest.fail('Pending request cannot be duplicated'))
    assert not result['submitted']


def test_tampered_frozen_reference_blocks_retry(pipeline):
    with pytest.raises(TimeoutError):call(pipeline,lambda *a,**k:(_ for _ in ()).throw(TimeoutError()))
    (pipeline.root/'price-updates'/('a'*32)/'source.png').write_bytes(PNG+b'changed')
    result=call(pipeline,lambda *a,**k:pytest.fail('Changed source'))
    assert not result['submitted'] and 'изменился' in result['message']


def test_json_price_fields_are_replaced_without_touching_features(pipeline):
    specs=price_update.specifications(json.dumps({'price':116811,'selling_price':116811,'cost_price':100000,
                                                'features':['116811 Вт']}),pipeline.value)
    assert '"price":116890' in specs and '"selling_price":116890' in specs
    assert '116811 Вт' in specs
    assert '"cost_price":100000' in specs


def test_display_price_replacement_keeps_source_cost_field(pipeline):
    specs=price_update.specifications('price: 116811 руб.\ncost_price: 100000\nМощность 116811 Вт',pipeline.value)
    assert 'price: 116890 руб.' in specs and 'cost_price: 100000' in specs


@pytest.mark.parametrize('field,value',[('old_price',0),('new_price','NaN'),('new_price',True),('new_price','123.45'),
                                        ('actor_id',0),('request_id','arbitrary')])
def test_invalid_owner_request_rejected(pipeline,field,value):
    pipeline.value[field]=value
    with pytest.raises(ValueError):call(pipeline,lambda *a,**k:pytest.fail('Invalid request'))


def test_factory_routes_price_command_through_project_adapter():
    seen=[]
    def runner(cmd,**kw):seen.append((cmd,kw));return SimpleNamespace(returncode=0,stdout='{"submitted":false}',stderr='')
    factory=Factory('python','/factory',runner=runner)
    factory.update_card_price({'request_id':'a'*32})
    assert seen[0][0][2]=='orchestrator_bot.catalog_cli'
    assert seen[0][0][3]=='update-card-price'
