import sqlite3
from pathlib import Path

import pytest

from orchestrator_bot.supplier_inventory import lookup, normalize_inventory, render, validate_selection


def inventory():
    data = [('LIFAN', '6 GF-3 (LF7000)', '55665', 2), ('CARVER', 'PPG-5000D', '45455', 149),
            ('CARVER', 'PPG-5000DE', '52200', 63), ('CARVER', 'PPG-5000ID', '50720', 6),
            ('CARVER', 'PPG-7000DE', '63910', None), ('CARVER', 'PPG-7000IDE', '71535', None),
            ('CARVER', 'PPG-11000 DR', '101575', 23), ('CARVER', 'PPG-11000 DVR', '107625', 10)]
    return {'rows': [dict(name=brand + ' ' + model, brand=brand, model=model, cost_price=price, stock=stock,
                          attachment_index=0, row_index=n) for n, (brand, model, price, stock) in enumerate(data, 1)],
            'warnings': [], 'complete': True, 'image_row_counts': [{'attachment_index': 0, 'row_count': len(data)}]}


@pytest.fixture
def dbs(tmp_path):
    state, queue = tmp_path / 'state.db', tmp_path / 'queue.db'
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE excel_items (key TEXT,status TEXT,card_job INTEGER,research_job INTEGER,error TEXT,ts REAL)')
        db.execute('CREATE TABLE awaiting (key TEXT,status TEXT,ts REAL)')
        db.executemany('INSERT INTO excel_items VALUES(?,?,?,?,?,?)', [
            ('excel|carver|ppg-5000d', 'preview', 3851, None, None, 10),
            ('excel|carver|ppg-11000 dr', 'failed', None, 3855, 'PHOTO_AUDIT_HOLD', 10),
            ('excel|carver|ppg-11000 dvr', 'failed', None, 3856, 'PHOTO_AUDIT_HOLD', 10)])
        db.execute("INSERT INTO awaiting VALUES('excel|carver|ppg-5000d','published',11)")
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER,brand TEXT,model TEXT,status TEXT,mode TEXT,error_text TEXT,result_sent INTEGER)')
        db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', [
            (3851, 'CARVER', 'PPG-5000D', 'done', 'kbt', None, 1),
            (3855, 'CARVER', 'PPG-11000', 'failed', 'research', 'SOURCE_UNAVAILABLE', 0),
            (3856, 'CARVER', 'PPG-11000', 'failed', 'research', 'SOURCE_UNAVAILABLE', 0),
            (3900, 'CARVER', 'PPG-5000ID', 'processing', 'kbt', None, 0)])
    return state, queue


def test_photo_full_inventory_survives_brand_filter_and_prior_exclusions(dbs):
    inv = inventory();states = lookup(*dbs, inv['rows'])
    text = render(inv, states, filter_brand='CARVER', excluded_models=('PPG-7000DE', 'PPG-7000IDE'), markup_percent=15)
    assert len(states) == 8
    for row in inv['rows']:
        assert row['model'] in text
    assert 'LIFAN 6 GF-3 (LF7000)' in text and 'вне отбора CARVER' in text
    assert 'Положительный остаток: 6; нулевой: 0; не указан/не читается: 2' in text
    assert '55 665 ₽' in text and '107 625 ₽' in text
    assert text.count('не ставить в генерацию до уточнения остатка') == 2
    assert states[1]['state'] == 'published'
    assert states[3]['state'] == 'processing'
    assert states[6]['state'] == states[7]['state'] == 'failed'
    assert states[6]['job_id'] == 3855 and states[7]['job_id'] == 3856
    assert text.count('ошибка поиска исходника') == 2
    assert 'уже обработаны' not in text


def test_stale_publication_from_old_selection_is_not_current(dbs):
    with sqlite3.connect(dbs[0]) as db:
        db.execute("UPDATE awaiting SET ts=1")
    states = lookup(*dbs, inventory()['rows'])
    assert states[1]['state'] == 'ready'


def test_unknown_stock_is_distinct_from_zero_and_is_not_removed(dbs):
    inv = inventory();inv['rows'][0]['stock'] = 0
    clean = normalize_inventory(inv)
    assert clean['rows'][0]['stock'] == 0 and clean['rows'][4]['stock'] is None
    text = render(inv, lookup(*dbs, inv['rows']))
    assert 'нулевой: 1' in text and 'не указан/не читается: 2' in text
    assert 'LIFAN' in text and 'на фото указан нулевой остаток' in text


def test_lookup_is_readonly_and_never_creates_missing_database(dbs, tmp_path):
    before = [path.read_bytes() for path in dbs]
    lookup(*dbs, inventory()['rows'])
    assert [path.read_bytes() for path in dbs] == before
    missing = tmp_path / 'no.db'
    states = lookup(missing, missing, inventory()['rows'])
    assert all(state['state'] == 'unavailable' for state in states) and not missing.exists()


def test_standalone_done_job_is_not_claimed_published_and_suffixes_do_not_merge(dbs):
    with sqlite3.connect(dbs[1]) as db:
        db.execute("INSERT INTO jobs VALUES(4000,'LIFAN','6 GF-3 (LF7000)','done','kbt',NULL,1)")
        db.execute("INSERT INTO jobs VALUES(4001,'CARVER','PPG-11000 D','done','kbt',NULL,1)")
    states = lookup(*dbs, inventory()['rows'])
    assert states[0]['state'] == 'ready' and states[0]['origin'] == 'queue'
    assert states[6]['job_id'] == 3855 and states[7]['job_id'] == 3856
    assert 'карточка готова и отправлена' in render(inventory(), states)


@pytest.mark.parametrize('field,value', [('stock', False), ('stock', -1), ('cost_price', '0'), ('row_index', -1)])
def test_invalid_transcription_rejected_without_guessed_facts(field, value):
    inv = inventory();inv['rows'][0][field] = value
    with pytest.raises(ValueError):
        normalize_inventory(inv)


def test_partial_row_and_disagreement_remain_visible(dbs):
    inv = inventory()
    inv['complete'] = False
    inv['rows'][0].update(name=None, brand=None, model=None, cost_price=None, stock=None)
    inv['warnings'] = ['Не совпала первая строка.']
    clean = normalize_inventory(inv)
    assert clean['complete'] is False and clean['rows'][0]['brand'] is None
    assert clean['image_row_counts'] == [{'attachment_index': 0, 'row_count': 8}]
    states = lookup(*dbs, clean['rows'])
    assert states[0]['state'] == 'unreadable' and states[0]['key'] is None
    text = render(inv, states)
    assert 'позиций: 8' in text
    assert 'Полнота разбора не подтверждена' in text
    assert 'Название не прочитано' in text and 'бренд или модель требуют уточнения' in text
    assert 'Не совпала первая строка.' in text
    assert 'None' not in text


@pytest.mark.parametrize('change', [
    lambda inv: inv['rows'][0].update(row_index=0),
    lambda inv: inv['rows'].pop(0),
    lambda inv: inv['image_row_counts'][0].update(row_count=9),
    lambda inv: inv.update(complete='true'),
    lambda inv: inv.update(source_sha256=['wrong']),
    lambda inv: inv.update(vision_passes=1),
])
def test_incomplete_coverage_and_fake_proof_rejected(change):
    inv = inventory()
    change(inv)
    with pytest.raises(ValueError):
        normalize_inventory(inv)


def test_verified_source_metadata_preserved():
    inv = inventory()
    inv.update(source_sha256=['a' * 64], vision_passes=2)
    clean = normalize_inventory(inv)
    assert clean['source_sha256'] == inv['source_sha256'] and clean['vision_passes'] == 2


def test_latest_exact_queue_match_and_unrelated_history_are_ignored(dbs):
    with sqlite3.connect(dbs[1]) as db:
        db.execute('ALTER TABLE jobs ADD COLUMN giant_prompt TEXT')
        db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)', [
            (4100, ' CARVER ', ' PPG-5000ID ', 'failed', 'kbt', 'old failure', 0, 'x' * 100000),
            (4101, 'CARVER', 'PPG-5000ID', 'pending', 'kbt', None, 0, 'x' * 100000),
            (4102, 'CARVER', 'PPG-5000IDE', 'done', 'kbt', None, 0, 'x' * 100000)])
    states = lookup(*dbs, inventory()['rows'])
    assert states[3]['state'] == 'queued' and states[3]['job_id'] == 4101
    assert 'giant_prompt' not in states[3]


def test_each_lookup_reads_current_status_instead_of_caching(dbs):
    before = lookup(*dbs, inventory()['rows'])
    with sqlite3.connect(dbs[1]) as db:
        db.execute("UPDATE jobs SET status='failed',error_text='source gone' WHERE id=3900")
    after = lookup(*dbs, inventory()['rows'])
    assert before[3]['state'] == 'processing' and after[3]['state'] == 'failed'


def selected_action(inv):
    return {'type': 'enqueue_supplier_products', 'category': 'генераторы', 'markup_percent': '15',
            'items': [{key: value for key, value in row.items() if key != 'row_index'}
                      for row in inv['rows'] if row['stock'] is not None and row['stock'] > 0]}


def test_selection_requires_verified_fact_match_and_returns_original():
    inv = inventory()
    action = selected_action(inv)
    action['items'][0].update(cost_price='55665.00', name='LIFAN   6 GF-3 (LF7000)')
    assert validate_selection(action, inv) is action


@pytest.mark.parametrize('field,value', [
    ('brand', 'another brand'), ('model', 'PPG-11000 D'), ('cost_price', '55664.99'),
    ('stock', 1), ('stock', 0), ('stock', False), ('attachment_index', 1),
    ('name', 'Генератор бензиновый CARVER PPG-5000D'),
])
def test_selection_cannot_change_source_fields(field, value):
    inv = inventory()
    action = selected_action(inv)
    action['items'][0][field] = value
    with pytest.raises(ValueError):
        validate_selection(action, inv)


def test_unknown_quantity_or_unverified_inventory_cannot_dispatch():
    inv = inventory()
    action = selected_action(inv)
    inv['rows'][0]['stock'] = None
    with pytest.raises(ValueError):
        validate_selection(action, inv)
    inv = inventory()
    inv['complete'] = False
    with pytest.raises(ValueError):
        validate_selection(action, inv)


def test_every_selection_item_is_checked():
    inv = inventory()
    action = selected_action(inv)
    action['items'][-1]['model'] = 'PPG-11000 DR'
    with pytest.raises(ValueError):
        validate_selection(action, inv)


def test_unknown_title_does_not_hide_readable_identity_and_price():
    inv = inventory()
    action = selected_action(inv)
    inv['rows'][0]['name'] = None
    assert validate_selection(action, inv) is action
    inv['rows'][0]['cost_price'] = None
    with pytest.raises(ValueError):
        validate_selection(action, inv)
