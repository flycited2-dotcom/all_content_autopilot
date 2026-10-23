from types import SimpleNamespace
import time

import pytest

from listener.intent_validation import validated_intent
from orchestrator_bot.catalog_cli import hint
from orchestrator_bot.catalog_selection import select_rows, model_name
from orchestrator_bot import selection_ui as ui
from orchestrator_bot.store import Store


def rows(n=12):
    return [{'key': f'k{i}', 'name': f'Холодильник {i:02}',
             'brand': 'Beko' if i < n - 2 else 'LG', 'source': 'supplier-a' if i % 2 else 'supplier-b'}
            for i in range(n)]


def test_full_brand_inventory_does_not_drop_brand_61_or_xiaomi():
    items = [SimpleNamespace(brand=f'Brand-{n}', section='Холодильники') for n in range(75)]
    items.append(SimpleNamespace(brand='Xiaomi', section='Телевизоры'))
    cf = SimpleNamespace(_paths=lambda args: ('db', 'prices', ''),
                         _items=lambda directory: ([('supplier', items)], [('supplier', i) for i in items]))
    data = hint(cf, None)
    assert len(data['brands']) == 76 and 'Xiaomi' in data['brands']
    assert data['brands_complete'] is True and data['source_counts']['supplier'] == 76


def test_automatic_selection_is_balanced_exact_and_reproducible():
    selected = select_rows(rows(), mode='auto', count=4)
    assert len(selected) == 4 and len({r['key'] for r in selected}) == 4
    assert {r['brand'] for r in selected} == {'LG', 'Beko'}
    assert {r['source'] for r in selected} == {'supplier-a', 'supplier-b'}
    assert selected == select_rows(list(reversed(rows())), mode='auto', count=4)


def test_brand_filter_never_fills_shortage_with_other_brands():
    selected = select_rows(rows(), count=7, quotas={'lg': 7}, mode='auto')
    assert len(selected) == 2 and all(r['brand'] == 'LG' for r in selected)


def test_same_model_in_two_sources_keeps_first_source_and_catalog_order():
    original = rows(3)
    duplicate = {**original[0], 'source': 'later-supplier'}
    selected = select_rows(original + [duplicate], mode='first')
    assert selected == original


def test_manual_keys_preserve_order_and_reject_stock_change():
    assert [r['key'] for r in select_rows(rows(), keys=['k7', 'k1'])] == ['k7', 'k1']
    with pytest.raises(ValueError, match='недоступна'):
        select_rows(rows(), keys=['unknown'])


def test_supplier_color_description_is_not_mistaken_for_part_of_exact_sku():
    assert model_name('C4F740CDBGU1 графитовый') == 'C4F740CDBGU1'
    assert model_name('АСН-10000/1-Ц') == 'АСН-10000/1-Ц'


def test_product_selection_is_validated_without_allowing_model_supplied_keys():
    action = {'type': 'enqueue_products', 'category': 'холодильники', 'count': 7,
              'selection': 'auto', 'quotas': {'beko': 7}}
    assert validated_intent({'reply': 'Понял', 'actions': [action]})['actions'][0]['count'] == 7
    with pytest.raises(ValueError):
        validated_intent({'reply': '', 'actions': [{**action, 'keys': ['k1']}]})


@pytest.fixture
def deps(tmp_path):
    class Factory:
        def __init__(self):
            self.enqueued = []
            self.fail = False

        def resolve(self, action):
            return {'items': rows(), 'total_matches': 12}

        def enqueue(self, action):
            self.enqueued.append(action)
            if self.fail:
                raise ValueError('stock changed')
            return {'accepted': action['keys'], 'items': [{'name': k} for k in action['keys']]}

    sent, edits = [], []
    return SimpleNamespace(store=Store(tmp_path / 'picker.db'), factory=Factory(), clock=time.time,
                           tg=SimpleNamespace(send=lambda *args: sent.append(args),
                                              edit=lambda *args: edits.append(args)),
                           sent=sent, edits=edits)


def open_picker(deps):
    text, keyboard = ui.open_selection(deps.store, deps.factory, None,
                                      {'type': 'enqueue_products', 'category': 'холодильники',
                                       'count': None, 'quotas': {}, 'selection': 'choose'})
    assert '12 моделей' in text
    return ui.Sessions(deps.store).get(1), keyboard


def callback(deps, session, operation, argument=0):
    ui.handle_callback(deps, 1, {'data': ui.button(session, '', operation, argument)['callback_data'],
                                'message': {'message_id': 100}})


def test_manual_picker_survives_reopen_and_double_click_cannot_enqueue_twice(deps):
    session, _ = open_picker(deps)
    callback(deps, session, 'manual')
    session = ui.Sessions(deps.store).get(1)
    callback(deps, session, 'toggle', 7)
    session = ui.Sessions(deps.store).get(1)
    assert session['selected'] == ['k7']
    callback(deps, session, 'run')
    callback(deps, session, 'run')
    assert len(deps.factory.enqueued) == 1
    assert deps.factory.enqueued[0]['keys'] == ['k7']
    assert ui.Sessions(deps.store).get(1)['state'] == 'done'


def test_user_quantity_is_honored_without_model_or_second_question(deps):
    session, _ = open_picker(deps)
    callback(deps, session, 'count')
    assert ui.handle_count(deps, 1, 123, '7 штук')
    assert len(deps.factory.enqueued[0]['keys']) == 7
    assert deps.factory.enqueued[0]['count'] == 7
    assert deps.store.has_tg_message(123)


def test_expired_or_stale_buttons_cannot_start_generation(deps):
    session, _ = open_picker(deps)
    callback(deps, session, 'manual')
    callback(deps, session, 'all')
    assert not deps.factory.enqueued
    fresh = ui.Sessions(deps.store).get(1)
    deps.clock = lambda: fresh['expires'] + 1
    callback(deps, fresh, 'all')
    assert not deps.factory.enqueued


def test_unknown_enqueue_result_is_held_without_blind_retry(deps):
    session, _ = open_picker(deps)
    deps.factory.fail = True
    callback(deps, session, 'auto', 5)
    callback(deps, session, 'auto', 5)
    assert len(deps.factory.enqueued) == 1
    assert ui.Sessions(deps.store).get(1)['state'] == 'held'
