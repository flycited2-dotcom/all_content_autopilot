"""An owner can explain a card defect; only a confirmed note orders a correction."""
import json

import pytest

from photo_auditor import owner_feedback as notes, runtime, service
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, product, report

OWNER = 123


def test_existing_card_can_be_selected_without_resending_photo(saved):
    _, store, row, tg = saved
    notes.show_cards(store, tg, OWNER)
    assert tg.events[-1][1][2][0][0]['callback_data'] == 'qf:begin:' + row['id']


class Telegram:
    def __init__(self):
        self.events = []

    def send(self, *args):
        self.events.append(('send', args))
        return len(self.events) + 100

    def answer_callback(self, *args):
        self.events.append(('answer', args))

    def clear_buttons(self, *args):
        self.events.append(('clear', args))

    def download_voice(self, file_id):
        self.events.append(('voice', file_id))
        return b'voice recording'

    def send_photo(self, *args, **kwargs):
        self.events.append(('photo', args, kwargs))


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'owner-feedback-test'})
    monkeypatch.delenv('ORCH_API_TOKEN', raising=False)
    runtime.register_job(tmp_path, 123, 'kbt', 'Midea', 'ABC-12-RU', 'Исходное задание')
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    store.finish(store.claim(audit_id=row['id']), report('review'))
    return tmp_path, store, store.get(row['id']), Telegram()


def click(action, identifier, actor=OWNER):
    return {'callback_query': {'id': 'owner-click', 'from': {'id': actor},
            'message': {'chat': {'id': OWNER}, 'message_id': 7}, 'data': f'qf:{action}:{identifier}'}}


def text(value, message_id=10, actor=OWNER):
    return {'message': {'from': {'id': actor}, 'chat': {'id': OWNER}, 'message_id': message_id, 'text': value}}


def voice(message_id=11, **extra):
    return {'message': {'from': {'id': OWNER}, 'chat': {'id': OWNER}, 'message_id': message_id,
            'voice': {'file_id': 'voice-id', 'duration': 20, 'file_size': 1024, **extra}}}


def test_button_prompts_exact_card_and_text_waits_for_confirmation(saved):
    root, store, row, tg = saved
    service.handle_update(click('begin', row['id']), store, tg, OWNER)
    service.handle_update(text('Это дизельный генератор, убери упоминание бензина.'), store, tg, OWNER)
    draft = notes.pending(store, OWNER)
    assert draft['audit_id'] == row['id'] and draft['sha256'] == row['sha256']
    assert 'дизельный' in draft['notes']
    calls = []
    notes.recover_owner_feedback(store, lambda payload: calls.append(payload))
    assert calls == []
    assert store.get(row['id'])['status'] == 'review'
    assert 'одна новая генерация' in tg.events[-1][1][1]
    assert tg.events[-1][1][2][0][0]['callback_data'] == f"qf:send:{draft['request_id']}"


def test_confirmed_note_survives_restart_and_is_delivered_once_with_old_report(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    draft = notes.capture(store, OWNER, 'Убери надпись про бензин.', 12)
    service.handle_update(click('send', draft['request_id']), store, tg, OWNER)
    service.handle_update(click('send', draft['request_id']), store, tg, OWNER)
    reopened = AuditStore(store.root)
    calls = []
    def deliver(payload):
        calls.append(payload)
        return {'handled': True, 'new_job_created': True, 'job_id': 124, 'source_job_id': 123}
    notes.recover_owner_feedback(reopened, deliver, tg=tg, owner=OWNER)
    notes.recover_owner_feedback(reopened, deliver, tg=tg, owner=OWNER)
    assert len(calls) == 1
    payload = calls[0]
    assert payload['audit_id'] == row['id'] and payload['sha256'] == row['sha256']
    assert payload['job_id'] == 123 and payload['actor_id'] == OWNER
    assert 'Убери надпись' in payload['notes'] and 'Корпус отличается' in payload['notes']
    assert notes.pending(reopened, OWNER) is None
    assert any('Задание №124' in event[1][1] for event in tg.events if event[0] == 'send')


def test_voice_transcript_is_previewed_without_queuing_any_render(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    seen = []
    def transcribe(data, hint):
        seen.append((data, hint))
        return 'Это дизельный генератор.'
    service.handle_update(voice(), store, tg, OWNER, transcriber=transcribe)
    assert seen == [(b'voice recording', 'Midea ABC-12-RU. Замечание к товарной карточке.')]
    assert notes.pending(store, OWNER)['notes'] == 'Это дизельный генератор.'
    notes.recover_owner_feedback(store, lambda _: pytest.fail('No render before confirm'))


def test_failed_voice_transcription_keeps_dialogue_and_hides_error(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    def broken(*_):
        raise RuntimeError('https://secret.example/token')
    service.handle_update(voice(), store, tg, OWNER, transcriber=broken)
    assert notes.pending(store, OWNER)['notes'] == ''
    assert 'не удалось' in tg.events[-1][1][1].casefold()
    assert 'secret.example' not in repr(tg.events)


@pytest.mark.parametrize('extra', [{'duration': 121}, {'file_size': 11 * 1024 * 1024}])
def test_large_voice_is_not_downloaded(saved, extra):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    service.handle_update(voice(**extra), store, tg, OWNER,
                          transcriber=lambda *_: pytest.fail('Oversized voice must not be transcribed'))
    assert not any(event[0] == 'voice' for event in tg.events)


def test_stale_or_changed_snapshot_cannot_confirm(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    draft = notes.capture(store, OWNER, 'Исправь тип топлива.', 12)
    (store.root / row['id'] / 'generated.bin').write_bytes(picture('black'))
    service.handle_update(click('send', draft['request_id']), store, tg, OWNER)
    notes.recover_owner_feedback(store, lambda _: pytest.fail('Changed file must not be redrawn'))
    assert 'изменилась' in tg.events[-1][1][1]


def test_older_transcript_button_cannot_confirm_a_new_note(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    first = notes.capture(store, OWNER, 'Первое замечание', 10)
    second = notes.capture(store, OWNER, 'Исправленный текст замечания', 11)
    assert first['request_id'] != second['request_id']
    service.handle_update(click('send', first['request_id']), store, tg, OWNER)
    notes.recover_owner_feedback(store, lambda _: pytest.fail('Old transcript must not be confirmed'))
    assert notes.pending(store, OWNER)['request_id'] == second['request_id']


def test_strangers_and_cancel_cannot_launch_a_correction(saved):
    root, store, row, tg = saved
    service.handle_update(click('begin', row['id'], actor=999), store, tg, OWNER)
    assert notes.pending(store, OWNER) is None and tg.events == []
    service.handle_update(click('begin', row['id']), store, tg, OWNER)
    service.handle_update(text('Чужое замечание', actor=999), store, tg, OWNER)
    assert notes.pending(store, OWNER)['notes'] == ''
    service.handle_update(text('/cancel'), store, tg, OWNER)
    assert notes.pending(store, OWNER) is None
    notes.recover_owner_feedback(store, lambda _: pytest.fail('Cancelled note must not be delivered'))


def test_manual_photo_and_source_search_do_not_buy_a_card_render(saved):
    root, store, row, tg = saved
    manual = store.submit({**product(), 'product_id': 'telegram-55'}, picture())
    store.finish(store.claim(audit_id=manual['id']), report('review'))
    with pytest.raises(ValueError, match='ручного фото'):
        notes.begin(store, manual['id'], OWNER, 'owner-click')
    runtime.register_job(root, 125, 'research', 'Midea', 'ABC-12-RU', 'категория')
    source = store.submit({**product(), 'product_id': 'fotogen-125'}, picture())
    store.finish(store.claim(audit_id=source['id']), report('review'))
    with pytest.raises(ValueError, match='поиск исходника'):
        notes.begin(store, source['id'], OWNER, 'owner-click')


def test_confirmed_delivery_retries_the_same_request_after_network_failure(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    draft = notes.capture(store, OWNER, 'Исправь надпись.', 10)
    notes.confirm(store, OWNER, draft['request_id'])
    calls = []
    def broken(payload):
        calls.append(payload)
        raise RuntimeError('network')
    notes.recover_owner_feedback(store, broken, now=100)
    notes.recover_owner_feedback(store, lambda _: pytest.fail('Backoff must delay retry'), now=110)
    notes.recover_owner_feedback(store, lambda payload: calls.append(payload) or {'handled': True, 'job_id': 124}, now=131)
    assert len(calls) == 2 and calls[0] == calls[1]


def test_tampered_signed_outbox_does_not_reach_generator(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    draft = notes.capture(store, OWNER, 'Исправь надпись.', 10)
    notes.confirm(store, OWNER, draft['request_id'])
    key = notes.PREFIX + 'outbox:' + draft['request_id']
    with store.connect() as db:
        value = json.loads(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
        value['payload']['notes'] = 'Подменённая инструкция'
        db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(value), key))
    notes.recover_owner_feedback(store, lambda _: pytest.fail('Tampered note must not be sent'))


def test_report_has_error_button_and_recheck_is_named_as_recheck(saved):
    root, store, row, tg = saved
    service.notify(store, tg, OWNER)
    buttons = [button for event in tg.events if event[0] == 'send' for line in (event[1][2] or []) for button in line]
    assert any(button['callback_data'] == 'qf:begin:' + row['id'] for button in buttons)
    assert any(button['text'] == '🔎 Перепроверить фото' for button in buttons)


def test_new_revision_cancels_confirmed_old_note_before_delivery(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    draft = notes.capture(store, OWNER, 'Исправь надпись.', 10)
    notes.confirm(store, OWNER, draft['request_id'])
    with store.connect() as db:
        db.execute("UPDATE audits SET status='regenerate' WHERE id=?", (row['id'],))
    store.submit(product(1), picture('gray'))
    notes.recover_owner_feedback(store, lambda _: pytest.fail('Old revision must not be delivered'), tg=tg, owner=OWNER)
    assert 'новая ревизия' in tg.events[-1][1][1]
    notes.recover_owner_feedback(store, lambda _: pytest.fail('Terminal cancellation must stay terminal'), tg=tg, owner=OWNER)
    assert len(tg.events) == 1


def test_backend_terminal_failure_does_not_call_old_job_a_new_render(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    draft = notes.capture(store, OWNER, 'Исправь надпись.', 10)
    notes.confirm(store, OWNER, draft['request_id'])
    notes.recover_owner_feedback(store, lambda _: {'handled': True, 'reason': 'superseded',
                                'message': 'У товара уже другая карточка.', 'job_id': 123}, tg=tg, owner=OWNER)
    assert 'У товара уже другая карточка' in tg.events[-1][1][1]
    assert 'Задание №123' not in tg.events[-1][1][1]


def test_voice_uses_shared_vps_recognizer_when_connected(monkeypatch):
    import base64
    import httpx
    from listener.client import ApiClient
    import orchestrator_bot.transcribe
    monkeypatch.setenv('ORCH_API_TOKEN', 'test-only-token')
    monkeypatch.setenv('ORCH_API_URL', 'http://127.0.0.1:8790')
    monkeypatch.setattr(orchestrator_bot.transcribe, 'Transcriber',
                        lambda: pytest.fail('Connected PC must not download a local voice model'))
    calls = []
    def post(client, path, body):
        calls.append((path, body))
        return {'text': 'Дизельный генератор'}
    monkeypatch.setattr(ApiClient, '_post', post)
    assert notes._transcribe(b'voice recording', 'CARVER PPG-11000 DR') == 'Дизельный генератор'
    assert calls == [('/photo-feedback-transcribe', {'audio': base64.b64encode(b'voice recording').decode(),
                                                   'hint': 'CARVER PPG-11000 DR'})]


def test_long_transcript_is_sent_in_full_and_only_last_chunk_confirms(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    original = '😀' * 4000
    service.handle_update(text(original), store, tg, OWNER)
    messages = [event[1] for event in tg.events if event[0] == 'send']
    assert original in ''.join(message[1] for message in messages)
    assert all(len(message[1].encode('utf-16-le')) // 2 <= 3800 for message in messages)
    assert all(message[2] is None for message in messages[:-1])
    assert messages[-1][2][0][0]['callback_data'].startswith('qf:send:')


def test_owner_can_reply_to_transcript_to_correct_recognition(saved):
    root, store, row, tg = saved
    notes.begin(store, row['id'], OWNER, 'owner-click')
    service.handle_update(text('Бензиновый'), store, tg, OWNER)
    draft = notes.pending(store, OWNER)
    correction = text('Дизельный', message_id=12)
    correction['message']['reply_to_message'] = {'message_id': draft['feedback_message_ids'][-1]}
    service.handle_update(correction, store, tg, OWNER)
    assert notes.pending(store, OWNER)['notes'] == 'Дизельный'
