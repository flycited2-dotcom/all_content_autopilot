"""Signed job scope closes card, research and replay upload routes."""
import hashlib
import hmac
import json
from pathlib import Path

from listener.pending import write_json
from photo_auditor.credentials import load
from photo_auditor.integration import AuditHold, require_permit

PRODUCT_MODES = {'kbt', 'mcp', 'conditioner', 'ready_light', 'ready_tv', 'research'}
CARD_MODES = PRODUCT_MODES - {'research'}
CONTENT_POLICY = 'content-manager-1'  # Stable upload contract; report.rules_version versions editorial rules.


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
    if required and mode in CARD_MODES:
        if not isinstance(specs, (str, type(None))) or len(specs or '') > 30000:
            raise AuditHold('Некорректное задание для проверки содержания карточки')
        brief = {'mode': mode, 'brand': payload['brand'], 'model': payload['model'], 'specs': specs or ''}
        brief_path = path.with_name(f'{job_id}.brief.json')
        signed = {'payload': brief, 'signature': _signature(root, {'job_id': job_id, 'brief': brief})}
        if brief_path.exists():
            if json.loads(brief_path.read_text(encoding='utf-8')) != signed:
                raise AuditHold('Изменилось задание уже зарегистрированной карточки')
        else:
            write_json(brief_path, signed)
    return required


def content_brief(root, product):
    """Keep display nomenclature and the original generation brief immutable."""
    import re
    match = re.fullmatch(r'fotogen-([1-9][0-9]*)', product['product_id'])
    if not match or not _path(root, int(match[1])).exists():
        return None
    scope = _read(root, int(match[1]))
    if scope['mode'] not in CARD_MODES:
        return None
    if not scope['required'] or any(scope[k] != product[k] for k in ('brand', 'model')):
        raise AuditHold('Задание контент-контроля относится к другому товару')
    try:
        signed = json.loads(_path(root, int(match[1])).with_name(f'{match[1]}.brief.json').read_text(encoding='utf-8'))
        brief = signed['payload']
        if (set(brief) != {'mode', 'brand', 'model', 'specs'} or
                any(brief[k] != scope[k] for k in ('mode', 'brand', 'model')) or
                not isinstance(brief['specs'], str) or len(brief['specs']) > 30000 or
                not hmac.compare_digest(signed['signature'], _signature(root, {'job_id': int(match[1]), 'brief': brief}))):
            raise ValueError('Invalid brief')
        from photo_auditor.presentation import read
        presentation = read(root, int(match[1]))
        return {**brief, 'presentation': presentation} if presentation is not None else brief
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuditHold('Нет подтверждённого задания для проверки текста и оформления карточки') from exc


def brief_digest(brief):
    return hashlib.sha256(json.dumps(brief, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def approval_covers_brief(root, product, report):
    import re
    match = re.fullmatch(r'fotogen-([1-9][0-9]*)', product['product_id'])
    if match and not _path(root, int(match[1])).exists():
        return False
    try:
        brief = content_brief(root, product)
    except AuditHold:
        return False
    if (report or {}).get('owner_approval'):
        from photo_auditor.owner_decisions import valid
        return report.get('verdict') == 'approved' and valid(root, product, report)
    if ((report or {}).get('content_review') or {}).get('reviewer') == 'owner':
        return False
    if brief is None:
        return True
    review = (report or {}).get('content_review') or {}
    return (review.get('policy_version') == CONTENT_POLICY and review.get('verdict') == 'approved' and
            review.get('brief_sha256') == brief_digest(brief))


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
