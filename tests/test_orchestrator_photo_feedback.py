import json
from dataclasses import replace

import pytest

from orchestrator_bot import bot, drafts, photo_feedback_ui as ui
from orchestrator_bot.store import Store

OWNER = 1264067528
NOW = 1800000000


class Tg:
    def __init__(self):
        self.sent = []
    def send(self, chat, text, keyboard=None, reply_keyboard=None):
        self.sent.append((text, keyboard))
        return len(self.sent)
    def edit(self, chat, mid, text, keyboard=None):
        self.sent.append((text, keyboard))
    def answer_callback(self, *args):
        pass
    def download_voice(self, file_id):
        return b'voice'
    def clear_buttons(self, *args):
        pass
    def send_photo(self, chat, path, caption='', keyboard=None):
        self.sent.append((caption, keyboard))
        return len(self.sent)


class Factory:
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.visual_calls = []
    def hint(self):
        return {}
    def correct_photo(self, payload):
        self.calls.append(payload)
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError('uncertain result')
        return {'job_id': 4000, 'new_job_created': len(self.calls) == 1}
    def visual_submit(self, scene, request_key, new=False):
        self.visual_calls.append((scene, request_key, new))
        return {'job_id': 555}


@pytest.fixture
def deps(tmp_path, monkeypatch):
    card = {'audit_id': 'a' * 32, 'job_id': 3857, 'revision': 1, 'sha256': 'f' * 64,
            'brand': 'CARVER', 'model': 'PPG-11000 DVR', 'status': 'review', 'summary': 'Проверьте тип топлива'}
    monkeypatch.setattr(ui.photo_reviews, 'get_card', lambda store, audit_id: card if audit_id == card['audit_id'] else None)
    monkeypatch.setattr(ui.photo_reviews, 'list_cards', lambda store, keys=None, limit=12, offset=0: [card])
    monkeypatch.setattr(ui.photo_reviews, 'create_feedback', lambda c, n, a, r:
                        {**{k: c[k] for k in ('audit_id', 'job_id', 'revision', 'sha256', 'brand', 'model')},
                         'notes': n, 'actor_id': a, 'request_id': r})
    result = bot.Deps(store=Store(tmp_path / 'orch.db'), tg=Tg(), factory=Factory(),
                      transcriber=lambda data, hint: 'Это дизельный генератор, убери бензин',
                      owner_chat_id=OWNER, clock=lambda: NOW)
    result.test_card = card
    return result


def cb(data, chat=OWNER):
    return {'callback_query': {'id': 'cb', 'data': data, 'from': {'id': OWNER},
                              'message': {'message_id': 9, 'chat': {'id': chat}}}}


def message(text=None, voice=False):
    return {'message': {'message_id': 10, 'from': {'id': OWNER}, 'chat': {'id': OWNER},
                        **({'voice': {'file_id': 'v'}} if voice else {'text': text})}}


def start(deps):
    bot.handle_update(cb('pf:edit:' + deps.test_card['audit_id']), deps)
    return deps.store.get_meta(ui.PENDING)


def test_voice_correction_survives_restart_and_needs_explicit_confirmation(deps):
    request_id = start(deps)
    bot.handle_update(message(voice=True), deps)
    assert not deps.factory.calls and deps.store.count_new() == 0
    assert 'дизельный' in deps.tg.sent[-2][0]
    assert deps.tg.sent[-1][1][0][0]['callback_data'] == 'pf:send:' + request_id
    restarted = replace(deps, store=Store(deps.store.path))
    bot.handle_update(cb('pf:send:' + request_id), restarted)
    payload = deps.factory.calls[0]
    assert 'Это дизельный генератор, убери бензин' in payload['notes']
    assert payload['notes'].index('Это дизельный') < payload['notes'].index('Проверьте тип топлива')
    assert payload['sha256'] == deps.test_card['sha256'] and payload['job_id'] == 3857
    bot.handle_update(cb('pf:send:' + request_id), restarted)
    assert len(deps.factory.calls) == 1


def test_timeout_retries_same_request_without_losing_comment(deps):
    request_id = start(deps)
    bot.handle_update(message('Убери бензин'), deps)
    deps.factory.fail_once = True
    bot.handle_update(cb('pf:send:' + request_id), deps)
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert len(deps.factory.calls) == 2
    assert deps.factory.calls[0] == deps.factory.calls[1]


def test_changed_photo_cannot_receive_old_comment(deps):
    request_id = start(deps)
    bot.handle_update(message('Это дизель'), deps)
    deps.test_card['sha256'] = 'e' * 64
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert not deps.factory.calls
    assert 'Карточка уже изменилась' in deps.tg.sent[-1][0]


def test_cancel_and_old_button_do_not_render(deps):
    request_id = start(deps)
    bot.handle_update(message('Это дизель'), deps)
    bot.handle_update(message('/cancel'), deps)
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert not deps.factory.calls


def test_voice_failure_preserves_target_and_does_not_render(deps):
    request_id = start(deps)
    deps.transcriber = lambda data, hint: ''
    bot.handle_update(message(voice=True), deps)
    assert deps.store.get_meta(ui.PENDING) == request_id
    assert not deps.factory.calls


def test_long_comment_is_not_truncated_in_payload_or_preview(deps):
    request_id = start(deps)
    comment = 'дизель ' * 570 + 'x' * 10
    assert len(comment) == 4000
    bot.handle_update(message(comment), deps)
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert comment.strip() in deps.factory.calls[0]['notes']
    assert all(len(text) <= 4096 for text, _ in deps.tg.sent)


def test_comment_over_limit_is_kept_out_of_generation(deps):
    request_id = start(deps)
    bot.handle_update(message('д' * 4001), deps)
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert not deps.factory.calls
    ticket = json.loads(deps.store.get_meta(ui._key(request_id)))
    assert ticket['phase'] == 'notes' and 'notes' not in ticket


def test_foreign_chat_cannot_submit_owner_correction(deps):
    request_id = start(deps)
    bot.handle_update(message('Это дизель'), deps)
    bot.handle_update(cb('pf:send:' + request_id, chat=99), deps)
    assert not deps.factory.calls


def test_new_card_selection_invalidates_previous_confirmation(deps):
    old_request = start(deps)
    bot.handle_update(message('Старое замечание'), deps)
    start(deps)
    bot.handle_update(cb('pf:send:' + old_request), deps)
    assert not deps.factory.calls


def test_editing_comment_invalidates_old_confirmation(deps):
    old_request = start(deps)
    bot.handle_update(message('Старое замечание'), deps)
    bot.handle_update(message('Дизель, а не бензин'), deps)
    new_request = deps.store.get_meta(ui.PENDING)
    assert old_request != new_request
    bot.handle_update(cb('pf:send:' + old_request), deps)
    assert not deps.factory.calls
    bot.handle_update(cb('pf:send:' + new_request), deps)
    assert 'Дизель, а не бензин' in deps.factory.calls[0]['notes']


def test_uncertain_submission_blocks_new_comment_and_target(deps):
    request_id = start(deps)
    bot.handle_update(message('Это дизель'), deps)
    deps.factory.fail_once = True
    bot.handle_update(cb('pf:send:' + request_id), deps)
    bot.handle_update(message('А теперь другой текст'), deps)
    start(deps)
    assert deps.store.get_meta(ui.PENDING) == request_id
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert deps.factory.calls[0] == deps.factory.calls[1]


def test_terminal_rejection_is_explained_without_success_message(deps):
    request_id = start(deps)
    bot.handle_update(message('Это дизель'), deps)
    deps.factory.correct_photo = lambda payload: {'handled': True, 'reason': 'not_active',
                                                 'message': 'Эта карточка заменена новой'}
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert deps.tg.sent[-1][0] == 'Эта карточка заменена новой'
    assert not deps.store.get_meta(ui.PENDING)


def test_reply_to_other_message_does_not_become_card_correction(deps):
    request_id = start(deps)
    update = message('Другой вопрос')
    update['message']['reply_to_message'] = {'message_id': 999, 'text': 'Совсем другая тема'}
    bot.handle_update(update, deps)
    ticket = json.loads(deps.store.get_meta(ui._key(request_id)))
    assert ticket['phase'] == 'notes' and not deps.factory.calls
    assert deps.store.count_new() == 1


def test_selected_card_photo_has_exact_correction_button(deps):
    deps.test_card['preview_path'] = '/saved/card.png'
    start(deps)
    assert deps.tg.sent[0][1] == [[{'text': '✏️ Указать ошибку',
                                   'callback_data': 'pf:edit:' + deps.test_card['audit_id']}]]


def test_editorial_correction_is_previewed_before_render(deps):
    deps.store.add_draft({'id': 'post', 'channel': 'tg_editorial', 'title': 'Генератор',
                          'text': 'Текст', 'visual_prompt': 'Старое задание'}, 'post')
    row = deps.store.get_draft('post')
    deps.store.update_draft(row['n'], status='ready', image_job=77)
    bot.handle_update(cb(f"d:f:{row['n']}"), deps)
    bot.handle_update(message('Это дизельный генератор'), deps)
    assert not deps.factory.visual_calls
    bot.handle_update(cb(f"d:g:{row['n']}"), deps)
    assert 'Это дизельный генератор' in deps.factory.visual_calls[0][0]
    bot.handle_update(cb(f"d:g:{row['n']}"), deps)
    assert len(deps.factory.visual_calls) == 1


def test_new_correction_is_not_dropped_after_long_previous_prompt(deps):
    deps.store.add_draft({'id': 'post', 'channel': 'tg_editorial', 'title': 'Генератор',
                          'text': 'Текст', 'visual_prompt': 'Старое описание ' * 200}, 'post')
    row = deps.store.get_draft('post')
    deps.store.update_draft(row['n'], status='ready')
    drafts.redo_visual(deps.store, deps.factory, row['n'], 'Это дизельный генератор, убери бензин')
    assert deps.factory.visual_calls[0][0].startswith(
        drafts.WISH_MARKER + ': Это дизельный генератор, убери бензин')


def test_older_cards_are_available_on_next_page(deps, monkeypatch):
    cards = [{**deps.test_card, 'audit_id': f'{n:032x}', 'job_id': n} for n in range(30, 4, -1)]
    calls = []
    def listing(store, keys=None, limit=12, offset=0):
        calls.append((limit, offset))
        return cards[offset:offset + limit]
    monkeypatch.setattr(ui.photo_reviews, 'list_cards', listing)
    ui.list_cards(deps, OWNER)
    first = deps.tg.sent[-1][1]
    assert len(first) == 13
    assert first[-1][0]['callback_data'] == 'pf:page:all.1'
    bot.handle_update(cb('pf:page:all.1'), deps)
    second = deps.tg.sent[-1][1]
    assert second[0][0]['callback_data'] == 'pf:edit:' + cards[12]['audit_id']
    assert second[-1][0]['callback_data'] == 'pf:page:all.0'
    assert calls == [(13, 0), (13, 12)]


def test_submission_without_product_link_never_promises_new_preview(deps):
    request_id = start(deps)
    bot.handle_update(message('Исправить топливо'), deps)
    calls = []
    def submit(payload):
        calls.append(payload)
        return {'handled': True, 'submitted': True, 'linked': False, 'job_id': 4000,
                'message': 'Товар отменён во время передачи; нужна ручная проверка'}
    deps.factory.correct_photo = submit
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert 'не удалось связать' in deps.tg.sent[-1][0]
    assert 'Пришлю новую версию' not in deps.tg.sent[-1][0]
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert len(calls) == 1


def test_rejected_old_job_id_is_not_reported_as_new_render(deps):
    request_id = start(deps)
    bot.handle_update(message('Исправить топливо'), deps)
    deps.factory.correct_photo = lambda _: {'handled': True, 'submitted': False,
        'job_id': deps.test_card['job_id'], 'message': 'Нет сохранённого исходника'}
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert deps.tg.sent[-1][0] == 'Нет сохранённого исходника'


def test_switching_dialogue_cannot_forget_uncertain_submission(deps):
    request_id = start(deps)
    bot.handle_update(message('Исправить топливо'), deps)
    deps.factory.fail_once = True
    bot.handle_update(cb('pf:send:' + request_id), deps)
    assert ui.dismiss_pending(deps.store) is False
    assert deps.store.get_meta(ui.PENDING) == request_id
