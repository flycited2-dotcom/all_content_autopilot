"""A saved-card price must belong to the selected exact-SKU card job."""
import sqlite3

import pytest

from orchestrator_bot.supplier_inventory import lookup


def source(model='PPG-5000D'):
    return [dict(name='CARVER ' + model, brand='CARVER', model=model, cost_price='60000', stock=5,
                 attachment_index=0, row_index=1)]


@pytest.fixture
def databases(tmp_path):
    state, queue = tmp_path / 'state.db', tmp_path / 'queue.db'
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE excel_items (key TEXT,brand TEXT,model TEXT,price INTEGER,status TEXT,'
                   'card_job INTEGER,research_job INTEGER,error TEXT,ts REAL)')
        db.execute('CREATE TABLE awaiting (key TEXT,status TEXT,ts REAL)')
        db.execute("INSERT INTO excel_items VALUES('excel|carver|ppg-5000d','CARVER','PPG-5000D',10000,'published',10,NULL,NULL,1)")
        db.execute("INSERT INTO excel_items VALUES('excel|cohort|2','CARVER','PPG-5000D',52273,'preview',20,19,NULL,2)")
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER,brand TEXT,model TEXT,status TEXT,mode TEXT,error_text TEXT,'
                   'result_sent INTEGER,created_at TEXT,updated_at TEXT)')
        db.execute("INSERT INTO jobs VALUES(10,'CARVER','PPG-5000D','done','kbt',NULL,1,'2026-10-01 09:00:00','2026-10-01T12:10:00')")
        db.execute("INSERT INTO jobs VALUES(20,'CARVER','PPG-5000D','done','kbt',NULL,1,'2026-10-30 16:22:03','2026-10-30T19:37:22.397380')")
    return state, queue


def test_latest_cohort_price_uses_exact_card_link_never_old_canonical_price(databases):
    status, = lookup(*databases, source())
    assert status['job_id'] == 20 and status['state'] == 'ready'
    assert status['card_price'] == '52273'
    assert status['card_price_source'] == 'excel_items.card_job'
    assert status['card_price_verified'] is False
    assert status['card_created_at'] == '2026-10-30T16:22:03+00:00'


def test_canonical_published_card_also_has_linked_price(databases):
    with sqlite3.connect(databases[1]) as db:
        db.execute('DELETE FROM jobs WHERE id=20')
    with sqlite3.connect(databases[0]) as db:
        db.execute("DELETE FROM excel_items WHERE card_job=20")
    status, = lookup(*databases, source())
    assert status['state'] == 'published' and status['card_price'] == '10000'


def test_missing_new_price_never_falls_back_to_old_selection(databases):
    with sqlite3.connect(databases[0]) as db:
        db.execute('DELETE FROM excel_items WHERE card_job=20')
    status, = lookup(*databases, source())
    assert status['job_id'] == 20 and status['card_price'] is None
    assert status['card_price_verified'] is False


@pytest.mark.parametrize('price', [None, -1, 0, 'NaN', 'Infinity', 'not-a-price'])
def test_unknown_or_invalid_saved_price_is_not_presented_as_verified(databases, price):
    with sqlite3.connect(databases[0]) as db:
        db.execute('UPDATE excel_items SET price=? WHERE card_job=20', (price,))
    status, = lookup(*databases, source())
    assert status['card_price'] is None and status['card_price_source'] is None


def test_conflicting_exact_card_link_prices_require_manual_check(databases):
    with sqlite3.connect(databases[0]) as db:
        db.execute("INSERT INTO excel_items VALUES('excel|conflicting|2','CARVER','PPG-5000D',60000,'preview',20,NULL,NULL,2)")
    status, = lookup(*databases, source())
    assert status['card_price'] is None


def test_different_model_cannot_supply_saved_price_for_same_job_id(databases):
    with sqlite3.connect(databases[0]) as db:
        db.execute("UPDATE excel_items SET model='PPG-5000DE' WHERE card_job=20")
    status, = lookup(*databases, source())
    assert status['card_price'] is None


def test_dr_and_dvr_cards_keep_separate_price_identity(databases):
    with sqlite3.connect(databases[1]) as db:
        db.execute("INSERT INTO jobs VALUES(21,'CARVER','PPG-11000 DR','done','kbt',NULL,1,'2026-10-30 12:00:00',NULL)")
        db.execute("INSERT INTO jobs VALUES(22,'CARVER','PPG-11000 DVR','done','kbt',NULL,1,'2026-10-30 13:00:00',NULL)")
    with sqlite3.connect(databases[0]) as db:
        db.execute("INSERT INTO excel_items VALUES('excel|cohort|dr','CARVER','PPG-11000 DR',116811,'preview',21,NULL,NULL,2)")
        db.execute("INSERT INTO excel_items VALUES('excel|cohort|dvr','CARVER','PPG-11000 DVR',123769,'preview',22,NULL,NULL,2)")
    dr, = lookup(*databases, source('PPG-11000 DR'))
    dvr, = lookup(*databases, source('PPG-11000 DVR'))
    assert (dr['job_id'], dr['card_price']) == (21, '116811')
    assert (dvr['job_id'], dvr['card_price']) == (22, '123769')
    compact_dr, = lookup(*databases, source('PPG-11000DR'))
    compact_dvr, = lookup(*databases, source('PPG-11000DVR'))
    assert (compact_dr['job_id'], compact_dr['card_price']) == (21, '116811')
    assert (compact_dvr['job_id'], compact_dvr['card_price']) == (22, '123769')
    assert compact_dr['key'] == 'excel|carver|ppg-11000dr'  # storage identities stay unchanged
    no_suffix, = lookup(*databases, source('PPG-11000'))
    assert no_suffix['state'] == 'missing' and 'card_price' not in no_suffix


@pytest.mark.parametrize('variant', ['PPG-5000DE', 'PPG-5000ID', 'PPG-5000IDE', 'PPG-5000DR', 'PPG-5000DVR'])
def test_suffix_characters_are_never_removed_or_merged(databases, variant):
    status, = lookup(*databases, source(variant))
    assert status['state'] == 'missing' and 'card_price' not in status


def test_new_research_does_not_expose_previous_card_price(databases):
    with sqlite3.connect(databases[1]) as db:
        db.execute("INSERT INTO jobs VALUES(21,'CARVER','PPG-5000D','done','research',NULL,1,'2026-10-30 18:00:00',NULL)")
    status, = lookup(*databases, source())
    assert status['state'] == 'research_done' and 'card_price' not in status


def test_readonly_lookup_leaves_source_databases_unchanged(databases):
    before = [path.read_bytes() for path in databases]
    lookup(*databases, source())
    assert before == [path.read_bytes() for path in databases]
