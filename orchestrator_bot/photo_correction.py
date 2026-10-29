"""One explicit owner correction -> one idempotent card job and a new preview.

The content-factory source is unchanged. Its normal submitter and timer render
the card; a durable reservation prevents the old preview from being approved
while submission is uncertain.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time


CARD_MODES = {'kbt', 'mcp', 'conditioner', 'ready_light', 'ready_tv'}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FIELDS = {'request_id', 'audit_id', 'job_id', 'revision', 'sha256', 'brand', 'model', 'notes', 'actor_id'}


class CorrectionUnavailable(ValueError):
    def __init__(self, reason, *, retry=False):
        super().__init__(reason)
        self.retry = retry


def _unavailable(exc):
    return {'handled': not exc.retry, 'submitted': False, 'reason': str(exc), 'message': str(exc), 'published': False}


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Неверный запрос исправления фотографии')
    for key, maximum in (('job_id', 2**63 - 1), ('actor_id', 2**63 - 1)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError('Нет подтверждённого задания или владельца')
    if type(value['revision']) is not int or not 0 <= value['revision'] <= 2:
        raise ValueError('Неверная ревизия фотографии')
    for key, length in (('request_id', 32), ('audit_id', 32), ('sha256', 64)):
        if not isinstance(value[key], str) or not re.fullmatch('[a-f0-9]{' + str(length) + '}', value[key]):
            raise ValueError('Неверный идентификатор исправления')
    for key, maximum in (('brand', 180), ('model', 180), ('notes', 6000)):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > maximum:
            raise ValueError('Нужны точная номенклатура и замечание владельца')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _connect(path, mode='rw'):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=' + mode, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _named(folder, name):
    if not isinstance(name, str) or not name or Path(name).name != name or '\\' in name:
        return None
    path = Path(folder) / name
    return path if path.is_file() and not path.is_symlink() else None


def _image(path):
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError('Исходная фотография превышает 10 МБ')
    data = path.read_bytes()
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        suffix = '.png'
    elif data.startswith(b'\xff\xd8\xff'):
        suffix = '.jpg'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        suffix = '.webp'
    else:
        raise ValueError('Исходная фотография не PNG/JPEG/WEBP')
    return data, suffix


def _specs(job, value):
    text = (
        f"OWNER_CORRECTION_REQUEST_ID={value['request_id']}\n"
        'Явное исправление владельца: подготовь одну новую готовую карточку. '
        'Замечания владельца ниже имеют приоритет над противоречащими им надписями исходного задания. '
        'Не повторяй указанную ошибку и не заменяй её похожим неподтверждённым обещанием. '
        'При недостатке доказательств удали ошибочную необязательную надпись, '
        'не придумывай характеристики или цену. Сохрани точную модель со всеми суффиксами, '
        'исходное фото товара и правила оформления этого режима. '
        'Служебный отчёт и замечания не печатай на карточке.\n\n'
        'ИСХОДНОЕ ЗАДАНИЕ:\n' + (job.get('specs') or '') + '\n\n'
        'КОНТЕКСТ ПРЕДЫДУЩЕЙ КАРТОЧКИ И ЗАМЕЧАНИЕ ВЛАДЕЛЬЦА:\n' +
        _json({k: value[k] for k in ('job_id', 'audit_id', 'revision', 'sha256', 'brand', 'model', 'notes')})
    )
    if len(text) > 30000:
        raise ValueError('Задание и замечания вместе превышают 30000 символов')
    return text


def _reserve(db, queue_path, output_dir, value, source_dirs, now, verified_supplier=None):
    job_id = value['job_id']
    with closing(_connect(queue_path, 'ro')) as queue:
        row = queue.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise CorrectionUnavailable('Исходное задание не найдено')
        job = dict(row)
        if job['mode'] not in CARD_MODES:
            raise CorrectionUnavailable('Исправление доступно для готовой товарной карточки, не поиска исходника')
        if any(str(job[k] or '').strip().casefold() != value[k].strip().casefold() for k in ('brand', 'model')):
            raise CorrectionUnavailable('Замечание относится к другому товару')
        if job['status'] not in ('done', 'failed'):
            raise CorrectionUnavailable('Задание ещё выполняется, отменено или его состояние неизвестно',
                                        retry=job['status'] in ('pending', 'processing'))
        if job['status'] == 'failed' and (
                'PHOTO_AUDIT_HOLD:' not in (job.get('error_text') or '') or
                'Не подтверждён результат начатой генерации' in (job.get('error_text') or '')):
            raise CorrectionUnavailable('Результат предыдущей генерации неизвестен; новое задание остановлено')
        if job['status'] == 'done':
            old = _named(output_dir, job.get('output_filename'))
            if old is None or old.stat().st_size > MAX_IMAGE_BYTES or hashlib.sha256(old.read_bytes()).hexdigest() != value['sha256']:
                raise CorrectionUnavailable('Предыдущая карточка отличается от выбранного снимка')

    items = db.execute('SELECT * FROM excel_items WHERE card_job=?', (job_id,)).fetchall()
    if len(items) != 1 or items[0]['status'] not in ('preview', 'failed', 'card'):
        raise CorrectionUnavailable('Карточка уже заменена, отменена или опубликована')
    item = dict(items[0])
    if any(str(item[k] or '').strip().casefold() != value[k].strip().casefold() for k in ('brand', 'model')):
        raise CorrectionUnavailable('Номенклатура активного товара изменилась')
    awaiting = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='awaiting'").fetchone():
        awaiting = db.execute('SELECT * FROM awaiting WHERE key=?', (item['key'],)).fetchone()
        if awaiting and awaiting['status'] == 'published':
            raise CorrectionUnavailable('Карточка уже опубликована')
        if awaiting and awaiting['status'] not in ('pending', 'regen'):
            raise CorrectionUnavailable('Превью уже снято с подтверждения')

    source = None
    for column, directory in source_dirs:
        source = _named(directory, job.get(column))
        if source is not None:
            break
    if source is None and item.get('research_job'):
        with closing(_connect(queue_path, 'ro')) as queue:
            research = queue.execute('SELECT status,output_filename,brand,model FROM jobs WHERE id=?', (item['research_job'],)).fetchone()
        if (research and research['status'] == 'done' and
                all(str(research[k] or '').strip().casefold() == value[k].strip().casefold() for k in ('brand', 'model'))):
            source = _named(output_dir, research['output_filename'])
    if source is None and verified_supplier:
        candidate, expected_hash = verified_supplier
        candidate = Path(candidate)
        if (candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= MAX_IMAGE_BYTES and
                hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_hash):
            source = candidate
    if source is None:
        raise CorrectionUnavailable('Нет сохранённого исходного фото товара; ошибочный макет не используется как исходник')
    try:
        data, suffix = _image(source)
        specs = _specs(job, value)
    except ValueError as exc:
        raise CorrectionUnavailable(str(exc)) from None
    folder = Path(db.execute('PRAGMA database_list').fetchone()['file']).parent / 'photo-corrections' / value['request_id']
    folder.mkdir(parents=True, exist_ok=True)
    frozen = folder / ('source' + suffix)
    try:
        with frozen.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        if frozen.read_bytes() != data:
            raise CorrectionUnavailable('Сохранённый исходник исправления изменился') from None
    context = {'item_key': item['key'], 'mode': job['mode'], 'specs': specs,
               'source': str(frozen), 'source_sha256': hashlib.sha256(data).hexdigest()}
    db.execute('INSERT INTO photo_owner_corrections(request_id,payload,context,created) VALUES(?,?,?,?)',
               (value['request_id'], _json(value), _json(context), now))
    db.execute("UPDATE excel_items SET status='correcting',error=?,ts=? WHERE key=? AND card_job=?",
               ('OWNER_CORRECTION_REQUESTED:' + value['request_id'], now, item['key'], job_id))
    if awaiting:
        db.execute("UPDATE awaiting SET status='correcting' WHERE key=? AND status=?", (item['key'], awaiting['status']))
    return context


def correct_photo(state_path, queue_path, value, *, submit_card, output_dir,
                  input_dir=None, archive_dir=None, failed_dir=None, verified_supplier=None, now=None):
    value = validate(value)
    now = time.time() if now is None else now
    base = Path(queue_path).resolve().parent
    source_dirs = [('input_filename', input_dir or base / 'input'),
                   ('archived_filename', archive_dir or base / 'processed'),
                   ('archived_filename', archive_dir or base / 'archive'),
                   ('failed_filename', failed_dir or base / 'failed')]
    with closing(_connect(state_path)) as db:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS photo_owner_corrections '
                       '(request_id TEXT PRIMARY KEY,payload TEXT NOT NULL,context TEXT NOT NULL,result TEXT,created REAL)')
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM photo_owner_corrections WHERE request_id=?', (value['request_id'],)).fetchone()
            if previous:
                if previous['payload'] != _json(value):
                    raise ValueError('Этот запрос исправления уже сохранён с другим содержимым')
                if previous['result']:
                    return {**json.loads(previous['result']), 'duplicate': True}
                context = json.loads(previous['context'])
                item = db.execute('SELECT status,card_job FROM excel_items WHERE key=?', (context['item_key'],)).fetchone()
                if not item or item['status'] != 'correcting' or item['card_job'] != value['job_id']:
                    return _unavailable(CorrectionUnavailable('Товар изменился после запроса исправления; повторная постановка остановлена'))
            else:
                try:
                    context = _reserve(db, queue_path, output_dir, value, source_dirs, now, verified_supplier)
                except CorrectionUnavailable as exc:
                    return _unavailable(exc)

    source = Path(context['source'])
    if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != context['source_sha256']:
        return _unavailable(CorrectionUnavailable('Исходник сохранённого запроса исправления изменился'))
    # A timeout leaves the reservation intact. Retrying the SAME request key
    # reaches the queue's immutable payload deduplication, never a second render.
    new_job = submit_card(value['brand'], value['model'], context['specs'], str(source),
                          context['mode'], request_key='owner-correction-' + value['request_id'])
    if type(new_job) is not int or new_job <= 0 or new_job == value['job_id']:
        raise ValueError('Сервер не подтвердил новое задание исправления')
    with closing(_connect(state_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        completed = db.execute('SELECT result FROM photo_owner_corrections WHERE request_id=?',
                               (value['request_id'],)).fetchone()
        if completed and completed['result']:
            return {**json.loads(completed['result']), 'duplicate': True}
        changed = db.execute("UPDATE excel_items SET status='card',card_job=?,card_request_key=?,"
                             'error=NULL,tries=0,ts=? WHERE key=? AND card_job=? AND status=\'correcting\'',
                             (new_job, 'owner-correction-' + value['request_id'], now, context['item_key'], value['job_id'])).rowcount
        result = {'handled': True, 'submitted': True, 'linked': bool(changed), 'job_id': new_job, 'new_job_id': new_job,
                  'previous_job_id': value['job_id'], 'request_id': value['request_id'], 'published': False,
                  'item_key': context['item_key']}
        if not changed:
            result['reason'] = 'Товар изменился во время постановки; новое задание сохранено отдельно, публикация не разрешена'
        result['message'] = (f'Карточка отправлена на исправление. Задание №{new_job}; результат придёт новым превью.'
                             if changed else result['reason'])
        db.execute('UPDATE photo_owner_corrections SET result=? WHERE request_id=?', (_json(result), value['request_id']))
    return result


def review_scope(cf, args):
    state, _, _ = cf._paths(args)
    with closing(_connect(state, 'ro')) as db:
        rows = db.execute('SELECT key FROM excel_items WHERE card_job=?', (args.job_id,)).fetchall()
    return {'item_key': rows[0]['key'] if len(rows) == 1 else None}


def mirrored_supplier(review_db, value):
    path = Path(review_db)
    if not path.is_file():
        return None
    with closing(_connect(path, 'ro')) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='photo_reviews'").fetchone()
        row = db.execute('SELECT payload FROM photo_reviews WHERE audit_id=?', (value['audit_id'],)).fetchone() if exists else None
    if not row:
        return None
    card = json.loads(row['payload'])
    if any(card.get(k) != value[k] for k in ('job_id', 'revision', 'sha256', 'brand', 'model')):
        return None
    source, fingerprint = card.get('supplier_path'), card.get('supplier_sha256')
    if not source or not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
        return None
    source = Path(source)
    if source.parent.resolve() != (path.parent / 'photo-review-suppliers').resolve():
        return None
    return source, fingerprint


def run(cf, args):
    import httpx
    from content_factory.orchestrator.card_submit import make_card_submitter
    state, _, queue = cf._paths(args)
    api = cf.config('FOTOGEN_API_URL', default='')
    if not api:
        raise ValueError('Не настроен сервер фотоагента')
    output = Path(cf.config('FOTOGEN_OUTPUT_DIR'))
    with httpx.Client(timeout=60, trust_env=False) as http:
        submit = make_card_submitter(api, {'x-agent-token': cf.config('FOTOGEN_API_TOKEN')}, output,
                                    cf.config('TELEGRAM_OWNER_CHAT_ID', default='0'), queue, http=http)
        value = validate(json.loads(args.feedback_json or '{}'))
        original = mirrored_supplier(os.environ.get('ORCH_DB', '/opt/agents-my/state/orchestrator.db'), value)
        return correct_photo(state, queue, value, submit_card=submit, output_dir=output, verified_supplier=original)
