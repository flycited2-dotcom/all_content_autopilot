"""A resend must show the complete source and current states, without paid retries."""
import base64
import copy
import io
import json

from PIL import Image
import pytest

from listener import supplier_vision
from listener.routing import ModelRouter
from orchestrator_bot.api import handle_intent
from orchestrator_bot.store import Store


def inventory():
    facts = [('LIFAN', '6 GF-3 (LF7000)', '55665', 2),
             ('CARVER', 'PPG-5000D', '45455', 149),
             ('CARVER', 'PPG-5000DE', '52200', 63),
             ('CARVER', 'PPG-5000ID', '50720', 6),
             ('CARVER', 'PPG-7000DE', '63910', None),
             ('CARVER', 'PPG-7000IDE', '71535', None),
             ('CARVER', 'PPG-11000 DR', '101575', 23),
             ('CARVER', 'PPG-11000 DVR', '107625', 10)]
    return {'rows': [dict(name=brand + ' ' + model, brand=brand, model=model,
                          cost_price=price, stock=stock, attachment_index=0, row_index=n)
                     for n, (brand, model, price, stock) in enumerate(facts, 1)],
            'complete': True, 'warnings': [],
            'image_row_counts': [{'attachment_index': 0, 'row_count': len(facts)}]}


def attachment():
    image = io.BytesIO()
    Image.new('RGB', (4, 4), 'white').save(image, format='PNG')
    return {'mime_type': 'image/png', 'data': base64.b64encode(image.getvalue()).decode('ascii')}


def inspection(value=None):
    return {'type': 'inspect_supplier_photo', 'inventory': value or inventory()}


class ReadOnlyFactory:
    def __init__(self):
        self.reads = []
        self.state = 'processing'

    def supplier_photo_status(self, value):
        self.reads.append(copy.deepcopy(value))
        return {'statuses': [dict(attachment_index=r['attachment_index'], row_index=r['row_index'],
                                  state=self.state, stage='research', job_id=3855)
                             for r in value['rows']]}

    def resolve(self, action):
        raise AssertionError('Photo inspection must not search for catalog substitutes')

    def enqueue_supplier(self, *args, **kwargs):
        raise AssertionError('Photo inspection must not start paid generation')


def test_same_photo_resend_lists_all_rows_and_reads_current_queue(tmp_path):
    store, factory, sent = Store(tmp_path / 'orch.db'), ReadOnlyFactory(), []
    photo = attachment()
    first = store.add_message(100, 'photo', 'возьми в работу', now=100, attachments=[photo])
    second = store.add_message(101, 'photo', 'возьми в работу', now=200, attachments=[photo])
    intent = {'reply': '', 'actions': [inspection()]}
    before = handle_intent(store, factory, lambda *args: sent.append(args), first, intent)
    factory.state = 'failed'
    after = handle_intent(store, factory, lambda *args: sent.append(args), second, intent)
    assert len(factory.reads) == 2
    for response in (before, after):
        for row in inventory()['rows']:
            assert row['model'] in response['reply']
        assert '149' in response['reply'] and '63' in response['reply']
        assert 'не указан' in response['reply']
        assert 'уже обработаны' not in response['reply']
    assert before['reply'] != after['reply']
    assert store.batches('open') == []


def test_same_message_retry_reuses_durable_reply_without_status_side_effect(tmp_path):
    store, factory = Store(tmp_path / 'orch.db'), ReadOnlyFactory()
    mid = store.add_message(100, 'photo', 'проверь фото', now=100, attachments=[attachment()])
    intent = {'reply': '', 'actions': [inspection()]}
    first = handle_intent(store, factory, lambda *args: None, mid, intent)
    factory.state = 'failed'
    again = handle_intent(store, factory, lambda *args: None, mid, intent)
    assert len(factory.reads) == 1
    assert again['duplicate'] and again['reply'] == first['reply']


def test_read_only_inspection_recovers_if_process_dies_before_saving_reply(tmp_path):
    store, factory = Store(tmp_path / 'orch.db'), ReadOnlyFactory()
    mid = store.add_message(100, 'photo', 'проверь фото', now=100, attachments=[attachment()])
    assert store.mark_intent(mid)
    response = handle_intent(store, factory, lambda *args: None, mid,
                             {'reply': '', 'actions': [inspection()]})
    assert len(factory.reads) == 1 and not response.get('duplicate')
    assert all(row['model'] in response['reply'] for row in inventory()['rows'])


def test_inspection_and_question_recover_with_the_same_buttons_after_crash(tmp_path):
    store, factory, sent = Store(tmp_path / 'orch.db'), ReadOnlyFactory(), []
    mid = store.add_message(100, 'photo', 'проверь фото', now=100, attachments=[attachment()])
    ask = {'type': 'ask', 'question': 'Какую наценку применить?', 'options': ['15%', 'Другая', 'Отмена']}
    assert store.mark_intent(mid)
    previous_id = store.save_ask(mid, ask, ask['options'])
    response = handle_intent(store, factory, lambda *args: sent.append(args), mid,
                             {'reply': '', 'actions': [inspection(), ask]})
    assert len(factory.reads) == 1 and not response.get('duplicate')
    assert all(row['model'] in response['reply'] for row in inventory()['rows'])
    assert 'Какую наценку применить?' in response['reply']
    assert sent[-1][1][0][0]['callback_data'] == f'a:{previous_id}:0'
    assert store.get_ask(previous_id + 1) is None


def test_inspection_does_not_prevent_receipt_recovery_after_dispatch_crash(tmp_path):
    class ReceiptFactory(ReadOnlyFactory):
        def __init__(self):
            super().__init__()
            self.dispatches = []

        def enqueue_supplier(self, action, source_id, attachments=(), *, new_only=False):
            self.dispatches.append((source_id, new_only))
            row = {**action['items'][0], 'key': 'excel|carver|ppg-5000d', 'price': 52273}
            return {'accepted': [row['key']], 'items': [row], 'manifest_path': 'durable-receipt'}

        def workers(self):
            return {'generation': True, 'agent_online': True}

    store, factory = Store(tmp_path / 'orch.db'), ReceiptFactory()
    mid = store.add_message(100, 'photo', 'CARVER с фото +15%', now=100, attachments=[attachment()])
    source = {k: v for k, v in inventory()['rows'][1].items() if k != 'row_index'}
    enqueue = {'type': 'enqueue_supplier_products', 'category': 'генераторы', 'brand': 'CARVER',
               'markup_percent': 15, 'items': [source]}
    # The process died after durable enqueue, before its reply/batch was saved.
    assert store.mark_intent(mid)
    response = handle_intent(store, factory, lambda *args: None, mid,
                             {'reply': '', 'actions': [inspection(), enqueue]})
    assert not response.get('duplicate')
    assert len(factory.dispatches) == 1 and factory.dispatches[0][1] is True
    batch, = store.batches('open')
    assert batch['keys'] == ['excel|carver|ppg-5000d']
    assert all(row['model'] in response['reply'] for row in inventory()['rows'])


@pytest.mark.parametrize('change', [{'cost_price': '45456'}, {'stock': 150},
                                    {'model': 'PPG-5000DE'}, {'attachment_index': 1}])
def test_server_does_not_dispatch_facts_that_disagree_with_source_inventory(tmp_path, change):
    class RecordingFactory(ReadOnlyFactory):
        def __init__(self):
            super().__init__()
            self.dispatches = []

        def enqueue_supplier(self, *args, **kwargs):
            self.dispatches.append((args, kwargs))
            return {'accepted': [], 'items': []}

    store, factory = Store(tmp_path / 'orch.db'), RecordingFactory()
    mid = store.add_message(100, 'photo', 'CARVER с фото +15%', now=100, attachments=[attachment()])
    source = {k: v for k, v in inventory()['rows'][1].items() if k != 'row_index'}
    enqueue = {'type': 'enqueue_supplier_products', 'category': 'генераторы', 'brand': 'CARVER',
               'markup_percent': 15, 'items': [{**source, **change}]}
    response = handle_intent(store, factory, lambda *args: None, mid,
                             {'reply': '', 'actions': [inspection(), enqueue]})
    assert factory.dispatches == []
    assert all(row['model'] in response['reply'] for row in inventory()['rows'])
    assert store.batches('open') == []


def test_unconfirmed_transcription_is_visible_but_cannot_launch_generation(tmp_path):
    class RecordingFactory(ReadOnlyFactory):
        def __init__(self):
            super().__init__()
            self.dispatches = []

        def enqueue_supplier(self, *args, **kwargs):
            self.dispatches.append((args, kwargs))
            return {'accepted': [], 'items': []}

    store, factory = Store(tmp_path / 'orch.db'), RecordingFactory()
    mid = store.add_message(100, 'photo', 'CARVER с фото +15%', now=100, attachments=[attachment()])
    value = inventory()
    value.update(complete=False, warnings=['Два чтения не сошлись по числу строк.'])
    source = {k: v for k, v in value['rows'][1].items() if k != 'row_index'}
    enqueue = {'type': 'enqueue_supplier_products', 'category': 'генераторы', 'brand': 'CARVER',
               'markup_percent': 15, 'items': [source]}
    response = handle_intent(store, factory, lambda *args: None, mid,
                             {'reply': '', 'actions': [inspection(value), enqueue]})
    assert factory.dispatches == []
    assert all(row['model'] in response['reply'] for row in inventory()['rows'])
    assert 'Два чтения не сошлись' in response['reply']


def test_long_source_is_delivered_in_full_instead_of_silent_truncation(tmp_path):
    store, factory, sent = Store(tmp_path / 'orch.db'), ReadOnlyFactory(), []
    mid = store.add_message(100, 'photo', 'разбери все строки', now=100, attachments=[attachment()])
    value = inventory()
    template = value['rows'][1]
    value['rows'] = [{**template, 'model': f'PPG-{n:03d}-DVR', 'row_index': n}
                     for n in range(1, 61)]
    value['image_row_counts'][0]['row_count'] = len(value['rows'])
    response = handle_intent(store, factory, lambda *args: sent.append(args), mid,
                             {'reply': '', 'actions': [inspection(value)]})
    assert len(sent) > 1
    assert ''.join(message[0] for message in sent) == response['reply']
    assert 'PPG-001-DVR' in response['reply'] and 'PPG-060-DVR' in response['reply']
    assert all(len(message[0].encode('utf-16-le')) // 2 <= 3800 for message in sent)


class Dialogue:
    def ready(self):
        return True

    def complete(self, instructions, payload, history=()):
        return json.dumps({'reply': 'На фото уже обработанные модели CARVER.', 'actions': []})


def router_root(root):
    command = root / '.claude' / 'commands'
    command.mkdir(parents=True)
    (command / 'chat-turn.md').write_text('Верни reply и actions.', encoding='utf-8')


def payload(mid=1, text='возьми в работу', kind='photo'):
    return {'message': {'id': mid, 'kind': kind, 'text': text, 'created': 100,
                        'attachments': [attachment()]},
            'context': {'capabilities': ['supplier_photo_import', 'supplier_photo_inventory'],
                        'history': [{'role': 'assistant', 'content': 'Все CARVER уже обработаны'}]}}


def test_router_cannot_replace_inventory_with_previous_generic_answer(tmp_path, monkeypatch):
    router_root(tmp_path)
    extracted = []

    def extract(current, complete):
        extracted.append(copy.deepcopy(current))
        return inventory()

    monkeypatch.setattr(supplier_vision, 'extract', extract)
    router = ModelRouter(tmp_path, claude=lambda p, s: {'reply': 'Уже обработано.', 'actions': []},
                         chatgpt=Dialogue(), vision_complete=lambda i, p: None)
    first = router.run_turn(payload(1), ('dialogue', False))
    second = router.run_turn(payload(2), ('dialogue', False))
    assert len(extracted) == 2
    for intent in (first, second):
        source = next(a for a in intent['actions'] if a['type'] == 'inspect_supplier_photo')
        assert len(source['inventory']['rows']) == 8
        assert source['inventory']['rows'][0]['brand'] == 'LIFAN'
        assert 'уже обработ' not in intent['reply'].lower()


def test_unrelated_status_text_with_recent_photos_skips_supplier_extraction(tmp_path, monkeypatch):
    router_root(tmp_path)
    monkeypatch.setattr(supplier_vision, 'extract', lambda *args: (_ for _ in ()).throw(
        AssertionError('Unrelated history attachments must not become a new price import')))
    current = payload(text='Что сейчас в очереди?', kind='text')
    current['message']['attachment_context'] = 'recent_photos'
    router = ModelRouter(tmp_path, claude=lambda p, s: {'reply': 'Проверяю очередь.', 'actions': []},
                         chatgpt=Dialogue(), vision_complete=lambda i, p: None)
    assert router.run_turn(current, ('dialogue', False))['reply'] == 'Проверяю очередь.'
