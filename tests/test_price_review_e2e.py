"""Owner decisions complete a price report without leaking into publication."""
from dataclasses import replace
from decimal import Decimal

import pytest

from orchestrator_bot import bot, price_review as ui
from orchestrator_bot.store import Store

OWNER, NOW = 1264067528, 1800000000


class Telegram:
    def __init__(self):
        self.sent = []
    def send(self, chat, text, keyboard=None, **kwargs):
        self.sent.append((text, keyboard))
        return len(self.sent)
    def edit(self, chat, mid, text, keyboard=None):
        self.sent.append((text, keyboard))
    def answer_callback(self, *args):
        pass
    def clear_buttons(self, *args):
        pass
    def download_voice(self, file_id):
        return b'voice'


class Factory:
    def __init__(self, statuses):
        self.statuses, self.calls, self.accepted = statuses, [], set()
        self.timeout_once = False
    def hint(self):
        return {}
    def supplier_photo_status(self, inventory):
        return {'statuses': self.statuses}
    def update_card_price(self, payload):
        self.calls.append(dict(payload))
        self.accepted.add(payload['request_id'])
        if self.timeout_once:
            self.timeout_once = False
            raise TimeoutError('accepted but response lost')
        return {'submitted': True, 'handled': True, 'job_id': payload['job_id']+4000,
                'old_job_id': payload['job_id'], 'linked': True, 'published': False}
    def publish_keys(self, *args, **kwargs):
        raise AssertionError('Price decisions must never publish')


def inputs(prices=('10050', '10300', '10600')):
    models = ['PPG-5000D', 'PPG-5000DE', 'PPG-11000DVR']
    rows = [dict(name='Генератор CARVER '+model, brand='CARVER', model=model,
                 cost_price=price, stock=2, attachment_index=0, row_index=n)
            for n, (model, price) in enumerate(zip(models, prices), 1)]
    inv = {'rows': rows, 'complete': True, 'warnings': [],
           'image_row_counts': [{'attachment_index': 0, 'row_count': len(rows)}]}
    statuses = [dict(attachment_index=0, row_index=n, state='ready', stage='card', job_id=9+n,
                     card_price='10000', card_price_verified=False, card_created_at='2026-10-30T10:00:00+00:00')
                for n in range(1, len(rows)+1)]
    return inv, statuses


@pytest.fixture
def prepared(tmp_path):
    inv, statuses = inputs()
    store = Store(tmp_path/'orch.db')
    mid = store.add_message(1, 'document', 'Готовые цены, добавлять проценты не надо', now=NOW)
    ticket = ui.prepare(store, mid, inv, statuses,
                        {'markup_percent': '0', 'label': 'готовая цена из нового прайса'}, now=NOW)
    deps = bot.Deps(store, Telegram(), lambda *_: 'Обновить цены', Factory(statuses), OWNER, clock=lambda: NOW)
    return deps, ticket, inv


def callback(ticket, operation):
    return f'pr:{operation}:{ticket["id"]}'


def cb_update(data, *, actor=OWNER):
    return {'callback_query': {'id': 'price-cb', 'data': data, 'from': {'id': actor},
                              'message': {'message_id': 9, 'chat': {'id': OWNER}}}}


@pytest.mark.parametrize('delta,expected', [('99.9','ignored'), ('100','ignored'), ('100.01','review'),
                                          ('500','review'), ('500.01','urgent'),
                                          ('-100','ignored'), ('-500','review'), ('-501','urgent')])
def test_percentage_boundaries_are_relative_to_old_price_and_symmetric(delta, expected):
    assert ui.classify('10000', str(Decimal('10000')+Decimal(delta)),
                       {'ignore_percent': '1', 'urgent_percent': '5'}) == expected


def test_default_does_not_activate_example_tolerance_without_owner_choice(prepared):
    deps, ticket, _ = prepared
    assert ui.get_rule(deps.store) is None
    assert ui.classify('10000', '10001', None) == 'review'
    assert ui.classify('10000', '10000', None) == 'equal'


@pytest.mark.parametrize('old,new,conflict', [(None,'100',False), ('0','100',False),
                                            ('100',None,False), ('100','100.1',True)])
def test_uncertain_or_conflicting_price_is_never_hidden_by_tolerance(old, new, conflict):
    assert ui.classify(old,new,{'ignore_percent':'1','urgent_percent':'5'},conflict=conflict) == 'unknown'


def test_report_explains_next_actions_and_every_button_carries_exact_ticket(prepared):
    _, ticket, _ = prepared
    text, keyboard = ui.render(ticket)
    buttons=[button for row in keyboard for button in row]
    assert any('Обновить' in button['text'] for button in buttons)
    assert any('Оставить' in button['text'] for button in buttons)
    assert any('порог' in button['text'].casefold() for button in buttons)
    assert all(ticket['id'] in button['callback_data'] for button in buttons)
    assert 'PPG-5000D' in text  # factual review, rather than only a button menu


def test_update_requires_concrete_confirmation_and_duplicate_does_not_resubmit(prepared):
    deps, ticket, _ = prepared
    ui.callback(deps,OWNER,callback(ticket,'update'))
    assert not deps.factory.calls
    assert any(callback(ticket,'confirm') == b['callback_data']
               for row in deps.tg.sent[-1][1] for b in row)
    restarted=replace(deps,store=Store(deps.store.path))
    ui.callback(restarted,OWNER,callback(ticket,'confirm'))
    assert len(deps.factory.calls)==3
    assert {p['job_id'] for p in deps.factory.calls} == {10,11,12}
    assert all(p['actor_id']==OWNER for p in deps.factory.calls)
    assert {p['new_price'] for p in deps.factory.calls} == {'10050','10300','10600'}
    ui.callback(restarted,OWNER,callback(ticket,'confirm'))
    assert len(deps.factory.calls)==3


def test_keep_closes_decision_without_publishing_or_rendering(prepared):
    deps,ticket,_=prepared
    ui.callback(deps,OWNER,callback(ticket,'keep'))
    assert not deps.factory.calls
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    assert not deps.factory.calls


def test_rule_preset_only_persists_after_owner_confirmation_and_never_redraws(prepared):
    deps,ticket,_=prepared
    ui.callback(deps,OWNER,callback(ticket,'rule'))
    assert ui.get_rule(deps.store) is None and not deps.factory.calls
    ui.callback(deps,OWNER,callback(ticket,'save1'))
    saved=ui.get_rule(Store(deps.store.path))
    assert Decimal(saved['ignore_percent'])==1 and Decimal(saved['urgent_percent'])==5
    assert not deps.factory.calls


def test_timeout_retries_same_request_and_skips_successful_items(prepared):
    deps,ticket,_=prepared
    deps.factory.timeout_once=True
    ui.callback(deps,OWNER,callback(ticket,'update'))
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    first=dict(deps.factory.calls[0])
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    same=[p for p in deps.factory.calls if p['job_id']==first['job_id']]
    assert len(same)==2 and same[0]['request_id']==same[1]['request_id']
    assert len(deps.factory.accepted)==3


def test_foreign_telegram_actor_cannot_apply_owner_price_choice(prepared):
    deps,ticket,_=prepared
    bot.handle_update(cb_update(callback(ticket,'confirm'),actor=99),deps)
    assert not deps.factory.calls


def test_voice_update_enters_same_confirmation_without_starting_generation(prepared):
    deps,ticket,_=prepared
    ui.callback(deps,OWNER,callback(ticket,'show'))
    bot.handle_update({'message': {'message_id':10,'from':{'id':OWNER},'chat':{'id':OWNER},
                                  'voice':{'file_id':'v'}}},deps)
    assert not deps.factory.calls and deps.store.count_new()==1  # only original document remains
    assert any('Услышал' in text for text,_ in deps.tg.sent)
    assert any(callback(ticket,'confirm') == b['callback_data']
               for row in deps.tg.sent[-1][1] for b in row)


def test_unrelated_status_request_is_not_consumed_as_price_decision(prepared):
    deps,ticket,_=prepared
    ui.callback(deps,OWNER,callback(ticket,'show'))
    assert ui.take_text(deps,OWNER,'Какой статус?') is False
    assert not deps.factory.calls


def test_conflicting_sku_requires_exact_owner_choice_then_one_target_price(prepared):
    deps,_,_=prepared
    inv,statuses=inputs(prices=('10100','10200'))
    inv['rows'][1].update(brand=inv['rows'][0]['brand'],model=inv['rows'][0]['model'],
                          name=inv['rows'][0]['name'])
    statuses[1]['job_id']=statuses[0]['job_id']
    deps.factory.statuses=statuses
    mid=deps.store.add_message(2,'document','Цены конечные',now=NOW)
    ticket=ui.prepare(deps.store,mid,inv,statuses,{'markup_percent':'0','label':'готовые цены'},now=NOW)
    assert len(ticket['items'])==1
    assert set(ticket['items'][0]['new_prices'])=={'10100','10200'}
    assert ticket['items'][0]['selected_price'] is None
    ui.callback(deps,OWNER,callback(ticket,'update'))
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    assert not deps.factory.calls
    ui.callback(deps,OWNER,f'pr:choose:{ticket["id"]}:0:1')
    ui.callback(deps,OWNER,callback(ticket,'update'))
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    assert len(deps.factory.calls)==1
    assert deps.factory.calls[0]['new_price']==ticket['items'][0]['new_prices'][1]


def test_reply_to_old_price_report_cannot_keep_the_newer_ticket(prepared):
    deps,old,inv=prepared
    ui.callback(deps,OWNER,callback(old,'show'))
    old_report_mid=len(deps.tg.sent)
    mid=deps.store.add_message(2,'document','Цены конечные',now=NOW)
    current=ui.prepare(deps.store,mid,inv,deps.factory.statuses,
                       {'markup_percent':'0','label':'готовые цены'},now=NOW)
    ui.callback(deps,OWNER,callback(current,'show'))
    ui.take_text(deps,OWNER,'Оставить карточки',reply_message_id=old_report_mid,message_id=3)
    ui.callback(deps,OWNER,callback(current,'update'))
    ui.callback(deps,OWNER,callback(current,'confirm'))
    assert len(deps.factory.calls)==3


def test_partial_batch_keeps_conflict_actionable_and_later_updates_only_remaining(prepared):
    deps,_,_=prepared
    inv,statuses=inputs(prices=('10050','10300','10600'))
    inv['rows'][1].update(brand=inv['rows'][0]['brand'],model=inv['rows'][0]['model'],
                          name=inv['rows'][0]['name'])
    statuses[1]['job_id']=statuses[0]['job_id']
    deps.factory.statuses=statuses
    mid=deps.store.add_message(2,'document','Цены конечные',now=NOW)
    ticket=ui.prepare(deps.store,mid,inv,statuses,{'markup_percent':'0','label':'готовые цены'},now=NOW)
    ui.callback(deps,OWNER,callback(ticket,'update'))
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    assert [p['job_id'] for p in deps.factory.calls]==[12]
    ui.callback(deps,OWNER,f'pr:choose:{ticket["id"]}:0:1')
    ui.callback(deps,OWNER,callback(ticket,'update'))
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    assert [p['job_id'] for p in deps.factory.calls]==[12,10]


def test_published_and_expired_cards_are_not_submitted_for_price_update(prepared):
    deps,_,inv=prepared
    deps.factory.statuses[0]['state']='published'
    deps.factory.statuses[1]['state']='expired'
    mid=deps.store.add_message(2,'document','Цены конечные',now=NOW)
    ticket=ui.prepare(deps.store,mid,inv,deps.factory.statuses,
                       {'markup_percent':'0','label':'готовые цены'},now=NOW)
    ui.callback(deps,OWNER,callback(ticket,'update'))
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    assert [p['job_id'] for p in deps.factory.calls]==[12]


def test_flat_ruble_threshold_is_rejected_without_saving_any_rule(prepared):
    deps,ticket,_=prepared
    ui.callback(deps,OWNER,callback(ticket,'rule'))
    assert ui.take_text(deps,OWNER,'Игнорируй разницу до 350 рублей',message_id=10) is True
    assert ui.get_rule(deps.store) is None
    assert 'процент' in deps.tg.sent[-1][0]
    assert not deps.factory.calls


def test_custom_percent_rule_is_only_a_preview_until_explicit_save(prepared):
    deps,ticket,_=prepared
    ui.callback(deps,OWNER,callback(ticket,'rule'))
    assert ui.take_text(deps,OWNER,'порог 0,5% и 3%',message_id=10) is True
    assert ui.get_rule(deps.store) is None
    ui.callback(deps,OWNER,callback(ticket,'save1'))
    rule=ui.get_rule(deps.store)
    assert Decimal(rule['ignore_percent'])==Decimal('0.5')
    assert Decimal(rule['urgent_percent'])==3
    assert not deps.factory.calls


@pytest.mark.parametrize('operation',['keep','rule','save1'])
def test_uncertain_submission_cannot_be_abandoned_by_old_action_buttons(prepared,operation):
    deps,ticket,_=prepared
    ui.callback(deps,OWNER,callback(ticket,'rule'))
    ui.callback(deps,OWNER,callback(ticket,'show'))
    deps.factory.timeout_once=True
    ui.callback(deps,OWNER,callback(ticket,'update'))
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    attempted=dict(deps.factory.calls[0])
    ui.callback(deps,OWNER,callback(ticket,operation))
    assert ui.get_rule(deps.store) is None
    ui.callback(deps,OWNER,callback(ticket,'confirm'))
    same=[p for p in deps.factory.calls if p['job_id']==attempted['job_id']]
    assert len(same)==2 and same[0]['request_id']==same[1]['request_id']
    assert len(deps.factory.accepted)==3
