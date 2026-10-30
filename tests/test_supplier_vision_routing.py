"""Mandatory photo gates and isolated source-reading provider regressions."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import re
import subprocess
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest

from listener import brain, supplier_vision
from listener.attachments import decode_images
from listener.provider_errors import ProviderError
from listener.routing import ModelRouter


def photo(color='orange'):
    stream = io.BytesIO()
    Image.new('RGB', (12, 12), color).save(stream, format='PNG')
    return {'mime_type': 'image/png', 'data': base64.b64encode(stream.getvalue()).decode('ascii')}


def payload(mid=1, image=None):
    return {'message': {'id': mid, 'kind': 'photo', 'text': 'возьми в работу',
                        'attachments': [image or photo()]},
            'context': {'capabilities': ['supplier_photo_import', 'supplier_photo_inventory'],
                        'history': [{'role': 'assistant', 'content': 'Все CARVER уже обработаны'}]}}


def raw_inventory(rows=True):
    return {'rows': ([{'name': 'Генератор CARVER PPG-11000 DVR', 'brand': 'CARVER',
                       'model': 'PPG-11000 DVR', 'cost_price': '107625', 'stock': 10,
                       'attachment_index': 0, 'row_index': 1}] if rows else []),
            'warnings': [], 'image_row_counts': [{'attachment_index': 0, 'row_count': int(rows)}]}


class UnusedReserve:
    def ready(self):
        return True

    def complete(self, *args, **kwargs):
        raise AssertionError('Unexpected dialogue fallback')


def router(root, decision, vision):
    command = root / '.claude' / 'commands'
    command.mkdir(parents=True)
    (command / 'chat-turn.md').write_text('Верни reply и actions.', encoding='utf-8')
    return ModelRouter(root, claude=decision, chatgpt=UnusedReserve(), vision_complete=vision)


@pytest.mark.parametrize('action', [
    {'type': 'enqueue_products', 'category': 'генераторы', 'count': 1},
    {'type': 'enqueue_supplier_products', 'category': 'генераторы', 'markup_percent': 15,
     'items': [{k: v for k, v in raw_inventory()['rows'][0].items() if k != 'row_index'}]},
])
def test_zero_row_photo_cannot_dispatch_from_old_history(tmp_path, action):
    seen = []

    def decision(current, session):
        seen.append(deepcopy(current))
        return {'reply': 'Снова запускаю старые CARVER.', 'actions': [deepcopy(action)]}

    routing = router(tmp_path, decision, lambda *_: raw_inventory(rows=False))
    result = routing.run_turn(payload(), ('old-dialogue', False))
    assert seen[0]['context']['supplier_photo_checked'] is True
    assert result['actions'] == []
    assert 'не подтверждён полный прайс' in result['reply']
    assert 'Снова запускаю' not in result['reply']


def test_non_price_photo_keeps_ordinary_image_reply(tmp_path):
    seen = []

    def decision(current, session):
        seen.append(current)
        return {'reply': 'На фото упаковка генератора.', 'actions': []}

    routing = router(tmp_path, decision, lambda *_: raw_inventory(rows=False))
    incoming = payload()
    incoming['message']['text'] = 'Что изображено?'
    result = routing.run_turn(incoming, ('old-dialogue', False))
    assert result == {'reply': 'На фото упаковка генератора.', 'actions': []}
    assert seen[0]['message']['attachments'] == incoming['message']['attachments']


def test_verified_photo_cache_reuses_facts_but_runs_current_decision_each_time(tmp_path):
    vision_calls, decision_calls = [], []

    def vision(instruction, current):
        vision_calls.append(deepcopy(current))
        return raw_inventory()

    def decision(current, session):
        decision_calls.append(deepcopy(current))
        return {'reply': 'Уже всё обработано.', 'actions': []}

    routing = router(tmp_path, decision, vision)
    original = payload(1)
    first = routing.run_turn(original, ('old-dialogue', False))
    repeated = payload(2)
    repeated['context']['history'] = [{'role': 'assistant', 'content': 'Статус изменился на failed'}]
    second = routing.run_turn(repeated, ('old-dialogue', False))
    assert len(vision_calls) == 2  # Both independent passes happen only on the first identical source.
    assert len(decision_calls) == 2 and decision_calls[1]['message']['id'] == 2
    assert decision_calls[1]['context']['history'] == repeated['context']['history']
    assert 'attachments' not in decision_calls[0]['message']
    assert original['message']['attachments']  # Do not discard the persisted source evidence.
    assert first == second
    source, = first['actions']
    assert source['type'] == 'inspect_supplier_photo'
    assert source['inventory']['rows'][0]['model'] == 'PPG-11000 DVR'
    cached, = (tmp_path / 'runs/chat/supplier-inventories').glob('*.json')
    value = json.loads(cached.read_text(encoding='utf-8'))
    assert set(value) == {'rows', 'warnings', 'image_row_counts', 'complete', 'source_sha256', 'vision_passes'}
    assert 'failed' not in json.dumps(value) and 'reply' not in value and 'statuses' not in value


def test_changed_image_bytes_force_two_fresh_reads(tmp_path):
    calls = []
    routing = router(tmp_path, lambda *_: {'reply': '', 'actions': []},
                     lambda instruction, current: calls.append(current) or raw_inventory())
    first = routing.run_turn(payload(1, photo('orange')), ('old', False))
    second = routing.run_turn(payload(2, photo('blue')), ('old', False))
    assert len(calls) == 4
    assert first['actions'][0]['inventory']['source_sha256'] != second['actions'][0]['inventory']['source_sha256']


def test_legacy_policy_cache_cannot_suppress_current_source_verification(tmp_path):
    incoming = payload()
    hashes = [image['sha256'] for image in decode_images(incoming)]
    legacy_key = hashlib.sha256(('supplier-inventory-v0|' + '|'.join(hashes)).encode()).hexdigest()
    directory = tmp_path / 'runs/chat/supplier-inventories'
    directory.mkdir(parents=True)
    value = {**raw_inventory(), 'complete': True, 'source_sha256': hashes, 'vision_passes': 2}
    (directory / (legacy_key + '.json')).write_text(json.dumps(value), encoding='utf-8')
    calls = []
    routing = router(tmp_path, lambda *_: {'reply': '', 'actions': []},
                     lambda instruction, current: calls.append(current) or raw_inventory())
    routing.run_turn(incoming, ('old', False))
    assert len(calls) == 2


@pytest.mark.parametrize('damage', ['different_source_hash', 'cached_queue_status', 'incomplete'])
def test_invalid_cached_source_is_re_read_instead_of_reusing_a_filtered_reply(tmp_path, damage):
    calls = []
    routing = router(tmp_path, lambda *_: {'reply': '', 'actions': []},
                     lambda instruction, current: calls.append(current) or raw_inventory())
    routing.run_turn(payload(1), ('old', False))
    cached, = (tmp_path / 'runs/chat/supplier-inventories').glob('*.json')
    value = json.loads(cached.read_text(encoding='utf-8'))
    if damage == 'different_source_hash':
        value['source_sha256'] = ['0' * 64]
    elif damage == 'cached_queue_status':
        value['statuses'] = [{'state': 'published'}]
    else:
        value['complete'] = False
    cached.write_text(json.dumps(value), encoding='utf-8')
    routing.run_turn(payload(2), ('old', False))
    assert len(calls) == 4


def test_raw_claude_vision_uses_fresh_sessions_and_source_only_files(tmp_path):
    incoming = payload()
    calls, files = [], []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs['timeout'] == 90
        assert '--session-id' in cmd and '--resume' not in cmd
        uuid.UUID(cmd[cmd.index('--session-id') + 1])
        prompt = cmd[cmd.index('-p') + 1]
        relative = re.search(r'runs/chat/vision-[a-f0-9]+\.json', prompt).group()
        files.append(relative)
        document = json.loads((tmp_path / relative).read_text(encoding='utf-8'))
        assert document['instructions'] == 'Источник: только полная транскрипция.'
        source = document['source']
        assert set(source['context']) == {'supplier_vision'}
        assert 'history' not in json.dumps(source) and 'Все CARVER уже обработаны' not in json.dumps(source)
        image, = source['message']['attachments']
        assert 'data' not in image and (tmp_path / image['path']).is_file()
        assert (tmp_path / image['path']).read_bytes() == base64.b64decode(incoming['message']['attachments'][0]['data'])
        return SimpleNamespace(returncode=0, stdout=json.dumps({'result': json.dumps(raw_inventory())}), stderr='')

    for phase in ('enumerate_top_to_bottom', 'verify_bottom_to_top'):
        isolated = supplier_vision.isolated_payload(incoming, phase)
        result = brain.complete_vision('Источник: только полная транскрипция.', isolated, runner=run, root=tmp_path)
        assert json.loads(result) == raw_inventory()
    assert len(set(files)) == 2
    assert calls[0][calls[0].index('--session-id') + 1] != calls[1][calls[1].index('--session-id') + 1]


def test_decision_after_pixels_removed_keeps_short_photo_timeout(tmp_path):
    current = {'message': {'id': 5, 'kind': 'photo', 'text': 'возьми в работу'},
               'context': {'supplier_photo_checked': True,
                           'supplier_inventory': {**raw_inventory(), 'complete': True}}}
    seen = []

    def run(cmd, **kwargs):
        seen.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])

    with pytest.raises(ProviderError) as failure:
        brain.complete_turn(current, ('old', False), runner=run, root=tmp_path)
    assert seen == [60] and failure.value.kind == 'timeout'
