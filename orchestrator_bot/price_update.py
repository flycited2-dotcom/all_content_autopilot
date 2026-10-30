"""One explicit price decision -> one replacement card and a new preview.

The existing composite stays immutable. A durable reservation freezes the
original reference and stops approval of the old preview while submission is
uncertain. Request-key recovery never buys a second render.
"""
from contextlib import closing
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from orchestrator_bot.photo_correction import CARD_MODES, MAX_IMAGE_BYTES, _connect, _image, _named

FIELDS = {'request_id', 'job_id', 'brand', 'model', 'old_price', 'new_price', 'actor_id'}


class PriceUpdateUnavailable(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _price(value):
    if type(value) not in (str, int):
        raise ValueError('Цена должна быть положительным целым числом рублей')
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 < number <= 1_000_000_000 and number == number.to_integral_value():
            return int(number)
    except InvalidOperation:
        pass
    raise ValueError('Цена должна быть положительным целым числом рублей')


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Неверный запрос обновления цены')
    if not isinstance(value['request_id'], str) or not re.fullmatch('[a-f0-9]{32}', value['request_id']):
        raise ValueError('Неверный идентификатор обновления цены')
    for name in ('job_id', 'actor_id'):
        if type(value[name]) is not int or not 0 < value[name] < 2**63:
            raise ValueError('Не подтверждена карточка или владелец')
    for name in ('brand', 'model'):
        if (not isinstance(value[name], str) or not value[name].strip() or len(value[name]) > 180 or
                any(ord(char) < 32 for char in value[name])):
            raise ValueError('Нужны точные бренд и модель')
    result = {**value, 'old_price': str(_price(value['old_price'])), 'new_price': str(_price(value['new_price']))}
    if result['old_price'] == result['new_price']:
        raise ValueError('Цена не изменилась; новая карточка не требуется')
    return result


def _identity(brand, model):
    # A supplier may omit the space before DR/DVR; preserve every suffix char.
    return str(brand).strip().casefold(), re.sub(r'\s+', '', str(model)).casefold()


def specifications(previous, value):
    """Keep product evidence while replacing price fields, never applying markup."""
    target = _price(value['new_price'])
    def rewrite(item):
        if isinstance(item, dict):
            return {key: (target if str(key).casefold() in {'price', 'selling_price', 'цена', 'стоимость'}
                          else rewrite(val)) for key, val in item.items()}
        if isinstance(item, list):
            return [rewrite(item) for item in item]
        return item
    try:
        parsed = json.loads(previous or '')
        text = _json(rewrite(parsed)) if isinstance(parsed, (dict, list)) else str(previous or '')
    except (ValueError, TypeError):
        text = str(previous or '')
    # Textual task templates commonly put the selling price on a labelled line.
    text = re.sub(r'(?i)(?<![\w])((?:цена|стоимость|selling_price|price)["\']?\s*[:=]?\s*)'
                  r'\d(?:[\d \u00a0]*\d)?(?:[.,]\d{1,2})?', lambda m: m[1] + str(target), text)
    # Remove the old monetary value even if the previous template omitted its
    # label, without touching equal wattages, dimensions, stock or model digits.
    old = str(_price(value['old_price']))
    grouped = r'[ \u00a0]*'.join(re.escape(c) for c in old)
    text = re.sub(r'(?<!\d)' + grouped + r'(?:[.,]00)?(?=\s*(?:₽|руб\b|рублей\b|рубля\b))',
                  str(target), text, flags=re.I)
    result = ('OWNER_PRICE_CHANGE_REQUEST_ID=' + value['request_id'] + '\n'
              'Явное изменение цены владельцем. Подготовь одну новую карточку по сохранённому исходному фото. '
              'Точная модель и подтверждённые характеристики сохраняются. '
              'Итоговая продажная цена: ' + str(target) + ' рублей. Наценку повторно не добавляй. '
              'Печатай только эту цену. Если прежнее задание содержит другую цену, новая цена имеет приоритет. '
              'После генерации нужен новый просмотр владельцем; публикация этим поручением не разрешена.\n'
              'ИСХОДНЫЕ ХАРАКТЕРИСТИКИ:\n' + text + '\n'
              'ПОДТВЕРЖДЁННАЯ ЦЕНА:\n' + _json({'brand': value['brand'], 'model': value['model'],
                                                               'selling_price': target}))
    if len(result) > 30000:
        raise PriceUpdateUnavailable('Исходное задание слишком длинное для безопасной замены цены')
    return result


def _failure(reason):
    return {'handled': True, 'submitted': False, 'linked': False, 'published': False,
            'reason': str(reason), 'message': str(reason)}


def _mirrored_source(review_db, job):
    if review_db is None or not Path(review_db).is_file():
        return None
    with closing(_connect(review_db, 'ro')) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='photo_reviews'").fetchone():
            return None
        for row in db.execute('SELECT payload FROM photo_reviews ORDER BY rowid DESC'):
            try:
                value = json.loads(row['payload'])
                if (value.get('job_id') != job['id'] or
                        _identity(value.get('brand'), value.get('model')) != _identity(job['brand'], job['model'])):
                    continue
                path, digest = Path(value['supplier_path']), value['supplier_sha256']
                if (path.parent.resolve() == (Path(review_db).resolve().parent / 'photo-review-suppliers') and
                        path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_IMAGE_BYTES and
                        hashlib.sha256(path.read_bytes()).hexdigest() == digest):
                    return path
            except (ValueError, TypeError, KeyError, OSError):
                continue
    return None


def _reserve(db, queue_path, output_dir, value, now, source_dirs, review_db):
    with closing(_connect(queue_path, 'ro')) as queue:
        row = queue.execute('SELECT * FROM jobs WHERE id=?', (value['job_id'],)).fetchone()
        if row is None:
            raise PriceUpdateUnavailable('Исходная карточка не найдена')
        job = dict(row)
        if job.get('status') != 'done' or job.get('mode') not in CARD_MODES:
            raise PriceUpdateUnavailable('Карточка ещё не готова или уже отменена')
        if _identity(job.get('brand'), job.get('model')) != _identity(value['brand'], value['model']):
            raise PriceUpdateUnavailable('Выбрана другая модель карточки')
        if any(_identity(candidate['brand'], candidate['model']) == _identity(job['brand'], job['model'])
               for candidate in queue.execute('SELECT brand,model FROM jobs WHERE id>?', (job['id'],))):
            raise PriceUpdateUnavailable('Для этой модели уже существует более новое задание; используйте актуальную карточку')
        old_card = _named(output_dir, job.get('output_filename'))
        if old_card is None or old_card.stat().st_size > MAX_IMAGE_BYTES:
            raise PriceUpdateUnavailable('Не найден сохранённый снимок готовой карточки')
        items = db.execute('SELECT * FROM excel_items WHERE card_job=?', (job['id'],)).fetchall()
        if len(items) != 1:
            raise PriceUpdateUnavailable('Карточка уже заменена или нет единственного активного товара')
        item = dict(items[0])
        if (item['status'] != 'preview' or
                _identity(item['brand'], item['model']) != _identity(job['brand'], job['model'])):
            raise PriceUpdateUnavailable('Карточка уже изменена, опубликована или не ожидает решения')
        if _price(item['price']) != _price(value['old_price']):
            raise PriceUpdateUnavailable('Сохранённая цена изменилась после сверки; обновите список')
        awaiting = db.execute('SELECT * FROM awaiting WHERE key=?', (item['key'],)).fetchone()
        if not awaiting or awaiting['status'] != 'pending':
            raise PriceUpdateUnavailable('Превью уже опубликовано, снято с проверки или изменяется')
        source = None
        for column, directory in source_dirs:
            source = _named(directory, job.get(column))
            if source:
                break
        if source is None and item.get('research_job'):
            research = queue.execute('SELECT * FROM jobs WHERE id=?', (item['research_job'],)).fetchone()
            if (research and research['status'] == 'done' and
                    _identity(research['brand'], research['model']) == _identity(job['brand'], job['model'])):
                source = _named(output_dir, research['output_filename'])
    source = source or _mirrored_source(review_db, job)
    if source is None:
        raise PriceUpdateUnavailable('Нет сохранённого исходного фото товара; новая генерация остановлена')
    try:
        data, suffix = _image(source)
    except ValueError as error:
        raise PriceUpdateUnavailable(str(error)) from None
    canonical = {**value, 'brand': job['brand'], 'model': job['model']}
    folder = Path(db.execute('PRAGMA database_list').fetchone()['file']).parent / 'price-updates' / value['request_id']
    folder.mkdir(parents=True, exist_ok=True)
    frozen = folder / ('source' + suffix)
    previous = folder / 'previous-card'
    from orchestrator_bot.supplier_catalog import _immutable
    _immutable(frozen, data)
    _immutable(previous, old_card.read_bytes())
    context = {'item_key': item['key'], 'brand': job['brand'], 'model': job['model'], 'mode': job['mode'],
               'specs': specifications(job.get('specs'), canonical), 'source': str(frozen),
               'source_sha256': hashlib.sha256(data).hexdigest(),
               'previous_card_sha256': hashlib.sha256(previous.read_bytes()).hexdigest(),
               'previous_item': item, 'previous_awaiting': dict(awaiting)}
    db.execute('INSERT INTO price_card_updates(request_id,payload,context,created) VALUES(?,?,?,?)',
               (value['request_id'], _json(value), _json(context), now))
    db.execute("UPDATE excel_items SET status='correcting',error=?,ts=? WHERE key=? AND card_job=?",
               ('OWNER_PRICE_UPDATE:' + value['request_id'], now, item['key'], value['job_id']))
    db.execute("UPDATE awaiting SET status='correcting' WHERE key=? AND status='pending'", (item['key'],))
    return context


def update_price(state_path, queue_path, value, *, submit_card, output_dir, input_dir=None,
                 archive_dir=None, failed_dir=None, review_db=None, now=None):
    value = validate(value)
    now = time.time() if now is None else now
    base = Path(queue_path).resolve().parent
    source_dirs = [('input_filename', input_dir or base / 'input'),
                   ('archived_filename', archive_dir or base / 'processed'),
                   ('archived_filename', archive_dir or base / 'archive'),
                   ('failed_filename', failed_dir or base / 'failed')]
    accepted = None
    with closing(_connect(state_path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS price_card_updates '
                   '(request_id TEXT PRIMARY KEY,payload TEXT NOT NULL,context TEXT NOT NULL,result TEXT,created REAL)')
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM price_card_updates WHERE request_id=?', (value['request_id'],)).fetchone()
        if previous:
            if previous['payload'] != _json(value):
                raise ValueError('Этот запрос цены уже сохранён с другими данными')
            if previous['result']:
                return {**json.loads(previous['result']), 'duplicate': True}
            context = json.loads(previous['context'])
            with closing(_connect(queue_path, 'ro')) as queue:
                found = queue.execute('SELECT * FROM jobs WHERE request_key=?', ('owner-price-' + value['request_id'],)).fetchone()
            if found:
                if (found['mode'] != context['mode'] or found['specs'] != context['specs'] or
                        (found['brand'], found['model']) != (context['brand'], context['model']) or
                        found['status'] == 'cancelled'):
                    return _failure('Принятое обновление цены изменилось; повторная генерация остановлена')
                accepted = found['id']
            item = db.execute('SELECT status,card_job,price,error FROM excel_items WHERE key=?', (context['item_key'],)).fetchone()
            if (not item or item['status'] != 'correcting' or item['card_job'] != value['job_id'] or
                    _price(item['price']) != _price(value['old_price']) or
                    item['error'] != 'OWNER_PRICE_UPDATE:' + value['request_id']):
                if accepted is not None:
                    result = {'handled': True, 'submitted': True, 'linked': False, 'job_id': accepted,
                              'old_job_id': value['job_id'], 'published': False, 'request_id': value['request_id'],
                              'item_key': context['item_key'], 'new_price': value['new_price'],
                              'message': f'Задание №{accepted} уже принято, но товар изменился. '
                                         'Привязка требует проверки; повторная генерация и публикация не разрешены.'}
                    db.execute('UPDATE price_card_updates SET result=? WHERE request_id=?',
                               (_json(result), value['request_id']))
                    return result
                return _failure('Товар изменился после запроса цены; повторная генерация остановлена')
        else:
            try:
                context = _reserve(db, queue_path, output_dir, value, now, source_dirs, review_db)
            except PriceUpdateUnavailable as error:
                return _failure(error)
    source = Path(context['source'])
    if accepted is None:
        if (not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_IMAGE_BYTES or
                hashlib.sha256(source.read_bytes()).hexdigest() != context['source_sha256']):
            return _failure('Сохранённый исходник обновления цены изменился')
        accepted = submit_card(context['brand'], context['model'], context['specs'], str(source), context['mode'],
                               request_key='owner-price-' + value['request_id'])
    if type(accepted) is not int or accepted <= 0 or accepted == value['job_id']:
        raise ValueError('Не подтверждено новое задание цены')
    with closing(_connect(state_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT result FROM price_card_updates WHERE request_id=?', (value['request_id'],)).fetchone()
        if prior and prior['result']:
            return {**json.loads(prior['result']), 'duplicate': True}
        changed = db.execute("UPDATE excel_items SET price=?,status='card',card_job=?,card_request_key=?,"
                             'error=NULL,tries=0,ts=? WHERE key=? AND card_job=? AND status=\'correcting\' AND error=? AND price=?',
                             (_price(value['new_price']), accepted, 'owner-price-' + value['request_id'], now,
                              context['item_key'], value['job_id'], 'OWNER_PRICE_UPDATE:' + value['request_id'],
                              _price(value['old_price']))).rowcount
        result = {'handled': True, 'submitted': True, 'linked': bool(changed), 'job_id': accepted,
                  'old_job_id': value['job_id'], 'published': False, 'request_id': value['request_id'],
                  'item_key': context['item_key'], 'new_price': value['new_price'],
                  'message': (f'Цена {_price(value["new_price"])} ₽ передана в новую карточку. Задание №{accepted}; '
                              'результат придёт новым превью для подтверждения.' if changed else
                              f'Задание №{accepted} принято, но товар изменился во время передачи. '
                              'Привязка требует проверки; публикация не разрешена.')}
        db.execute('UPDATE price_card_updates SET result=? WHERE request_id=?', (_json(result), value['request_id']))
    return result


def run(cf, args):
    import httpx
    from content_factory.orchestrator.card_submit import make_card_submitter
    state, _, queue = cf._paths(args)
    value = validate(json.loads(args.feedback_json or '{}'))
    owner = int(os.environ.get('ORCH_OWNER_CHAT_ID') or cf.config('TELEGRAM_OWNER_CHAT_ID', default='0'))
    if value['actor_id'] != owner:
        raise ValueError('Изменение цены разрешено только владельцу')
    api = cf.config('FOTOGEN_API_URL', default='')
    if not api:
        raise ValueError('Не настроен сервер фотоагента')
    if Decimal(str(cf.config('EXCEL_MARKUP_PCT', default='0'))) != 0:
        raise ValueError('В конвейере включена дополнительная наценка. Обновление конечной цены остановлено')
    output = Path(cf.config('FOTOGEN_OUTPUT_DIR'))
    with httpx.Client(timeout=60, trust_env=False) as http:
        submit = make_card_submitter(api, {'x-agent-token': cf.config('FOTOGEN_API_TOKEN')}, output,
                                    str(owner), queue, http=http)
        return update_price(state, queue, value, submit_card=submit, output_dir=output,
                            review_db=os.environ.get('ORCH_DB', '/opt/agents-my/state/orchestrator.db'))
