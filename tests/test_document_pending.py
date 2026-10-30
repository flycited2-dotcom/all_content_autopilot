"""Short answers inherit a file only through a real source-bound question."""
from types import SimpleNamespace

import pytest

from listener.routing import ModelRouter
from orchestrator_bot.document_context import pending_ask_id, pending_response
from orchestrator_bot.document_transport import save_original
from orchestrator_bot.store import Store
from orchestrator_bot.supplier_import import source_message


SOURCE = 'Модель;Цена;Остаток\nCARVER PPG-5000D;45455;149'


def test_multistep_clarifications_keep_original_document_across_restart(tmp_path):
    store, first, _ = setup(tmp_path)
    choose = store.add_message(2, 'text', 'Все позиции', now=1001)
    assert source_message(store, choose)['document']['source_message_id'] == first
    store.save_ask(choose, {'type': 'ask', 'question': 'Какая наценка?', 'options': ['15%']}, ['15%'])
    reopened = Store(store.path)
    percent = reopened.add_message(3, 'voice', '15%', now=1002)
    bound = source_message(reopened, percent)
    assert bound['document_context'] == 'pending_document'
    assert bound['document']['sha256'] == store.get_message(first)['document']['sha256']


def setup(tmp_path, *, kind='document', ask=True):
    store = Store(tmp_path / 'orch.db')
    document = save_original(tmp_path, SOURCE.encode(), 'source.csv', {'text': SOURCE, 'metadata': {}})
    mid = store.add_message(1, kind, 'Разбери прайс', now=1000, document=document)
    aid = store.save_ask(mid, {'type': 'ask', 'question': 'Какая наценка?',
                              'options': ['15%', '20%']}, ['15%', '20%']) if ask else None
    return store, mid, aid


@pytest.mark.parametrize('kind', ['document', 'text'])
@pytest.mark.parametrize('answer', ['15%', 'Наценка 15%', 'пятнадцать процентов', 'все', 'Все позиции'])
def test_pending_file_or_pasted_source_binds_exact_short_response(tmp_path, kind, answer):
    store, mid, aid = setup(tmp_path, kind=kind)
    current = store.add_message(2, 'text', answer, now=1001)
    selected = source_message(store, current)
    assert selected['document_context'] == 'pending_document'
    assert selected['document']['source_message_id'] == mid
    assert selected['text'] == answer
    assert store.get_ask(aid)['used'] is False  # read-only lookup; retries stable
    assert source_message(store, current) == selected


def test_short_response_runs_guarded_two_read_inventory_path(tmp_path):
    store, _, _ = setup(tmp_path)
    current = store.add_message(2, 'text', '15%', now=1001)
    selected = source_message(store, current)
    calls = []
    row = {'name': 'CARVER PPG-5000D', 'brand': 'CARVER', 'model': 'PPG-5000D',
           'cost_price': '45455', 'stock': 149, 'attachment_index': 0, 'row_index': 1}
    def read(instruction, payload):
        calls.append(payload)
        assert '15%' not in str(payload)
        return {'rows': [row], 'warnings': [], 'image_row_counts': [{'attachment_index': 0, 'row_count': 1}]}
    router = ModelRouter(tmp_path, claude=lambda *_: {'reply': '', 'actions': []},
                         chatgpt=SimpleNamespace(ready=lambda: False), vision_complete=read)
    intent = router.run_turn({'message': selected, 'context': {'capabilities': ['supplier_photo_inventory']}},
                              ('session', False))
    assert len(calls) == 2
    assert intent['actions'][0]['type'] == 'inspect_supplier_photo'
    assert intent['actions'][0]['inventory']['complete']


@pytest.mark.parametrize('answer', ['Какой статус?', 'Все холодильники Beko', 'Новая категория: телевизоры',
                                  '15% холодильников', 'Отмена'])
def test_pending_question_does_not_capture_unrelated_commands(tmp_path, answer):
    store, _, _ = setup(tmp_path)
    current = store.add_message(2, 'text', answer, now=1001)
    assert 'document' not in source_message(store, current)


def test_no_question_means_no_inferred_document_context(tmp_path):
    store, _, _ = setup(tmp_path, ask=False)
    current = store.add_message(2, 'text', '15%', now=1001)
    selected = source_message(store, current)
    assert 'document' not in selected
    assert 'document_source_error' in selected
    calls = []
    router = ModelRouter(tmp_path, claude=lambda *_: calls.append('decision'),
                         chatgpt=SimpleNamespace(ready=lambda: False), vision_complete=lambda *_: calls.append('read'))
    result = router.run_turn({'message': selected, 'context': {}}, ('session', False))
    assert not calls and not result['actions']
    assert 'к какому файлу' in result['reply']


def test_expired_question_cannot_select_file(tmp_path):
    store, _, _ = setup(tmp_path)
    current = store.add_message(2, 'text', '15%', now=2801)
    assert 'document' not in source_message(store, current)


def test_used_ask_cannot_be_selected_by_freeform_answer(tmp_path):
    store, _, aid = setup(tmp_path)
    store.use_ask(aid)
    current = store.add_message(2, 'text', '15%', now=1001)
    assert 'document' not in source_message(store, current)


def test_unanswered_old_question_does_not_capture_later_short_answer(tmp_path):
    store, _, _ = setup(tmp_path)
    first = store.add_message(2, 'text', '15%', now=1001)
    assert source_message(store, first).get('document')
    store.mark_done(first)
    current = store.add_message(3, 'text', '20%', now=1002)
    assert 'document' not in source_message(store, current)


def test_intervening_status_stops_freeform_source_inference(tmp_path):
    store, _, _ = setup(tmp_path)
    store.add_message(2, 'text', 'Какой статус?', now=1001)
    current = store.add_message(3, 'text', '15%', now=1002)
    assert 'document' not in source_message(store, current)


def test_latest_pending_question_must_itself_have_a_source(tmp_path):
    store, _, _ = setup(tmp_path)
    unrelated = store.add_message(2, 'text', 'Покажи холодильники', now=1001)
    store.save_ask(unrelated, {'type': 'ask', 'question': 'Сколько?', 'options': ['Все', 'Пять']}, ['Все', 'Пять'])
    current = store.add_message(3, 'text', 'все', now=1002)
    assert 'document' not in source_message(store, current)


def test_exact_callback_marker_preserves_used_ask_source_after_unrelated_status(tmp_path):
    store, mid, aid = setup(tmp_path)
    store.use_ask(aid)
    store.add_message(2, 'text', 'Какой статус?', now=1001)
    text = f'Ответ на вопрос «Какая наценка?»: 15%\n[ответ на вопрос id: {aid}]'
    current = store.add_message(None, 'text', text, now=1002)
    selected = source_message(store, current)
    assert selected['document_context'] == 'pending_document'
    assert selected['document']['source_message_id'] == mid


@pytest.mark.parametrize('kind', ['document', 'photo'])
def test_newer_source_blocks_old_ask_even_with_exact_marker(tmp_path, kind):
    store, mid, aid = setup(tmp_path)
    store.use_ask(aid)
    fields = ({'document': dict(store.get_message(mid)['document'], sha256='a' * 64)}
              if kind == 'document' else {'attachments': [{'data': 'new-photo'}]})
    store.add_message(2, kind, 'Новый источник', now=1001, **fields)
    text = f'Ответ на вопрос «Какая наценка для товаров из файла?»: 15%\n[ответ на вопрос id: {aid}]'
    current = store.add_message(None, 'text', text, now=1002)
    selected = source_message(store, current)
    assert 'document' not in selected
    assert 'attachments' not in selected
    assert 'document_source_error' in selected


def test_recent_lookup_also_supports_saved_pasted_text_source(tmp_path):
    store, mid, _ = setup(tmp_path, kind='text')
    current = store.add_message(2, 'text', 'Выбери товары из этого файла', now=1001)
    selected = source_message(store, current)
    assert selected['document_context'] == 'recent_document'
    assert selected['document']['source_message_id'] == mid


@pytest.mark.parametrize('text,expected', [('15%', True), ('+15%', True), ('все', True),
                                         ('15% холодильников', False), ('15', False), ('Как дела?', False)])
def test_short_answer_grammar_does_not_accept_arbitrary_tasks(text, expected):
    assert pending_response(text) is expected
    assert pending_ask_id(text) is None
