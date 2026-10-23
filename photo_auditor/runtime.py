"""Signed job scope closes card, research and replay upload routes."""
import hashlib
import hmac
import json
from pathlib import Path

from listener.pending import write_json
from photo_auditor.credentials import load
from photo_auditor.integration import AuditHold, require_permit

PRODUCT_MODES = {'kbt', 'mcp', 'conditioner', 'ready_light', 'ready_tv', 'research'}


def _path(root, job_id):
    if type(job_id) is not int or job_id < 1:
        raise AuditHold('Нет корректного номера задания')
    return Path(root) / 'runs/photo-auditor/job-scope' / f'{job_id}.json'


def _signature(root, value):
    return hmac.new(load(root)['api_token'].encode(),
                    json.dumps(value, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


def register_job(root, job_id, mode, brand, model, specs):
    editorial = mode == 'research' and not brand and (specs or '').strip() == 'editorial'
    required = not editorial and mode not in ('ritual', 'wreath')
    payload = {'job_id': job_id, 'mode': mode, 'brand': brand or '', 'model': model or '',
               'required': required, 'editorial': editorial}
    path = _path(root, job_id)
    if path.exists():
        old = _read(root, job_id)
        if old != payload:
            raise AuditHold('Изменилась номенклатура уже зарегистрированного задания')
    else:
        write_json(path, {'payload': payload, 'signature': _signature(root, payload)})
    if required and (mode not in PRODUCT_MODES or not payload['brand'].strip() or not payload['model'].strip()):
        raise AuditHold('Для товарной фотографии нужны точные бренд и модель; требуется ручной разбор')
    return required


def _read(root, job_id):
    try:
        value = json.loads(_path(root, job_id).read_text(encoding='utf-8'))
        payload = value['payload']
        if payload['job_id'] == job_id and hmac.compare_digest(value['signature'], _signature(root, payload)):
            return payload
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise AuditHold('Не подтверждён режим контроля задания; старый outbox требует разбора')


def requires_audit(root, job_id):
    return _read(root, job_id)['required']


def guard_upload(root, job_id, image):
    if requires_audit(root, job_id):
        if image is None:
            raise AuditHold('У товарного задания нет фотографии для контроля')
        require_permit(root, f'fotogen-{job_id}', image)
