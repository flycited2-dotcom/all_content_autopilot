"""Exact saved-card previews shared by the PC auditor and Orchestrator controls."""
import base64
import binascii
import hashlib
import json
import re
import time
from pathlib import Path

from orchestrator_bot.telegram_api import IMAGE_MAX_BYTES, image_mime


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS photo_reviews '
               '(job_id INTEGER PRIMARY KEY,audit_id TEXT UNIQUE,revision INTEGER,payload TEXT,updated REAL)')


def _identity(value):
    if not isinstance(value, dict):
        raise ValueError('Нет данных карточки')
    for key, pattern in [('audit_id', r'[a-f0-9]{32}'), ('sha256', r'[a-f0-9]{64}')]:
        if not isinstance(value.get(key), str) or not re.fullmatch(pattern, value[key]):
            raise ValueError('Неверный снимок карточки')
    if type(value.get('job_id')) is not int or not 0 < value['job_id'] < 2**63:
        raise ValueError('Неверное задание карточки')
    if type(value.get('revision')) is not int or not 0 <= value['revision'] <= 2:
        raise ValueError('Неверная ревизия карточки')
    for key in ('brand', 'model'):
        if not isinstance(value.get(key), str) or not 0 < len(value[key].strip()) <= 180:
            raise ValueError('Нужны точные бренд и модель')


def register(store, value, *, item_key=None):
    _identity(value)
    if value.get('status') not in ('approved', 'review', 'regenerate'):
        raise ValueError('Проверка карточки ещё не завершена')
    summary = value.get('summary', '')
    if not isinstance(summary, str) or len(summary) > 6000:
        raise ValueError('Слишком длинный отчёт карточки')
    encoded = value.get('preview', '')
    if not isinstance(encoded, str) or len(encoded) > 4 * ((IMAGE_MAX_BYTES + 2) // 3):
        raise ValueError('Слишком большое фото карточки')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Повреждено фото карточки') from exc
    if len(data) > IMAGE_MAX_BYTES or hashlib.sha256(data).hexdigest() != value['sha256']:
        raise ValueError('Фото не соответствует проверенному снимку')
    suffix = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}[image_mime(data)]
    folder = store.path.parent / 'photo-reviews'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (value['sha256'] + suffix)
    card = {k: value[k] for k in ('audit_id', 'job_id', 'revision', 'sha256', 'brand', 'model', 'status')}
    card.update(summary=summary, preview_path=str(path))
    if value.get('supplier') is not None:
        encoded_source = value['supplier']
        source_hash = value.get('supplier_sha256')
        if (not isinstance(encoded_source, str) or len(encoded_source) > 4 * ((IMAGE_MAX_BYTES + 2) // 3) or
                not isinstance(source_hash, str) or not re.fullmatch('[a-f0-9]{64}', source_hash)):
            raise ValueError('Некорректный исходник карточки')
        try:
            source_data = base64.b64decode(encoded_source, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('Повреждён исходник карточки') from exc
        if len(source_data) > IMAGE_MAX_BYTES or hashlib.sha256(source_data).hexdigest() != source_hash:
            raise ValueError('Хеш исходника карточки не совпадает')
        source_suffix = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}[image_mime(source_data)]
        source_folder = store.path.parent / 'photo-review-suppliers'
        source_folder.mkdir(parents=True, exist_ok=True)
        source_path = source_folder / (source_hash + source_suffix)
        if not source_path.exists() or hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            temporary_source = source_path.with_suffix(source_suffix + '.tmp')
            temporary_source.write_bytes(source_data)
            temporary_source.replace(source_path)
        card.update(supplier_path=str(source_path), supplier_sha256=source_hash)
    if item_key is not None:
        if not isinstance(item_key, str) or not 0 < len(item_key) <= 1000:
            raise ValueError('Неверный товар карточки')
        card['item_key'] = item_key
    with store._c() as db:
        _schema(db)
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT revision,payload FROM photo_reviews WHERE job_id=?', (card['job_id'],)).fetchone()
        if old:
            previous = json.loads(old['payload'])
            if old['revision'] > card['revision']:
                return {'registered': False, 'reason': 'superseded'}
            if old['revision'] == card['revision'] and previous['sha256'] != card['sha256']:
                raise ValueError('Фото существующей ревизии изменилось')
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != value['sha256']:
            temporary = path.with_suffix(suffix + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(path)
        db.execute('INSERT INTO photo_reviews VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET '
                   'audit_id=excluded.audit_id,revision=excluded.revision,payload=excluded.payload,updated=excluded.updated',
                   (card['job_id'], card['audit_id'], card['revision'], json.dumps(card, ensure_ascii=False), time.time()))
        db.execute('COMMIT')
    return {'registered': True, 'audit_id': card['audit_id']}


def get_card(store, audit_id):
    if not isinstance(audit_id, str) or not re.fullmatch(r'[a-f0-9]{32}', audit_id):
        return None
    with store._c() as db:
        _schema(db)
        row = db.execute('SELECT payload FROM photo_reviews WHERE audit_id=?', (audit_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def list_cards(store, keys=None, limit=12):
    # Batch membership uses exact job IDs when available; the latest-card menu is
    # deliberately independent from publication and cannot approve an old card.
    with store._c() as db:
        _schema(db)
        query = 'SELECT payload FROM photo_reviews ORDER BY job_id DESC'
        rows = db.execute(query).fetchall() if keys is not None else db.execute(
            query + ' LIMIT ?', (max(1, min(int(limit), 50)),)).fetchall()
    cards = [json.loads(row['payload']) for row in rows]
    if keys is not None:
        wanted = set(keys)
        def belongs(card):
            if card.get('item_key'):
                return card['item_key'] in wanted
            prefix = 'excel|' + card['brand'].strip().casefold() + '|'
            return any(key == prefix + card['model'].strip().casefold() for key in wanted)
        cards = [card for card in cards if belongs(card)]
    return cards[:max(1, min(int(limit), 50))]


def create_feedback(card, notes, actor_id, request_id):
    _identity(card)
    if not isinstance(notes, str) or not notes.strip() or len(notes) > 6000:
        raise ValueError('Замечание должно содержать от 1 до 6000 символов')
    if type(actor_id) is not int or actor_id <= 0 or not re.fullmatch(r'[a-f0-9]{32}', str(request_id)):
        raise ValueError('Нет подтверждённого действия владельца')
    return {**{k: card[k] for k in ('audit_id', 'job_id', 'revision', 'sha256', 'brand', 'model')},
            'notes': notes.strip(), 'actor_id': actor_id, 'request_id': request_id}


def correct(store, factory, value):
    if not isinstance(value, dict):
        raise ValueError('Нет замечания к карточке')
    card = get_card(store, value.get('audit_id'))
    if card is None:
        with store._c() as db:
            _schema(db)
            newer = db.execute('SELECT audit_id FROM photo_reviews WHERE job_id=?', (value.get('job_id'),)).fetchone()
        if newer:
            return {'handled': True, 'submitted': False, 'reason': 'superseded',
                    'message': 'Уже появилась новая ревизия. Укажите замечание под актуальной карточкой.'}
        raise ValueError('Карточка ещё не синхронизирована либо уже появилась новая ревизия')
    expected = create_feedback(card, value.get('notes'), value.get('actor_id'), value.get('request_id'))
    if value != expected:
        raise ValueError('Замечание относится к другому снимку карточки')
    return factory.correct_photo(expected)
