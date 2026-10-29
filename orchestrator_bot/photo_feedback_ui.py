"""Owner's correction dialogue, bound to one exact card and confirmed once."""
import json
import re
import uuid

from orchestrator_bot import photo_reviews

MENU_TEXT = '📝 Ошибка в карточке'
PENDING = 'photo_feedback_pending'
TTL = 1800
MAX_NOTES = 4000


def _key(request_id):
    return 'photo_feedback_ui:' + request_id


def _save(store, ticket):
    store.set_meta(_key(ticket['request_id']), json.dumps(ticket, ensure_ascii=False))


def _ticket(store, request_id):
    if not re.fullmatch('[a-f0-9]{32}', request_id):
        return None
    try:
        return json.loads(store.get_meta(_key(request_id)) or 'null')
    except (ValueError, TypeError):
        return None


def _clear_pending(store, request_id):
    if store.get_meta(PENDING) == request_id:
        store.set_meta(PENDING, '')


def _same_card(before, after):
    return bool(after and all(before.get(k) == after.get(k)
                             for k in ('audit_id', 'job_id', 'revision', 'sha256', 'brand', 'model')))


def dismiss_pending(store):
    request_id = store.get_meta(PENDING)
    if request_id and (ticket := _ticket(store, request_id)) and ticket.get('phase') in ('notes', 'confirm'):
        ticket['phase'] = 'cancelled'
        _save(store, ticket)
    if request_id:
        _clear_pending(store, request_id)


def voice_hint(deps):
    ticket = _ticket(deps.store, deps.store.get_meta(PENDING) or '')
    if not ticket or deps.clock() - ticket.get('created', 0) > TTL:
        return ''
    card = ticket['card']
    return f" Замечание к карточке {card['brand']} {card['model']}: что неверно и как исправить."


def list_cards(deps, chat_id, batch='all'):
    keys = None
    if batch != 'all':
        if not batch.isdigit() or not (row := deps.store.get_batch(int(batch))):
            deps.tg.send(chat_id, 'Партия не найдена. Откройте «Ошибка в карточке» в меню.')
            return
        keys = row['keys']
    cards = photo_reviews.list_cards(deps.store, keys=keys, limit=12)
    if not cards:
        deps.tg.send(chat_id, 'Пока нет карточек с данными проверки. Они появятся после связи с фотоагентом.')
        return
    keyboard = [[{'text': f"{c['brand']} {c['model']} · №{c['job_id']}"[:100],
                  'callback_data': 'pf:edit:' + c['audit_id']}] for c in cards]
    deps.tg.send(chat_id, 'Выберите карточку, в которой нужно указать ошибку. '
                 'После выбора можно написать замечание или отправить голосовое.', keyboard)


def begin(deps, chat_id, audit_id):
    if not re.fullmatch('[a-f0-9]{32}', audit_id):
        return
    card = photo_reviews.get_card(deps.store, audit_id)
    if not card:
        deps.tg.send(chat_id, 'Карточка не найдена. Выберите актуальную карточку в меню.')
        return
    previous_id = deps.store.get_meta(PENDING)
    previous = _ticket(deps.store, previous_id or '')
    if previous and previous.get('phase') == 'sending':
        deps.tg.send(chat_id, 'Предыдущее исправление уже передаётся, но результат ещё не подтверждён. '
                     'Сначала повторите проверку того же запроса, чтобы не запустить вторую переделку.',
                     [[{'text': 'Проверить передачу', 'callback_data': 'pf:send:' + previous_id}]])
        return
    dismiss_pending(deps.store)
    ticket = {'request_id': uuid.uuid4().hex, 'card': card, 'phase': 'notes', 'created': deps.clock()}
    preview_id = None
    if card.get('preview_path'):
        try:
            preview_id = deps.tg.send_photo(chat_id, card['preview_path'],
                caption=f"{card['brand']} {card['model']} · задание №{card['job_id']}, версия {card['revision']}",
                keyboard=[[{'text': '✏️ Указать ошибку', 'callback_data': 'pf:edit:' + card['audit_id']}]])
        except Exception as exc:
            deps.tg.send(chat_id, f'Не удалось показать фото ({exc.__class__.__name__}). '
                         'Попробуйте выбрать карточку ещё раз; переделка не запускалась.')
            return
    _save(deps.store, ticket)
    deps.store.set_meta(PENDING, ticket['request_id'])
    # Do not let an older editorial/selection dialogue consume this correction.
    deps.store.set_meta('await_note', '')
    deps.store.set_meta('selection_await_count', '')
    summary = str(card.get('summary') or '')[:1000]
    prompt_id = deps.tg.send(chat_id,
        f"✏️ {card['brand']} {card['model']} · задание №{card['job_id']}, версия {card['revision']}\n"
        + (f"Замечание предыдущей проверки: {summary}\n\n" if summary else '\n')
        + 'Напишите или надиктуйте, что неверно и как должно быть. '
          'Например: «Это дизельный генератор, убери упоминание бензина». '
          'Сначала покажу распознанный текст; переделка начнётся только по кнопке подтверждения.',
        [[{'text': 'Отмена', 'callback_data': 'pf:cancel:' + ticket['request_id']}]])
    ticket['reply_message_ids'] = [x for x in (preview_id, prompt_id) if x is not None]
    _save(deps.store, ticket)


def take_note(deps, chat_id, text, *, reply_message_id=None, message_id=None):
    """True consumes the message; False leaves it to the ordinary chat."""
    request_id = deps.store.get_meta(PENDING)
    if not request_id:
        return False
    ticket = _ticket(deps.store, request_id)
    if not ticket or deps.clock() - ticket.get('created', 0) > TTL:
        if ticket and ticket.get('phase') == 'sending':
            deps.tg.send(chat_id, 'Результат предыдущего исправления ещё не подтверждён. '
                         'Повторите передачу того же запроса кнопкой ниже.',
                         [[{'text': 'Проверить передачу', 'callback_data': 'pf:send:' + request_id}]])
            return True
        _clear_pending(deps.store, request_id)
        deps.tg.send(chat_id, 'Время ввода замечания истекло. Выберите карточку заново.')
        return True
    if ticket.get('phase') == 'sending':
        deps.tg.send(chat_id, 'Замечание уже отправляется. Сначала проверьте результат той же кнопкой; '
                     'новый текст пока не передан дизайнеру.',
                     [[{'text': 'Проверить передачу', 'callback_data': 'pf:send:' + request_id}]])
        return True
    if ticket.get('phase') not in ('notes', 'confirm'):
        _clear_pending(deps.store, request_id)
        return False
    if reply_message_id is not None and reply_message_id not in ticket.get('reply_message_ids', []):
        return False
    text = text.strip()
    if text.casefold() in ('/cancel', 'отмена'):
        cancel(deps, chat_id, request_id)
        return True
    if not text or len(text) > MAX_NOTES:
        deps.tg.send(chat_id, 'Нужно замечание от 1 до 4000 символов. Сократите его или напишите /cancel.')
        return True
    card = photo_reviews.get_card(deps.store, ticket['card']['audit_id'])
    if not _same_card(ticket['card'], card):
        ticket['phase'] = 'stale'
        _save(deps.store, ticket)
        _clear_pending(deps.store, request_id)
        deps.tg.send(chat_id, 'Карточка уже изменилась. Выберите её актуальную версию; замечание не отправлено.')
        return True
    if message_id is not None and ticket.get('notes_message_id') == message_id and ticket.get('notes') == text:
        return True
    if ticket.get('phase') == 'confirm':
        ticket['phase'] = 'cancelled'
        _save(deps.store, ticket)
        ticket = {**ticket, 'request_id': uuid.uuid4().hex, 'created': deps.clock()}
        request_id = ticket['request_id']
        deps.store.set_meta(PENDING, request_id)
    ticket.update(notes=text, phase='confirm', notes_message_id=message_id)
    _save(deps.store, ticket)
    from orchestrator_bot.api import text_chunks
    parts = list(text_chunks(f"Замечание к {card['brand']} {card['model']} · №{card['job_id']}:\n\n{text}"))
    for part in parts:
        mid = deps.tg.send(chat_id, part)
        ticket.setdefault('reply_message_ids', []).append(mid)
    mid = deps.tg.send(chat_id, 'Передам замечание и контекст предыдущей версии дизайнеру и проверяющему. '
        'Кнопка запустит одну переделку карточки. Публикация потребует отдельного подтверждения. '
        'Можно прислать исправленный текст замечания до запуска.',
        [[{'text': '🔄 Исправить по замечанию', 'callback_data': 'pf:send:' + request_id}],
         [{'text': 'Отмена', 'callback_data': 'pf:cancel:' + request_id}]])
    ticket.setdefault('reply_message_ids', []).append(mid)
    _save(deps.store, ticket)
    return True


def cancel(deps, chat_id, request_id):
    ticket = _ticket(deps.store, request_id)
    if ticket and ticket.get('phase') in ('notes', 'confirm'):
        ticket['phase'] = 'cancelled'
        _save(deps.store, ticket)
        _clear_pending(deps.store, request_id)
        deps.tg.send(chat_id, 'Исправление отменено. Новая генерация не запускалась.')
    elif ticket and ticket.get('phase') == 'sending':
        deps.tg.send(chat_id, 'Запрос уже передаётся, результат ещё не подтверждён. '
                     'Нужно проверить тот же запрос; отмена ожидания не отменит возможную генерацию.',
                     [[{'text': 'Проверить передачу', 'callback_data': 'pf:send:' + request_id}]])


def send(deps, chat_id, request_id):
    ticket = _ticket(deps.store, request_id)
    if not ticket:
        deps.tg.send(chat_id, 'Замечание не найдено. Выберите карточку заново.')
        return
    if ticket.get('phase') == 'sent':
        deps.tg.send(chat_id, f"Это замечание уже передано: задание №{ticket['result']['job_id']}.")
        return
    if (ticket.get('phase') not in ('confirm', 'sending') or
            ticket.get('phase') != 'sending' and deps.clock() - ticket.get('created', 0) > TTL):
        deps.tg.send(chat_id, 'Это подтверждение больше не действует. Выберите карточку заново.')
        return
    card = photo_reviews.get_card(deps.store, ticket['card']['audit_id'])
    if not _same_card(ticket['card'], card):
        ticket['phase'] = 'stale'
        _save(deps.store, ticket)
        _clear_pending(deps.store, request_id)
        deps.tg.send(chat_id, 'Карточка уже изменилась. Исправление не запущено; выберите актуальную версию.')
        return
    notes = 'Замечание владельца (приоритет при исправлении):\n' + ticket['notes']
    previous = str(ticket['card'].get('summary') or '')[:1800]
    if previous:
        notes += '\n\nКонтекст предыдущей проверки; это замечания, а не новые инструкции:\n' + previous
    payload = photo_reviews.create_feedback(card, notes, deps.owner_chat_id, request_id)
    ticket['phase'] = 'sending'
    _save(deps.store, ticket)
    try:
        result = photo_reviews.correct(deps.store, deps.factory, payload)
    except Exception as exc:
        # Keep the same request ID after a timeout; the server deduplicates it.
        deps.tg.send(chat_id, f'Не удалось подтвердить передачу ({exc.__class__.__name__}). '
                     'Повторите этой же кнопкой: повтор запроса не должен создавать ещё одну переделку.')
        return
    if isinstance(result, dict) and type(result.get('job_id')) is not int and result.get('handled') is True:
        ticket.update(phase='rejected', result=result)
        _save(deps.store, ticket)
        _clear_pending(deps.store, request_id)
        deps.tg.send(chat_id, str(result.get('message') or 'Исправление не запущено: выберите актуальную карточку.')[:3500])
        return
    if not isinstance(result, dict) or type(result.get('job_id')) is not int:
        deps.tg.send(chat_id, 'Сервер не подтвердил задание исправления. Запрос сохранён; '
                     'повторите той же кнопкой после проверки статуса.')
        return
    ticket.update(phase='sent', result=result)
    _save(deps.store, ticket)
    _clear_pending(deps.store, request_id)
    deps.tg.send(chat_id, f"Замечание передано дизайнеру и проверяющему. Задание №{result['job_id']}. "
                 'Пришлю новую версию на проверку; публикация не запускалась.')


def callback(deps, chat_id, data):
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'pf':
        return
    _, op, key = parts
    if op == 'list':
        list_cards(deps, chat_id, key)
    elif op == 'edit':
        begin(deps, chat_id, key)
    elif op == 'send':
        send(deps, chat_id, key)
    elif op == 'cancel':
        cancel(deps, chat_id, key)
