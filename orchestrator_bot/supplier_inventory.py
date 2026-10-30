"""Read and present every photographed row before dispatch filters are applied."""
from collections import Counter
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3

from orchestrator_bot.supplier_import import _decimal, _identity, _text


def normalize_inventory(value):
    allowed = {'rows', 'warnings', 'image_row_counts', 'complete', 'source_sha256', 'vision_passes'}
    if (not isinstance(value, dict) or set(value) - allowed or
            not isinstance(value.get('rows'), list) or not 1 <= len(value['rows']) <= 500):
        raise ValueError('Нужен полный список строк фотографии')
    if type(value.get('complete')) is not bool:
        raise ValueError('Не указана проверка полноты фотографии')
    counts = value.get('image_row_counts')
    if not isinstance(counts, list) or not 1 <= len(counts) <= 8:
        raise ValueError('Не перечислены фотографии источника')
    by_image = {}
    for count in counts:
        if not isinstance(count, dict) or set(count) != {'attachment_index', 'row_count'}:
            raise ValueError('Неверный счётчик строк фотографии')
        index, total = count['attachment_index'], count['row_count']
        if (type(index) is not int or not 0 <= index < len(counts) or index in by_image or
                type(total) is not int or not 0 <= total <= 200):
            raise ValueError('Неверный счётчик строк фотографии')
        by_image[index] = total
    rows, positions = [], set()
    for original in value['rows']:
        if not isinstance(original, dict) or set(original) != {
                'name', 'brand', 'model', 'cost_price', 'stock', 'attachment_index', 'row_index'}:
            raise ValueError('Некорректная строка фотографии')
        row = {k: (_text(original[k], k, 1000 if k == 'name' else 180) if original[k] is not None else None)
               for k in ('name', 'brand', 'model')}
        for key in ('attachment_index', 'row_index'):
            number = original.get(key)
            if (type(number) is not int or
                    not (0 <= number < len(counts) if key == 'attachment_index' else 1 <= number <= 200)):
                raise ValueError('Не указано место строки на фотографии')
            row[key] = number
        position = (row['attachment_index'], row['row_index'])
        if position in positions:
            raise ValueError('Повтор строки на одной фотографии')
        positions.add(position)
        cost = original.get('cost_price')
        row['cost_price'] = (str(_decimal(cost, 'входящая цена', Decimal('0.01'), Decimal('999999999999.99')))
                             if cost is not None else None)
        stock = original.get('stock')
        if stock is not None and (type(stock) is not int or not 0 <= stock <= 1000000000):
            raise ValueError('Непрочитанный остаток должен быть null, а не выдуманным числом')
        row['stock'] = stock
        rows.append(row)
    expected = {(index, ordinal) for index, count in by_image.items() for ordinal in range(1, count + 1)}
    if positions != expected:
        raise ValueError('Список позиций не совпадает с числом строк фотографии')
    warnings = value.get('warnings', [])
    if not isinstance(warnings, list) or len(warnings) > 2500 or any(
            not isinstance(w, str) or not w.strip() or len(w) > 1000 for w in warnings):
        raise ValueError('Некорректные замечания к распознаванию')
    clean = {'rows': sorted(rows, key=lambda row: (row['attachment_index'], row['row_index'])),
             'warnings': warnings, 'complete': value['complete'],
             'image_row_counts': [{'attachment_index': i, 'row_count': by_image[i]} for i in range(len(counts))]}
    if 'source_sha256' in value:
        import re
        hashes = value['source_sha256']
        if (not isinstance(hashes, list) or len(hashes) != len(counts) or
                any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes)):
            raise ValueError('Неверная привязка к фотографиям источника')
        clean['source_sha256'] = list(hashes)
    if 'vision_passes' in value:
        if type(value['vision_passes']) is not int or value['vision_passes'] != 2:
            raise ValueError('Не подтверждены два чтения фотографии')
        clean['vision_passes'] = 2
    return clean


def key_for(row):
    if not row.get('brand') or not row.get('model'):
        return None
    return 'excel|' + _identity(row['brand']) + '|' + _identity(row['model'])


def validate_selection(action, inventory):
    """Bind every dispatch fact back to a verified photographed source row.

    A brand filter may select fewer rows, but cannot rewrite a model suffix,
    incoming price, stock or source title. Queue eligibility is checked separately
    against live state immediately before dispatch.
    """
    from orchestrator_bot.supplier_import import normalize_action
    inventory = normalize_inventory(inventory)
    if not inventory['complete']:
        raise ValueError('Разбор фото не прошёл сверку: генерация остановлена')
    clean = normalize_action(action)
    for item in clean['items']:
        def same_source(row):
            return (row['attachment_index'] == item.get('attachment_index') and
                    row['brand'] is not None and row['model'] is not None and
                    _identity(row['brand']) == _identity(item['brand']) and
                    _identity(row['model']) == _identity(item['model']) and
                    row['cost_price'] is not None and
                    Decimal(row['cost_price']) == Decimal(item['cost_price']) and
                    row['stock'] is not None and row['stock'] > 0 and row['stock'] == item['stock'] and
                    (row['name'] is None or ' '.join(row['name'].split()) == ' '.join(item['name'].split())))

        if not any(same_source(row) for row in inventory['rows']):
            raise ValueError('Товар действия не совпадает с полной транскрипцией фотографии')
    return action


def _read(path, table, keys):
    if path is None:
        return [], False
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return [], False
            rows = []
            keys = list(keys)
            for start in range(0, len(keys), 200):
                batch = keys[start:start + 200]
                rows.extend(dict(row) for row in db.execute(
                    'SELECT * FROM ' + table + ' WHERE key IN (' + ','.join('?' for _ in batch) + ')', batch))
            return rows, True
    except (OSError, sqlite3.Error):
        return [], False


def _queue_rows(path, linked_ids, identities):
    """Avoid materializing queue prompts, image payloads and unrelated history."""
    if path is None:
        return [], False
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                return [], False
            selected = set(linked_ids)
            unmatched = set(identities)
            # Canonical whitespace/unicode normalization is shared with source
            # keys. Stream only identifiers so older variant spellings can match
            # without loading every job or weakening suffix identity.
            if unmatched:
                for row in db.execute('SELECT id,brand,model FROM jobs ORDER BY id DESC'):
                    if not isinstance(row['brand'], str) or not isinstance(row['model'], str):
                        continue
                    identity = (_identity(row['brand']), _identity(row['model']))
                    if identity in unmatched:
                        selected.add(row['id'])
                        unmatched.remove(identity)
                        if not unmatched:
                            break
            rows = []
            ids = list(selected)
            available = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            columns = [name for name in ('id', 'brand', 'model', 'status', 'mode', 'error_text', 'result_sent') if name in available]
            for start in range(0, len(ids), 200):
                batch = ids[start:start + 200]
                rows.extend(dict(row) for row in db.execute(
                    'SELECT ' + ','.join(columns) + ' FROM jobs WHERE id IN (' + ','.join('?' for _ in batch) + ')', batch))
            return rows, True
    except (OSError, sqlite3.Error):
        return [], False


def _current_decision(item, decisions):
    decision = decisions.get(item.get('key'))
    before, after = item.get('ts'), decision.get('ts') if decision else None
    if not decision or before is None or after is None:
        return None
    try:
        return decision if float(after) >= float(before) else None
    except (ValueError, TypeError):
        return None


def _job_state(job, stage):
    status = job.get('status')
    if status == 'pending':
        return 'queued'
    if status == 'processing':
        return 'processing'
    if status == 'failed':
        return 'failed'
    if status == 'cancelled':
        return 'cancelled'
    if status == 'done':
        return 'research_done' if stage == 'research' else 'ready'
    return 'unknown'


def lookup(state_path, queue_path, rows):
    """Only read exact current item/job identities; never infer states from chat."""
    keys = {key_for(row) for row in rows} - {None}
    items, state_available = _read(state_path, 'excel_items', keys)
    decisions, _ = _read(state_path, 'awaiting', keys)
    linked_ids = {item.get(field) for item in items for field in ('card_job', 'research_job')} - {None}
    identities = {(_identity(row['brand']), _identity(row['model'])) for row in rows if key_for(row)}
    jobs, queue_available = _queue_rows(queue_path, linked_ids, identities)
    by_key = {row.get('key'): row for row in items}
    awaiting = {row.get('key'): row for row in decisions}
    by_job = {row['id']: row for row in jobs}
    latest = {}
    for job in sorted(jobs, key=lambda row: row['id']):
        if isinstance(job.get('brand'), str) and isinstance(job.get('model'), str):
            latest[(_identity(job['brand']), _identity(job['model']))] = job
    result = []
    for row in rows:
        key = key_for(row)
        item = by_key.get(key)
        value = {'attachment_index': row['attachment_index'], 'row_index': row['row_index'], 'key': key,
                 'state': 'missing' if state_available and queue_available else 'unavailable', 'stage': None}
        if key is None:
            value['state'] = 'unreadable'
            result.append(value)
            continue
        latest_job = latest.get((_identity(row['brand']), _identity(row['model'])))
        linked = [item.get(field) for field in ('card_job', 'research_job')
                  if item and item.get(field) is not None]
        # A later exact-SKU run can use a cohort key while the canonical
        # item and its publication decision remain as historical records.
        # Those records cannot describe the newer job's current state.
        if latest_job and linked and latest_job['id'] > max(linked):
            item = None
        if item:
            decision = _current_decision(item, awaiting)
            card_job, research_job = item.get('card_job'), item.get('research_job')
            job_id = max(linked) if linked else None
            stage = ('card' if job_id == card_job else 'research') if job_id is not None else None
            job = by_job.get(job_id)
            value.update(origin='factory', item_status=item.get('status'), stage=stage, job_id=job_id)
            if item.get('status') in ('cancelled', 'held'):
                value['state'] = item['status']
            elif decision and decision.get('status') in ('published', 'rejected', 'expired'):
                value['state'] = decision['status']
            elif item.get('status') == 'published':
                value['state'] = 'published'
            elif item.get('status') == 'failed':
                value.update(state='failed', error=item.get('error') or (job or {}).get('error_text'))
            elif job:
                value.update(state=_job_state(job, stage), error=job.get('error_text'))
            elif item.get('status') in ('new', 'research', 'card', 'submission', 'correcting'):
                value['state'] = 'queued' if item.get('status') == 'new' else 'processing'
            elif decision and decision.get('status') == 'pending':
                value['state'] = 'ready'
            else:
                value['state'] = 'unknown'
        else:
            job = latest_job
            if job:
                stage = 'research' if job.get('mode') == 'research' else 'card'
                value.update(origin='queue', state=_job_state(job, stage), stage=stage, job_id=job['id'],
                             error=job.get('error_text'), result_sent=bool(job.get('result_sent')))
        result.append(value)
    return result


_LABELS = {'missing': 'в текущей очереди не найдено', 'unavailable': 'состояние очереди недоступно',
           'unreadable': 'бренд или модель требуют уточнения',
           'queued': 'в очереди', 'processing': 'в работе', 'ready': 'карточка готова',
           'research_done': 'поиск исходника завершён; готовность карточки не подтверждена',
           'failed': 'ошибка', 'cancelled': 'отменено', 'held': 'удержано', 'published': 'опубликовано',
           'rejected': 'отклонено', 'expired': 'превью истекло', 'unknown': 'состояние требует проверки'}


def _number(value):
    number = Decimal(str(value))
    return f'{number:,.2f}'.replace(',', ' ').rstrip('0').rstrip('.')


def render(inventory, statuses=(), *, filter_brand=None, excluded_models=(), markup_percent=None):
    """Show extraction and live state on separate lines; filters never omit rows."""
    inventory = normalize_inventory(inventory)
    rows = inventory['rows']
    by_position = {(s['attachment_index'], s['row_index']): s for s in statuses}
    brands = Counter(row['brand'] or 'бренд не прочитан' for row in rows)
    lines = [f"📷 На фото распознано позиций: {len(rows)} ({'; '.join(f'{k}: {v}' for k,v in brands.items())}).",
             f"Положительный остаток: {sum(row['stock'] is not None and row['stock'] > 0 for row in rows)}; "
             f"нулевой: {sum(row['stock'] == 0 for row in rows)}; "
             f"не указан/не читается: {sum(row['stock'] is None for row in rows)}."]
    if not inventory['complete']:
        lines.append('⚠️ Два чтения фото не совпали. Полнота разбора не подтверждена; перед запуском нужны уточнения.')
    if filter_brand:
        filter_brand = _text(filter_brand, 'бренд отбора', 100)
        lines.append('Отбор для запуска: ' + filter_brand + '. Остальные строки также показаны.')
    if markup_percent is not None:
        markup = _decimal(markup_percent, 'наценка', Decimal(0), Decimal(1000))
        lines.append('Наценка для задания: ' + _number(markup) + '%. Ниже — входящие цены с фото.')
    excluded = {_identity(model) for model in excluded_models}
    for n, row in enumerate(rows, 1):
        price = _number(row['cost_price']) + ' ₽' if row['cost_price'] is not None else 'не читается'
        stock = str(row['stock']) if row['stock'] is not None else 'не указан/не читается — уточнить'
        title = ' '.join(part for part in (row['brand'], row['model']) if part) or row['name'] or 'Название не прочитано'
        lines += ['', f"{n}. {title} — входящая {price}; остаток {stock}."]
        state = by_position.get((row['attachment_index'], row['row_index']), {'state': 'unavailable'})
        status = _LABELS.get(state.get('state'), _LABELS['unknown'])
        if state.get('state') == 'failed' and state.get('stage') == 'research':
            status = 'ошибка поиска исходника'
        if state.get('state') == 'ready' and state.get('origin') == 'queue' and state.get('result_sent'):
            status = 'карточка готова и отправлена'
        if state.get('job_id'):
            status += ' · задание №' + str(state['job_id'])
        annotations = []
        if filter_brand and (not row['brand'] or _identity(row['brand']) != _identity(filter_brand)):
            annotations.append('вне отбора ' + filter_brand)
        if row['model'] and _identity(row['model']) in excluded:
            annotations.append('исключена отдельным условием отбора')
        if not row['brand'] or not row['model'] or row['cost_price'] is None:
            annotations.append('не ставить в генерацию до уточнения реквизитов')
        if row['stock'] is None:
            annotations.append('не ставить в генерацию до уточнения остатка')
        elif row['stock'] == 0:
            annotations.append('на фото указан нулевой остаток')
        lines.append('Состояние: ' + status + ('. ' + '; '.join(annotations) if annotations else '') + '.')
    if inventory['warnings']:
        lines += ['', 'Требует внимания: ' + '; '.join(inventory['warnings'])]
    return '\n'.join(lines)
