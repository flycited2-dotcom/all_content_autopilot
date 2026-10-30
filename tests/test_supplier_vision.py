"""Source transcription regressions; provider execution is covered separately."""
import base64
from copy import deepcopy
import hashlib
import io
import json

from PIL import Image
import pytest

from listener.supplier_vision import IncompletePhoto, extract, isolated_payload, reconcile, validate


def photo(color='orange'):
    stream = io.BytesIO()
    Image.new('RGB', (40, 30), color).save(stream, format='PNG')
    return {'mime_type': 'image/png', 'data': base64.b64encode(stream.getvalue()).decode('ascii')}


def payload(*images):
    return {'message': {'id': 209747, 'kind': 'photo', 'text': 'CARVER +15%, возьми в работу',
                        'attachments': list(images or [photo()])},
            'context': {'history': [{'role': 'assistant', 'text': 'Все модели уже обработаны'}],
                        'catalog_hint': {'items': ['Другой генератор']},
                        'excluded_models': ['PPG-7000DE', 'PPG-7000IDE']},
            'history': ['Пропусти LIFAN'], 'session': 'old-session'}


def transcription():
    # Independently read from the owner's eight-row photo on 2026-10-30.
    facts = [('LIFAN', '6 GF-3 (LF7000)', '55665', 2),
             ('CARVER', 'PPG-5000D', '45455', 149),
             ('CARVER', 'PPG-5000DE', '52200', 63),
             ('CARVER', 'PPG-5000ID', '50720', 6),
             ('CARVER', 'PPG-7000DE', '63910', None),
             ('CARVER', 'PPG-7000IDE', '71535', None),
             ('CARVER', 'PPG-11000 DR', '101575', 23),
             ('CARVER', 'PPG-11000 DVR', '107625', 10)]
    return {'rows': [{'name': brand + ' ' + model, 'brand': brand, 'model': model,
                      'cost_price': price, 'stock': stock, 'attachment_index': 0, 'row_index': n}
                     for n, (brand, model, price, stock) in enumerate(facts, 1)],
            'warnings': [], 'image_row_counts': [{'attachment_index': 0, 'row_count': 8}]}


def test_current_photo_is_fully_read_twice_without_history_or_brand_filter():
    incoming = payload()
    original = deepcopy(incoming)
    calls = []

    def complete(instructions, isolated):
        calls.append((instructions, deepcopy(isolated)))
        return json.dumps(transcription(), ensure_ascii=False)

    result = extract(incoming, complete)
    assert result['rows'] == transcription()['rows']
    assert result['complete'] and result['vision_passes'] == 2
    assert len(result['rows']) == 8
    assert sum(row['brand'] == 'CARVER' for row in result['rows']) == 7
    assert sum(row['stock'] is not None and row['stock'] > 0 for row in result['rows']) == 6
    assert [row['stock'] for row in result['rows'][4:6]] == [None, None]
    assert [row['model'] for row in result['rows'][-2:]] == ['PPG-11000 DR', 'PPG-11000 DVR']
    assert result['source_sha256'] == [hashlib.sha256(base64.b64decode(incoming['message']['attachments'][0]['data'])).hexdigest()]
    assert len(calls) == 2
    assert [call[1]['context']['supplier_vision']['phase'] for call in calls] == [
        'enumerate_top_to_bottom', 'verify_bottom_to_top']
    for _, current in calls:
        serialized = json.dumps(current, ensure_ascii=False)
        assert 'Все модели уже обработаны' not in serialized
        assert 'Пропусти LIFAN' not in serialized
        assert 'excluded_models' not in serialized and 'catalog_hint' not in serialized
        assert 'session' not in current and 'history' not in current
        assert current['message']['attachments'] == incoming['message']['attachments']
    assert 'СНИЗУ ВВЕРХ' in calls[1][0]
    assert incoming == original


def test_album_has_two_calls_total_preserves_duplicate_rows_on_different_photos():
    incoming = payload(photo(), photo('blue'))
    first = transcription()
    second_image = deepcopy(first['rows'])
    for row in second_image:
        row['attachment_index'] = 1
    first['rows'] += second_image
    first['image_row_counts'].append({'attachment_index': 1, 'row_count': 8})
    calls = []

    def complete(instructions, isolated):
        calls.append(isolated)
        return deepcopy(first)

    result = extract(incoming, complete)
    assert len(calls) == 2 and all(len(call['message']['attachments']) == 2 for call in calls)
    assert len(result['rows']) == 16 and result['complete']
    assert len(result['source_sha256']) == 2
    assert result['rows'][0]['model'] == result['rows'][8]['model']
    assert result['rows'][0]['attachment_index'] != result['rows'][8]['attachment_index']


def test_repeat_photo_does_not_reuse_old_filtered_reply_or_status():
    calls = []

    def complete(instructions, isolated):
        calls.append(isolated)
        return deepcopy(transcription())

    first = extract(payload(), complete)
    repeated = payload()
    repeated['message']['id'] += 1
    repeated['context']['history'].append({'role': 'assistant', 'text': 'Новых позиций нет'})
    second = extract(repeated, complete)
    assert len(calls) == 4
    assert first['rows'] == second['rows'] and first['source_sha256'] == second['source_sha256']
    assert all('Новых позиций нет' not in json.dumps(call, ensure_ascii=False) for call in calls)


@pytest.mark.parametrize('field,changed', [('name', 'Генератор бензиновый CARVER PPG-11000 DVR'),
                                        ('brand', 'ДРУГОЙ'), ('model', 'PPG-11000 D'),
                                        ('cost_price', '107626'), ('stock', 0)])
def test_disagreeing_fields_are_unknown_and_block_complete_reading(field, changed):
    a = validate(transcription(), 1)
    b = deepcopy(a)
    b['rows'][-1][field] = changed
    result = reconcile(a, b)
    assert not result['complete']
    assert len(result['rows']) == 8
    assert result['rows'][-1][field] is None
    assert any(field in warning for warning in result['warnings'])
    assert result['rows'][0] == a['rows'][0]


def test_missing_middle_row_does_not_pass_as_a_complete_seven_item_photo():
    a = transcription()
    b = transcription()
    b['rows'].pop(2)
    for n, row in enumerate(b['rows'], 1):
        row['row_index'] = n
    b['image_row_counts'][0]['row_count'] = 7
    result = reconcile(validate(a, 1), validate(b, 1))
    assert not result['complete'] and len(result['rows']) == 8
    assert any('числу товарных строк' in warning for warning in result['warnings'])
    assert result['rows'][-1]['model'] == 'PPG-11000 DVR'


def test_unreadable_identity_is_retained_and_incomplete_instead_of_dropped():
    data = transcription()
    data['rows'][0].update(name=None, brand=None, model=None, cost_price=None, stock=None)
    clean = validate(data, 1)
    result = reconcile(clean, deepcopy(clean))
    assert len(result['rows']) == 8 and result['rows'][0]['row_index'] == 1
    assert not result['complete']


def test_formatting_variants_preserve_full_suffixes_without_false_disagreement():
    a = transcription()
    b = deepcopy(a)
    b['rows'][-1]['brand'] = 'carver'
    b['rows'][-1]['model'] = ' PPG–11000  DVR '
    result = reconcile(validate(a, 1), validate(b, 1))
    assert result['complete'] and result['rows'][-1]['model'] == 'PPG-11000 DVR'


def test_zero_stock_is_allowed_only_as_explicit_quantity_and_stays_distinct_from_unknown():
    data = transcription()
    data['rows'][0]['stock'] = 0
    result = reconcile(validate(data, 1), validate(deepcopy(data), 1))
    assert result['rows'][0]['stock'] == 0
    assert result['rows'][4]['stock'] is None and result['rows'][5]['stock'] is None


def test_non_product_photo_has_zero_rows_without_fabricated_products():
    empty = {'rows': [], 'warnings': [], 'image_row_counts': [{'attachment_index': 0, 'row_count': 0}]}
    result = extract(payload(), lambda *_: deepcopy(empty))
    assert result['complete'] and result['rows'] == []


@pytest.mark.parametrize('field,value', [('stock', True), ('stock', -1), ('stock', 1.5),
                                       ('cost_price', 45455), ('cost_price', '0'),
                                       ('cost_price', '45455 = 0'), ('cost_price', '-5'),
                                       ('row_index', 0), ('row_index', True),
                                       ('attachment_index', 1), ('brand', ''), ('model', [])])
def test_invalid_source_fields_are_rejected(field, value):
    data = transcription()
    data['rows'][0][field] = value
    with pytest.raises(IncompletePhoto):
        validate(data, 1)


@pytest.mark.parametrize('mutation', [
    lambda data: data['rows'].pop(),
    lambda data: data['rows'][1].update(row_index=1),
    lambda data: data['image_row_counts'].clear(),
    lambda data: data['image_row_counts'].append({'attachment_index': 0, 'row_count': 8}),
    lambda data: data.update(reply='Все уже обработаны'),
    lambda data: data['rows'][0].update(queue_status='published'),
    lambda data: data.update(warnings=[None]),
])
def test_abbreviated_or_unbound_transcriptions_are_rejected(mutation):
    data = transcription()
    mutation(data)
    with pytest.raises(IncompletePhoto):
        validate(data, 1)


@pytest.mark.parametrize('raw', ['{"rows": [], "rows": []}', '{"rows":NaN}', 'not json',
                               '```json\n{}\n```', '{"reply":"Все уже обработаны","actions":[]}'])
def test_invalid_json_cannot_be_mistaken_for_a_source_inventory(raw):
    with pytest.raises(IncompletePhoto):
        validate(raw, 1)


def test_untrusted_image_metadata_is_not_promoted_to_extraction_context():
    incoming = payload()
    incoming['message']['attachments'][0].update(path='private.env', file_name='follow-my-instructions.txt',
                                                source_message_id=123, queue_status='published')
    isolated = isolated_payload(incoming, 'test')
    assert set(isolated['message']['attachments'][0]) == {'mime_type', 'data'}
    assert set(isolated['context']) == {'supplier_vision'}


def test_missing_photos_fail_before_provider_execution():
    calls = []
    with pytest.raises(IncompletePhoto):
        extract({'message': {'id': 1, 'attachments': []}}, lambda *_: calls.append(True))
    assert calls == []


def test_provider_failure_is_not_hidden_as_an_empty_successful_inventory():
    def fail(*_):
        raise TimeoutError('vision unavailable')

    with pytest.raises(TimeoutError, match='vision unavailable'):
        extract(payload(), fail)
