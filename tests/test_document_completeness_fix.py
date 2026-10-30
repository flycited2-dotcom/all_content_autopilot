"""Stable missing fields cannot stop known rows in an otherwise counted file."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from listener import supplier_documents, supplier_vision
from listener.routing import ModelRouter


def payload(names=None, caption=None):
    rows = names or [('Генератор CARVER PPG-5000D', 149, 52290),
                     ('Блок автопуска для БЭГ 7000E, 8500E, 11000E', 2, 13090),
                     ('Генератор CARVER PPG-5000DE', 63, 60090)]
    lines = ['Лист: Прайс', 'Строка 1: ' + json.dumps(['№', 'Номенклатура', 'Количество', 'Цена, ₽'], ensure_ascii=False)]
    lines += [f'Строка {n+1}: ' + json.dumps([n, *[name, stock, price]], ensure_ascii=False)
              for n, (name, stock, price) in enumerate(rows, 1)]
    text = '\n'.join(lines)
    return {'message': {'id': 1, 'kind': 'document', 'text': caption or
            'Разбери прайс и поставь в генерацию, цена уже указана в столбце, добавлять проценты не надо',
            'document': {'filename': 'price.xlsx', 'text': text, 'metadata': {}, 'sha256': hashlib.sha256(text.encode()).hexdigest()}},
            'context': {'capabilities': ['supplier_photo_inventory']}}


def transcription(item):
    source = supplier_documents.structured_rows(item)
    rows = []
    for n, row in enumerate(source, 1):
        name = row['name']
        brand = 'CARVER' if 'CARVER' in name else None
        model = name.split('CARVER ', 1)[1] if brand else None
        rows.append(dict(name=name, brand=brand, model=model, cost_price=row['cost_price'], stock=row['stock'],
                         attachment_index=0, row_index=n))
    return {'rows': rows, 'warnings': [], 'image_row_counts': [{'attachment_index': 0, 'row_count': len(rows)}]}


def test_both_readers_missing_brand_does_not_mean_missing_source_row():
    item = payload();raw=transcription(item)
    result=supplier_vision.reconcile(raw,deepcopy(raw))
    assert result['complete'] and len(result['rows']) == 3
    assert result['rows'][1]['brand'] is None


def test_wholly_unreadable_row_and_true_disagreement_still_block_source():
    raw=transcription(payload()); other=deepcopy(raw)
    other['rows'][0]['stock']=150
    assert supplier_vision.reconcile(raw,other)['complete'] is False
    for field in ('name','brand','model','cost_price','stock'):
        raw['rows'][1][field]=None
    assert supplier_vision.reconcile(raw,deepcopy(raw))['complete'] is False


def test_structured_numeric_cells_and_exact_names_beat_model_retyping():
    item=payload();calls=[]
    def read(*args):
        calls.append(args)
        raw=transcription(item)
        raw['rows'][0].update(name='CARVER PPG-5000D',stock=1,cost_price='99999')
        if len(calls)==2:
            raw['rows'][0].update(stock=99,cost_price='1')
        return raw
    result=supplier_documents.extract(item,read)
    assert result['complete']
    assert result['rows'][0]['stock']==149 and result['rows'][0]['cost_price']=='52290'
    assert result['rows'][0]['name']=='Генератор CARVER PPG-5000D'


def test_brand_from_memory_cannot_replace_absent_source_brand():
    item=payload()
    def read(*args):
        raw=transcription(item)
        raw['rows'][1].update(brand='CARVER',model='БЭГ 7000E')
        return raw
    result=supplier_documents.extract(item,read)
    assert result['rows'][1]['brand'] is None and result['complete']


def test_structured_reader_omission_is_rejected_instead_of_hidden():
    item=payload();raw=transcription(item);raw['rows'].pop();raw['image_row_counts'][0]['row_count']=2
    with pytest.raises(supplier_vision.IncompletePhoto,match='все строки'):
        supplier_documents.extract(item,lambda *_: deepcopy(raw))


def test_duplicate_price_headers_do_not_select_arbitrary_price_column():
    item=payload();item['message']['document']['text']='Строка 1: ["Наименование","Цена","Цена","Количество"]\nСтрока 2: ["CARVER PPG-5000D",100,999,1]'
    assert supplier_documents.structured_rows(item) is None


def test_generic_file_order_dispatches_known_rows_and_asks_only_for_missing_row(tmp_path):
    item=payload();read=[];decisions=[]
    router=ModelRouter(tmp_path,claude=lambda *_: decisions.append(True),
                       chatgpt=SimpleNamespace(ready=lambda:False),
                       vision_complete=lambda *args: read.append(args) or transcription(item))
    intent=router.run_turn(item,('session',False))
    assert len(read)==2 and decisions==[]
    inspection,enqueue,ask=intent['actions']
    assert inspection['inventory']['complete'] and len(inspection['inventory']['rows'])==3
    assert enqueue['markup_percent']=='0' and len(enqueue['items'])==2
    assert all(row['brand']=='CARVER' for row in enqueue['items'])
    assert 'Блок автопуска' in ask['question']


def test_conflicting_prices_exclude_only_that_sku_without_stopping_others(tmp_path):
    item=payload([('Генератор CARVER PPG-11000DR',20,116690),
                  ('Генератор CARVER PPG-11000DR',23,116890),
                  ('Генератор CARVER PPG-5000D',149,52290)])
    router=ModelRouter(tmp_path,claude=lambda *_: pytest.fail('Generic final-price file does not need another decision'),
                      chatgpt=SimpleNamespace(ready=lambda:False),vision_complete=lambda *_: transcription(item))
    result=router.run_turn(item,('session',False))
    assert [a['type'] for a in result['actions']]==['inspect_supplier_photo','enqueue_supplier_products','ask']
    assert [row['model'] for row in result['actions'][1]['items']]==['PPG-5000D']
    assert 'PPG-11000DR' in result['actions'][2]['question']


def test_all_conflicting_rows_still_offer_a_concrete_next_step():
    item=payload([('Генератор CARVER PPG-11000DR',20,116690),('Генератор CARVER PPG-11000DR',23,116890)])
    inv=supplier_documents.extract(item,lambda *_: transcription(item))
    action=ModelRouter._generic_document_dispatch(item['message'],inv)
    result=ModelRouter._inventory_intent({'reply':'','actions':[action]},inv,item['message'])
    assert [a['type'] for a in result['actions']]==['inspect_supplier_photo','ask']


@pytest.mark.parametrize('caption', [
    'Поставь генераторы в генерацию, добавлять проценты не надо',
    'Поставь CARVER в генерацию, добавлять проценты не надо',
    'Поставь первые 2 в генерацию, добавлять проценты не надо',
    'Разбери прайс, цена конечная',
    'Поставь в генерацию с наценкой 15%',
])
def test_specific_scope_or_no_generation_never_expands_to_generic_file_order(caption):
    item=payload(caption=caption);raw=transcription(item);inv=supplier_vision.reconcile(raw,deepcopy(raw))
    assert ModelRouter._generic_document_dispatch(item['message'],inv) is None


def test_explicit_no_markup_overrides_accidentally_repeated_old_markup():
    item=payload();raw=transcription(item);inv=supplier_vision.reconcile(raw,deepcopy(raw))
    action=ModelRouter._generic_document_dispatch(item['message'],inv);action['markup_percent']='15'
    result=ModelRouter._inventory_intent({'reply':'','actions':[action]},inv,item['message'])
    assert result['actions'][1]['markup_percent']=='0'


def test_forty_positions_with_one_unknown_brand_keep_all_rows_visible_and_run_others(tmp_path):
    names=[(f'Генератор CARVER PPG-{n}D',n,10000+n) for n in range(1,40)]
    names.insert(20,('Блок автопуска для БЭГ 7000E',2,13090));item=payload(names)
    router=ModelRouter(tmp_path,claude=lambda *_:pytest.fail('No extra question for final price'),
                       chatgpt=SimpleNamespace(ready=lambda:False),vision_complete=lambda *_:transcription(item))
    result=router.run_turn(item,('session',False))
    assert len(result['actions'][0]['inventory']['rows'])==40
    assert result['actions'][0]['inventory']['complete']
    assert len(result['actions'][1]['items'])==39
    assert result['actions'][2]['type']=='ask'
