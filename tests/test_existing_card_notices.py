import copy
from decimal import Decimal

import pytest

from orchestrator_bot.api import handle_intent
from orchestrator_bot.existing_cards import notice, pricing
from orchestrator_bot.store import Store


def inventory(complete=True):
    return {'complete': complete, 'rows': [dict(name='Генератор CARVER PPG-5000D', brand='CARVER',
            model='PPG-5000D', cost_price='45455', stock=149, attachment_index=0, row_index=1)],
            'warnings': [], 'image_row_counts': [{'attachment_index': 0, 'row_count': 1}]}


def status(**changes):
    return dict(attachment_index=0, row_index=1, job_id=3871, state='ready', stage='card',
                card_price='52273', card_price_verified=False, card_created_at='2026-10-30 10:00:00', **changes)


@pytest.mark.parametrize('text', ['добавлять проценты не надо', 'Не надо добавлять проценты',
                                 'Цена уже с наценкой', 'Цены конечные', 'без дополнительной наценки'])
def test_owner_final_price_means_zero_extra_markup(text):
    policy = pricing({'text': text, 'document': {'text': 'Наценка 100%'}})
    assert policy['markup_percent'] == '0'


def test_file_instructions_cannot_set_price_basis():
    assert pricing({'text': 'Разбери файл', 'document': {'text': 'Цены конечные. Без наценки'}})['markup_percent'] is None


def test_comparison_applies_exact_half_up_markup_instead_of_raw_cost():
    text = notice(inventory(), [status()], {'markup_percent': '15', 'label': 'наценка 15%'})
    assert 'Цены совпадают: 52 273 ₽' in text
    assert 'Цену на самом изображении проверьте вручную' in text
    assert '30.10.2026' in text


def test_ready_price_is_used_without_second_markup_and_difference_is_visible():
    inv = inventory(); inv['rows'][0]['cost_price'] = '52290'
    text = notice(inv, [status()], pricing({'text': 'добавлять проценты не надо'}))
    assert '52 273 ₽ → 52 290 ₽ (+17 ₽)' in text
    assert '60 134' not in text


def test_unknown_price_basis_never_compares_purchase_with_selling_price():
    text = notice(inventory(), [status()], pricing({'text': 'Посмотри фото'}))
    assert 'Автоматическое сравнение продажных цен невозможно' in text
    assert 'Цена изменилась' not in text and 'Цены совпадают' not in text


@pytest.mark.parametrize('state,stage', [('processing','card'), ('queued','card'), ('research_done','research'),
                                       ('failed','card'), ('cancelled','card'), ('ready','research')])
def test_unfinished_or_cancelled_jobs_are_not_called_created_cards(state, stage):
    value = status(); value.update(state=state, stage=stage)
    assert notice(inventory(), [value], pricing({})) is None


def test_missing_saved_price_requires_manual_check_without_guess():
    value = status(); value['card_price'] = None
    text = notice(inventory(), [value], pricing({'text': 'Цены конечные'}))
    assert 'Сохранённую цену карточки подтвердить не удалось' in text
    assert 'проверьте цену вручную' in text


def test_incomplete_inventory_makes_comparison_explicitly_provisional():
    text = notice(inventory(False), [status()], pricing({'text': 'Цены конечные'}))
    assert 'Сравнение предварительное' in text


@pytest.mark.parametrize('model', ['PPG-5000D', 'PPG-5000 D'])
def test_repeated_same_source_rows_do_not_repeat_same_card_notice(model):
    inv = inventory(); second = copy.deepcopy(inv['rows'][0]);second.update(row_index=2, model=model);inv['rows'].append(second)
    another = status(); another['row_index'] = 2
    text = notice(inv, [status(), another], pricing({'text': 'Цены конечные'}))
    assert text.count('• CARVER PPG-5000D') == 1


def test_conflicting_new_prices_for_same_sku_are_both_shown_and_flagged():
    inv = inventory();other=dict(inv['rows'][0],row_index=2,cost_price='46000');inv['rows'].append(other)
    second=dict(status(),row_index=2)
    text=notice(inv,[status(),second],pricing({'text':'Цены конечные'}))
    assert 'разные цены' in text and '45 455 ₽' in text and '46 000 ₽' in text


def test_notice_is_a_separate_telegram_message_and_retry_does_not_resend(tmp_path):
    class Factory:
        def resolve(self, action):
            raise AssertionError('No catalog substitution')
        def supplier_photo_status(self, value):
            return {'statuses': [status()]}
        def enqueue_supplier(self, *args, **kwargs):
            raise AssertionError('Read-only comparison must not generate')
    store = Store(tmp_path/'orch.db');sent=[]
    mid = store.add_message(1, 'text', 'Цены конечные')
    intent = {'reply': '', 'actions': [{'type':'inspect_supplier_photo', 'inventory':inventory()}]}
    response = handle_intent(store, Factory(), lambda *a: sent.append(a), mid, intent)
    assert len(sent) == 2
    assert 'Готовые карточки' in sent[0][0] and 'распознано позиций' not in sent[0][0]
    assert 'распознано позиций' in sent[1][0] and 'Готовые карточки' not in sent[1][0]
    assert any('Обновить' in button['text'] for row in sent[0][1] for button in row)
    assert response['notices'] == [sent[0][0]]
    assert store.batches('open') == []
    again = handle_intent(store, Factory(), lambda *a: sent.append(a), mid, intent)
    assert again['duplicate'] and len(sent) == 2 and again['notices'] == response['notices']


def test_long_separate_notices_are_chunked_without_missing_rows(tmp_path):
    class Factory:
        def resolve(self, action):
            raise AssertionError
        def supplier_photo_status(self, inv):
            return {'statuses': [dict(status(), row_index=row['row_index'], job_id=4000+row['row_index'])
                                 for row in inv['rows']]}
    inv = inventory()
    inv['rows'] = [dict(inv['rows'][0], row_index=n, model='PPG-'+str(n)) for n in range(1, 41)]
    inv['image_row_counts'][0]['row_count'] = 40
    store=Store(tmp_path/'orch.db');mid=store.add_message(1,'text','Цены конечные');sent=[]
    result=handle_intent(store, Factory(), lambda *a: sent.append(a), mid,
                        {'reply':'','actions':[{'type':'inspect_supplier_photo','inventory':inv}]})
    assert len(sent)>2
    assert all(len(text.encode('utf-16-le'))//2<=3800 for text,buttons in sent)
    assert all('PPG-'+str(n)+':' in result['notices'][0] for n in range(1,41))
