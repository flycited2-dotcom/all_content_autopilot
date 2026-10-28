"""Resume an existing failed card once, never create a new paid photo job."""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


def validate(value, *, allow_review=False):
    """allow_review: the advisory auditor passes doubtful photos on to the owner's preview."""
    fields = {'job_id', 'audit_id', 'attempt', 'revision', 'status', 'sha256', 'brand', 'model'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Неверное сообщение фотоаудита')
    for key, minimum, maximum in [('job_id', 1, 2**63 - 1), ('attempt', 0, 1000), ('revision', 0, 2)]:
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError('Неверный номер проверки')
    allowed = ('approved', 'regenerate', 'review') if allow_review else ('approved', 'regenerate')
    if value['status'] not in allowed or (value['revision'] == 2 and value['status'] == 'regenerate'):
        raise ValueError('Решение не разрешает продолжение')
    for key, pattern in [('audit_id', '[a-f0-9]{32}'), ('sha256', '[a-f0-9]{64}')]:
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise ValueError('Неверный идентификатор проверки')
    if any(not isinstance(value[k], str) or not 0 < len(value[k]) <= 180 for k in ('brand', 'model')):
        raise ValueError('Нужна точная номенклатура')
    return value


def reconcile(state_path, queue_path, value):
    value = validate(value, allow_review=True)
    receipt = value['audit_id'] + ':' + str(value['attempt'])
    # Wrong configuration must not create an empty replacement database.
    state_uri = Path(state_path).resolve().as_uri() + '?mode=rw'
    queue_uri = Path(queue_path).resolve().as_uri() + '?mode=rw'
    with sqlite3.connect(state_uri, uri=True, timeout=20) as db:
        db.execute('ATTACH DATABASE ? AS photo_queue', (queue_uri,))
        db.execute('CREATE TABLE IF NOT EXISTS photo_audit_recoveries (event TEXT PRIMARY KEY,result TEXT)')
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT result FROM photo_audit_recoveries WHERE event=?', (receipt,)).fetchone()
        if previous:
            return {**json.loads(previous[0]), 'duplicate': True}
        job = db.execute('SELECT status,mode,brand,model,error_text FROM photo_queue.jobs WHERE id=?',
                         (value['job_id'],)).fetchone()
        if not job:
            return {'handled': True, 'reason': 'job_missing'}
        identity = (value['brand'].strip().casefold(), value['model'].strip().casefold())
        if identity != (str(job[2]).strip().casefold(), str(job[3]).strip().casefold()):
            raise ValueError('Номенклатура задания не совпадает с проверкой')
        if job[1] not in ('kbt', 'mcp', 'conditioner', 'ready_light', 'ready_tv', 'research'):
            return {'handled': True, 'reason': 'not_a_card'}
        if job[0] in ('pending', 'processing'):
            return {'handled': False, 'reason': 'in_flight'}
        if job[0] != 'failed':
            return {'handled': True, 'reason': 'already_finished'}
        stage = 'research' if job[1] == 'research' else 'card'
        item = db.execute('SELECT key,status FROM excel_items WHERE ' + stage + '_job=?', (value['job_id'],)).fetchall()
        if len(item) != 1 or item[0][1] not in ('failed', stage):
            return {'handled': True, 'reason': 'not_an_active_factory_item'}
        if not any(marker in (job[4] or '') for marker in (
                'Фото остановлено для ручной проверки:', 'Контроль ещё не завершён;',
                'Служба контроля недоступна; фото не выгружено', 'PHOTO_AUDIT_HOLD:')):
            return {'handled': True, 'reason': 'unrelated_failure'}
        db.execute("UPDATE photo_queue.jobs SET status='pending',claimed_by=NULL,claimed_at=NULL,"
                   'error_text=NULL,updated_at=? WHERE id=? AND status=\'failed\'',
                   (datetime.now().isoformat(), value['job_id']))
        db.execute('UPDATE excel_items SET status=?,error=NULL WHERE key=? AND ' + stage + '_job=?',
                   (stage, item[0][0], value['job_id']))
        result = {'handled': True, 'resumed': True, 'job_id': value['job_id'], 'new_job_created': False,
                  'item_key': item[0][0]}
        db.execute('INSERT INTO photo_audit_recoveries VALUES(?,?)', (receipt, json.dumps(result)))
    return result
