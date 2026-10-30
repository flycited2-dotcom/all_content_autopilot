"""Files travel from Telegram to durable facts and an idempotent supplier dispatch."""
import base64
import hashlib
from types import SimpleNamespace

import pytest

from orchestrator_bot import bot
from orchestrator_bot.api import handle_intent
from orchestrator_bot.document_transport import original_bundle
from orchestrator_bot.store import Store
from orchestrator_bot.supplier_import import source_message
from listener.routing import ModelRouter

SOURCE = 'Бренд;Модель;Цена;Остаток\nCARVER;PPG-5000D;45455;149'


class Telegram:
    def __init__(self, data):
        self.data, self.sent, self.downloads = data, [], 0

    def send(self, *args, **kwargs):
        self.sent.append(args)

    def download_file(self, file_id, max_bytes):
        self.downloads += 1
        return self.data


class Factory:
    def __init__(self):
        self.calls = []

    def supplier_photo_status(self, inventory):
        return {'statuses': []}

    def resolve(self, action):
        raise AssertionError('File import must not substitute catalog products')

    def enqueue_supplier(self, action, source_id, attachments, **options):
        self.calls.append((action, source_id, attachments, options))
        return {'accepted': ['excel|carver|ppg-5000d'], 'items': [], 'manifest_path': 'same-receipt'}


def setup(tmp_path, raw=SOURCE.encode(), filename='price.csv', caption=None):
    deps = bot.Deps(Store(tmp_path / 'orch.db'), Telegram(raw), lambda *_: '', Factory(), 100,
                    clock=lambda: 1000)
    deps.store.heartbeat(1000)
    message = {'message_id': 7, 'from': {'id': 100}, 'chat': {'id': 100},
               'document': {'file_id': 'F', 'file_name': filename, 'file_size': len(raw)}}
    if caption:
        message['caption'] = caption
    return deps, {'message': message}


def test_csv_original_survives_restart_and_duplicate_delivery(tmp_path):
    deps, update = setup(tmp_path)
    bot.handle_update(update, deps)
    bot.handle_update(update, deps)
    reopened = Store(deps.store.path)
    message = reopened.claim_next(1000)
    assert message['kind'] == 'document'
    assert 'PPG-5000D' in message['document']['text']
    bundle = original_bundle(tmp_path, message['document'])
    assert base64.b64decode(bundle['data']) == SOURCE.encode()
    assert deps.tg.downloads == 1 and reopened.count_new() == 1
    assert 'Файл принят' in deps.tg.sent[0][1]


@pytest.mark.parametrize('filename,raw', [('price.exe', b'anything'), ('price.xlsx', b'broken'), ('price.txt', b'')])
def test_invalid_file_never_enters_generation_queue(tmp_path, filename, raw):
    deps, update = setup(tmp_path, raw, filename)
    bot.handle_update(update, deps)
    assert deps.store.count_new() == 0 and deps.factory.calls == []
    assert len(deps.tg.sent) == 1


def test_full_file_to_two_reads_to_selection_retains_original_and_retries_once(tmp_path):
    deps, update = setup(tmp_path, caption='Генераторы в генерацию, наценка 15%')
    bot.handle_update(update, deps)
    message = deps.store.claim_next(1000)
    row = dict(name='Генератор CARVER PPG-5000D', brand='CARVER', model='PPG-5000D',
               cost_price='45455', stock=149, attachment_index=0, row_index=1)
    action = dict(type='enqueue_supplier_products', category='генераторы', markup_percent='15',
                  items=[{k: v for k, v in row.items() if k != 'row_index'}])
    reads = []
    def read(instruction, payload):
        reads.append(payload)
        assert 'наценка 15%' not in str(payload)
        return {'rows': [row], 'warnings': [], 'image_row_counts': [{'attachment_index': 0, 'row_count': 1}]}
    router = ModelRouter(root=tmp_path, claude=lambda *_: {'reply': '', 'actions': [action]},
                         chatgpt=SimpleNamespace(ready=lambda: False), vision_complete=read)
    intent = router.run_turn({'message': message, 'context': {'capabilities': ['supplier_photo_inventory']}}, ('new', True))
    assert len(reads) == 2
    sent = []
    first = handle_intent(deps.store, deps.factory, lambda *a: sent.append(a), message['id'], intent)
    second = handle_intent(deps.store, deps.factory, lambda *a: sent.append(a), message['id'], intent)
    assert second['duplicate'] and first['reply'] == second['reply']
    assert len(deps.factory.calls) == 1
    dispatched, _, images, options = deps.factory.calls[0]
    assert images == [] and 'attachment_index' not in dispatched['items'][0]
    assert options['new_only'] is True
    assert base64.b64decode(options['document']['data']) == SOURCE.encode()
    assert options['document']['sha256'] == hashlib.sha256(SOURCE.encode()).hexdigest()


def test_document_is_available_for_later_explicit_task_and_does_not_attach_to_status(tmp_path):
    deps, update = setup(tmp_path)
    bot.handle_update(update, deps)
    follow = deps.store.add_message(8, 'text', 'Запусти товары из файла с наценкой 15%', now=1001)
    selected = source_message(deps.store, follow)
    assert selected['document_context'] == 'recent_document'
    assert selected['document']['source_message_id'] == 1
    status = deps.store.add_message(9, 'text', 'Какой статус?', now=1002)
    assert 'document' not in source_message(deps.store, status)


def test_original_tampering_blocks_dispatch(tmp_path):
    deps, update = setup(tmp_path)
    bot.handle_update(update, deps)
    document = deps.store.claim_next(1000)['document']
    (tmp_path / 'documents' / (document['sha256'] + '.csv')).write_bytes(b'changed')
    with pytest.raises(ValueError, match='изменился'):
        original_bundle(tmp_path, document)


@pytest.mark.parametrize('prefix', ['', 'Новый прайс\n', 'Новый список\n'])
def test_new_pasted_table_does_not_inherit_previous_file_or_photos(tmp_path, prefix):
    deps, update = setup(tmp_path)
    bot.handle_update(update, deps)
    deps.store.add_message(8, 'photo', 'Предыдущее фото другого товара', now=1000.5,
                           attachments=[{'mime_type': 'image/png', 'data': 'old-image'}])
    text = prefix + 'Модель;Цена;Остаток\nCARVER PPG-7000DE;63910;7'
    follow = deps.store.add_message(9, 'text', text, now=1001)
    selected = source_message(deps.store, follow)
    assert selected['text'] == text
    assert 'document' not in selected and 'attachments' not in selected
    assert 'document_context' not in selected and 'attachment_context' not in selected


@pytest.mark.parametrize('text', ['Из Excel в работу с наценкой 15%', 'Возьми товары из этого файла',
                                'Разбери последний прайс', 'Из него выбери генераторы'])
def test_server_and_listener_agree_on_explicit_document_references(tmp_path, text):
    from listener.supplier_documents import is_task
    from orchestrator_bot.document_context import explicit_reference
    deps, update = setup(tmp_path)
    bot.handle_update(update, deps)
    follow = deps.store.add_message(8, 'text', text, now=1001)
    selected = source_message(deps.store, follow)
    assert explicit_reference(text)
    assert selected['document_context'] == 'recent_document'
    assert is_task({'message': selected})


@pytest.mark.parametrize('text', ['Какой статус?', 'Все холодильники Beko', 'Новая категория: телевизоры'])
def test_unrelated_followup_never_attaches_old_document(tmp_path, text):
    deps, update = setup(tmp_path)
    bot.handle_update(update, deps)
    follow = deps.store.add_message(8, 'text', text, now=1001)
    assert 'document' not in source_message(deps.store, follow)
