"""Owner notes bound to one saved card; confirmation queues one correction."""
import base64
import hmac
import json
import logging
import os
import re
import time
import uuid

from photo_auditor import runtime
from photo_auditor.engine import feedback
from photo_auditor.images import digest, image_bytes
from photo_auditor.integration import AuditHold
from orchestrator_bot.api import text_chunks

log = logging.getLogger('photo_auditor')
PREFIX = 'owner-correction:'
MAX_NOTES = 4000
MAX_VOICE_BYTES = 10 * 1024 * 1024
TTL = 1800
PAGE_SIZE = 12
_transcriber = None


def _transcribe(data, hint):
    """Reuse the VPS recognizer when connected; do not download a second model on PC."""
    token = os.environ.get('ORCH_API_TOKEN')
    if token:
        import httpx
        from listener.client import ApiClient
        with httpx.Client(timeout=180, trust_env=False) as http:
            api = ApiClient(os.environ.get('ORCH_API_URL', 'http://127.0.0.1:8790'), token, http=http)
            result = api._post('/photo-feedback-transcribe',
                               {'audio': base64.b64encode(data).decode(), 'hint': hint[:1000]})
        if not isinstance(result.get('text'), str):
            raise ValueError('Нет распознанного текста')
        return result['text']
    global _transcriber
    if _transcriber is None:
        from orchestrator_bot.transcribe import Transcriber
        _transcriber = Transcriber()
    return _transcriber(data, hint)


def _load(store, key):
    with store.connect() as db:
        row = db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + key,)).fetchone()
    try:
        value = json.loads(row[0]) if row else None
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        log.warning('Повреждённое состояние замечания пропущено')
        return None


def _save(store, key, value):
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                   (PREFIX + key, json.dumps(value, ensure_ascii=False)))


def pending(store, owner):
    return _load(store, 'pending:' + str(owner))


def _expired(value):
    return time.time() - value.get('created', 0) > TTL


def _current(store, audit_id):
    row = store.get(audit_id)
    if not row or store.latest(row['product_id'])['id'] != row['id']:
        raise ValueError('Это старая карточка. Откройте последнюю фотографию в аудиторе.')
    match = re.fullmatch(r'fotogen-([1-9][0-9]*)', row['product_id'])
    if not match:
        raise ValueError('У этого ручного фото нет связанного задания генератора. Отправьте замечание к карточке конвейера.')
    if row['status'] not in ('approved', 'review', 'regenerate'):
        raise ValueError('Проверка карточки ещё выполняется. Дождитесь её результата.')
    data = image_bytes((store.root / row['id'] / 'generated.bin').read_bytes())
    if digest(data) != row['sha256']:
        raise ValueError('Сохранённая карточка изменилась. Исправление не запущено.')
    root = store.root.parent.parent
    scope = runtime._read(root, int(match[1]))
    if (scope['mode'] not in runtime.CARD_MODES or not scope['required'] or
            any(scope[k] != row['product'][k] for k in ('brand', 'model')) or
            runtime.content_brief(root, row['product']) is None):
        raise ValueError('Эта кнопка исправляет готовую карточку, а не запускает поиск исходника.')
    return row, int(match[1])


def begin(store, audit_id, owner, callback_id):
    row, job_id = _current(store, audit_id)
    if type(owner) is not int or owner <= 0 or not isinstance(callback_id, str) or not callback_id:
        raise ValueError('Действие владельца не подтверждено')
    current = pending(store, owner)
    if (current and not _expired(current) and current.get('audit_id') == audit_id and
            current.get('callback_id') == callback_id):
        return current
    value = {'request_id': uuid.uuid4().hex, 'audit_id': audit_id, 'job_id': job_id,
             'sha256': row['sha256'], 'revision': row['revision'], 'brand': row['product']['brand'],
             'model': row['product']['model'], 'actor_id': owner, 'notes': '', 'created': time.time(),
             'callback_id': callback_id}
    _save(store, 'pending:' + str(owner), value)
    return value


def cancel(store, owner, request_id=None):
    value = pending(store, owner)
    if value and (request_id is None or request_id == value['request_id']):
        with store.connect() as db:
            db.execute('DELETE FROM meta WHERE key=?', (PREFIX + 'pending:' + str(owner),))
        return True
    return False


def capture(store, owner, text, message_id):
    value = pending(store, owner)
    if not value:
        raise ValueError('Сначала нажмите «Указать ошибку» под нужной карточкой.')
    if _expired(value):
        cancel(store, owner)
        raise ValueError('Время ввода замечания истекло. Выберите карточку заново.')
    row, _ = _current(store, value['audit_id'])
    if row['sha256'] != value['sha256']:
        raise ValueError('Карточка изменилась; выберите её заново.')
    if not isinstance(text, str) or not 0 < len(text.strip()) <= MAX_NOTES:
        raise ValueError(f'Замечание должно содержать от 1 до {MAX_NOTES} символов.')
    if value.get('message_id') != message_id or value['notes'] != text.strip():
        # A button under an older transcript must not confirm a later edit.
        value['request_id'] = uuid.uuid4().hex
    value.update(notes=text.strip(), message_id=message_id)
    _save(store, 'pending:' + str(owner), value)
    return value


def confirm(store, owner, request_id):
    existing = _load(store, 'outbox:' + request_id)
    if existing:
        if existing['payload']['actor_id'] != owner:
            raise ValueError('Действие другого владельца')
        return existing, False
    value = pending(store, owner)
    if not value or value['request_id'] != request_id or not value['notes']:
        raise ValueError('Это устаревшая кнопка. Укажите ошибку под нужной карточкой заново.')
    if _expired(value):
        cancel(store, owner)
        raise ValueError('Время подтверждения истекло. Выберите карточку заново.')
    row, job_id = _current(store, value['audit_id'])
    if row['sha256'] != value['sha256']:
        raise ValueError('Карточка изменилась; исправление не запущено.')
    previous = feedback(row['report']) if row['report'] else 'Предыдущая проверка ещё не завершена.'
    notes = ('Замечание владельца к предыдущей карточке:\n' + value['notes'] +
             '\n\nПредыдущий отчёт аудитора (для контекста):\n' + previous[:1800])
    payload = {key: value[key] for key in ('request_id', 'audit_id', 'revision', 'sha256', 'brand', 'model', 'actor_id')}
    payload.update(job_id=job_id, notes=notes)
    item = {'payload': payload, 'signature': runtime._signature(store.root.parent.parent, payload), 'status': 'pending'}
    # Save confirmation and consume the dialogue atomically. Repeated callbacks use the same request id.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        inserted = db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',
                              (PREFIX + 'outbox:' + request_id, json.dumps(item, ensure_ascii=False))).rowcount
        db.execute('DELETE FROM meta WHERE key=?', (PREFIX + 'pending:' + str(owner),))
    return _load(store, 'outbox:' + request_id), bool(inserted)


def _keyboard(value):
    return [[{'text': '🔄 Исправить по замечанию', 'callback_data': 'qf:send:' + value['request_id']}],
            [{'text': 'Отмена', 'callback_data': 'qf:cancel:' + value['request_id']}]]


def show_cards(store, tg, owner, page=0):
    if type(page) is not int or not 0 <= page <= 10000:
        return
    with store.connect() as db:
        rows = db.execute("SELECT id FROM audits WHERE status IN ('approved','review','regenerate') "
                          'ORDER BY created DESC').fetchall()
    buttons = []
    skipped = 0
    for saved in rows:
        try:
            row, job_id = _current(store, saved['id'])
        except (ValueError, OSError, AuditHold):
            continue
        if skipped < page * PAGE_SIZE:
            skipped += 1
            continue
        buttons.append([{'text': f"{row['product']['brand']} {row['product']['model']} · №{job_id}"[:100],
                         'callback_data': 'qf:begin:' + row['id']}])
        if len(buttons) > PAGE_SIZE:
            break
    more = len(buttons) > PAGE_SIZE
    buttons = buttons[:PAGE_SIZE]
    navigation = []
    if page:
        navigation.append({'text': '← Назад', 'callback_data': f'qf:page:{page - 1}'})
    if more:
        navigation.append({'text': 'Дальше →', 'callback_data': f'qf:page:{page + 1}'})
    if navigation:
        buttons.append(navigation)
    tg.send(owner, 'Выберите карточку, затем напишите или надиктуйте, что исправить.' if buttons else
            'Пока нет готовых карточек, связанных с генератором.', buttons or None)


def handle_callback(callback, store, tg, owner):
    parts = callback.get('data', '').split(':')
    if parts == ['qf', 'list', 'all'] or (len(parts) == 3 and parts[:2] == ['qf', 'page'] and
                                         parts[2].isdigit() and len(parts[2]) <= 5):
        try:
            tg.answer_callback(callback['id'])
        except Exception:
            pass
        show_cards(store, tg, owner, 0 if parts[1] == 'list' else int(parts[2]))
        return True
    if len(parts) != 3 or parts[0] != 'qf' or not re.fullmatch('[a-f0-9]{32}', parts[2]):
        return False
    try:
        tg.answer_callback(callback['id'])
    except Exception:
        pass
    try:
        if parts[1] == 'begin':
            value = begin(store, parts[2], owner, callback['id'])
            mid = tg.send(owner, f"✏️ Указать ошибку: {value['brand']} {value['model']}, ревизия {value['revision']}.\n"
                          'Напишите замечание или отправьте голосовое сообщение. Например: '
                          '«Это дизельный генератор, убери упоминание бензина».\n'
                          'Сначала покажу текст для проверки. После вашего подтверждения будет запущена одна новая генерация.',
                          [[{'text': 'Отмена', 'callback_data': 'qf:cancel:' + value['request_id']}]])
            value['prompt_message_id'] = mid
            value['control_message_id'] = callback['message']['message_id']
            _save(store, 'pending:' + str(owner), value)
        elif parts[1] == 'cancel':
            tg.send(owner, 'Ввод замечания отменён.' if cancel(store, owner, parts[2]) else 'Это окно ввода уже закрыто.')
        elif parts[1] == 'send':
            receipt, new = confirm(store, owner, parts[2])
            tg.send(owner, 'Замечание подтверждено и сохранено для отправки на исправление. Публикация не запрашивается.'
                    if new else (_result_text(receipt) if receipt.get('status') == 'delivered' else
                                 'Это замечание уже подтверждено и ожидает отправки; повторная генерация не заказывается.'))
        else:
            return False
    except (ValueError, OSError, AuditHold) as exc:
        tg.send(owner, 'Замечание не отправлено: ' + str(exc))
    return True


def handle_message(message, store, tg, owner, transcriber=None):
    value = pending(store, owner)
    if not value:
        return False
    if _expired(value):
        cancel(store, owner)
        tg.send(owner, 'Время ввода замечания истекло. Выберите карточку заново; это сообщение не передано генератору.')
        return True
    text = (message.get('text') or '').strip()
    if text in ('/cancel', 'Отмена'):
        cancel(store, owner)
        tg.send(owner, 'Ввод замечания отменён.')
        return True
    if text.startswith('/'):
        return False
    reply_id = message.get('reply_to_message', {}).get('message_id')
    allowed_replies = [value.get('prompt_message_id'), value.get('control_message_id'),
                       *value.get('feedback_message_ids', [])]
    if reply_id is not None and reply_id not in allowed_replies:
        tg.send(owner, 'Сейчас вводится замечание к другой карточке. Ответьте на приглашение «Указать ошибку» или нажмите «Отмена».')
        return True
    voice = message.get('voice')
    try:
        if voice:
            if voice.get('duration', 0) > 120 or voice.get('file_size', 0) > MAX_VOICE_BYTES:
                raise ValueError('Голосовое замечание должно быть не длиннее двух минут и не больше 10 МБ.')
            data = tg.download_voice(voice['file_id'])
            if len(data) > MAX_VOICE_BYTES:
                raise ValueError('Голосовое замечание должно быть не больше 10 МБ.')
            if transcriber is None:
                transcriber = _transcribe
            try:
                text = (transcriber(data, f"{value['brand']} {value['model']}. Замечание к товарной карточке.") or '').strip()
            except Exception:
                tg.send(owner, 'Не удалось распознать голосовое замечание. Отправьте его ещё раз или напишите текстом; генерация не запускалась.')
                return True
        if not text:
            raise ValueError('Отправьте замечание текстом или голосовым сообщением.')
        value = capture(store, owner, text, message['message_id'])
        preview = (f"Замечание к {value['brand']} {value['model']}:\n\n{value['notes']}\n\n"
                   'Проверьте распознанный текст. Можно прислать исправленный текст или подтвердить кнопкой. '
                   'После подтверждения будет запущена одна новая генерация; публикация не запрашивается.')
        chunks = list(text_chunks(preview))
        mids = value.get('feedback_message_ids', [])
        for i, chunk in enumerate(chunks):
            mid = tg.send(owner, chunk, _keyboard(value) if i == len(chunks) - 1 else None)
            if mid is not None:
                mids.append(mid)
        value['feedback_message_ids'] = mids[-8:]
        _save(store, 'pending:' + str(owner), value)
    except (ValueError, OSError, AuditHold) as exc:
        tg.send(owner, 'Замечание не сохранено: ' + str(exc))
    except Exception:
        tg.send(owner, 'Не удалось получить голосовое замечание. Попробуйте ещё раз или напишите текстом; генерация не запускалась.')
    return True


def _result_text(item):
    payload, result = item['payload'], item.get('result') or {}
    job = result.get('job_id') or result.get('new_job_id')
    if job and result.get('linked') is False:
        return (f'Задание исправления №{job} принято генератором, но его не удалось связать с товаром. '
                + str(result.get('message') or result.get('reason') or 'Нужен разбор состояния карточки.')[:1200])
    if job and not result.get('reason') and result.get('submitted') is not False:
        return (f"🔄 Замечание передано генератору: {payload['brand']} {payload['model']}. "
                f'Задание №{job}. В него включены ваши замечания и контекст предыдущей карточки.')
    return 'Исправление карточки не запущено: ' + str(
        result.get('message') or result.get('reason') or 'проверьте актуальное превью')[:1200]


def _notify_result(store, key, item, tg, now):
    # Records delivered by older releases have no notification marker; avoid
    # replaying their entire history when this version first starts.
    if tg is None or item.get('notified', True) or item.get('notify_retry_at', 0) > now:
        return
    try:
        for part in text_chunks(_result_text(item)):
            tg.send(item['payload']['actor_id'], part)
    except Exception as exc:
        # The render request is already delivered. Retrying the notification
        # must never re-submit the paid operation.
        log.warning('Ответ по замечанию владельца отложен: %s', type(exc).__name__)
        item['notify_retry_at'] = now + 30
    else:
        item['notified'] = True
        item.pop('notify_retry_at', None)
    _save(store, key.removeprefix(PREFIX), item)


def recover_owner_feedback(store, deliver=None, *, tg=None, owner=None, now=None):
    now = time.time() if now is None else now
    if deliver is None:
        token = os.environ.get('ORCH_API_TOKEN')
        if not token:
            return
        import httpx
        from listener.client import ApiClient
        with httpx.Client(timeout=20, trust_env=False) as http:
            api = ApiClient(os.environ.get('ORCH_API_URL', 'http://127.0.0.1:8790'), token, http=http)
            return recover_owner_feedback(store, lambda payload: api._post('/photo-feedback', payload), tg=tg, owner=owner, now=now)
    with store.connect() as db:
        rows = db.execute('SELECT key,value FROM meta WHERE key LIKE ?', (PREFIX + 'outbox:%',)).fetchall()
    for saved in rows:
        try:
            item = json.loads(saved['value'])
        except (ValueError, TypeError):
            log.warning('Повреждённое подтверждение замечания пропущено')
            continue
        if not isinstance(item, dict) or not isinstance(item.get('payload'), dict):
            continue
        payload = item['payload']
        if type(payload.get('actor_id')) is not int:
            continue
        if owner is not None and payload['actor_id'] != owner:
            continue
        if not isinstance(item.get('signature'), str) or not hmac.compare_digest(
                item['signature'], runtime._signature(store.root.parent.parent, payload)):
            log.warning('Неподтверждённое замечание владельца не отправлено')
            continue
        if item.get('status') in ('delivered', 'superseded'):
            _notify_result(store, saved['key'], item, tg, now)
            continue
        if item.get('status') != 'pending' or item.get('retry_at', 0) > now:
            continue
        row = store.get(payload['audit_id'])
        latest = store.latest(row['product_id']) if row else None
        if (not row or not latest or latest['id'] != row['id'] or row['sha256'] != payload['sha256']):
            item.update(status='superseded', notified=False, result={'handled': True, 'reason': 'superseded',
                        'message': 'Исправление старой карточки отменено: появилась новая ревизия. '
                                   'Откройте последнее фото и укажите замечание к нему.'})
            _save(store, saved['key'].removeprefix(PREFIX), item)
            _notify_result(store, saved['key'], item, tg, now)
            continue
        if row['status'] not in ('approved', 'review', 'regenerate'):
            item['retry_at'] = now + 30
            _save(store, saved['key'].removeprefix(PREFIX), item)
            continue
        try:
            _current(store, payload['audit_id'])
            result = deliver(payload)
        except Exception as exc:
            log.warning('Отправка замечания владельца отложена: %s', type(exc).__name__)
            item['failures'] = item.get('failures', 0) + 1
            item['retry_at'] = now + min(300, 30 * 2 ** min(item['failures'] - 1, 4))
            _save(store, saved['key'].removeprefix(PREFIX), item)
            continue
        if not (result.get('ok') or result.get('handled')):
            item['retry_at'] = now + 30
            _save(store, saved['key'].removeprefix(PREFIX), item)
            continue
        item.update(status='delivered', result=result, notified=False)
        _save(store, saved['key'].removeprefix(PREFIX), item)
        _notify_result(store, saved['key'], item, tg, now)
