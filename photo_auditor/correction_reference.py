"""A separately verified full-product reference never rewrites an old snapshot."""
import hmac
import json
import re
from pathlib import Path

from listener.pending import write_json
from photo_auditor import runtime
from photo_auditor.images import digest, image_bytes
from photo_auditor.sources import canonical


def folder(root, audit_id):
    if not isinstance(audit_id, str) or not re.fullmatch('[a-f0-9]{32}', audit_id):
        raise ValueError('Неверный снимок исправления')
    return Path(root) / 'runs/photo-auditor/correction-references' / audit_id


def stamp(root, audit_id):
    path = folder(root, audit_id) / 'reference.json'
    try:
        return digest(path.read_bytes()) if path.exists() else None
    except OSError:
        return 'unreadable'


def save(root, row, path, *, page_url, image_url, note, main_product_verified=False):
    if main_product_verified is not True:
        raise ValueError('Сначала нужно проверить, что эталон показывает основной товар целиком')
    if not isinstance(note, str) or not 0 < len(note) <= 6000:
        raise ValueError('Нет объяснения выбора эталона')
    brief = runtime.content_brief(root, row['product'])
    if brief is None:
        raise ValueError('Нет подписанного задания карточки')
    data = image_bytes(Path(path).read_bytes())
    payload = {'audit_id': row['id'], 'product': row['product'], 'sha256': row['sha256'],
               'reference_sha256': digest(data), 'page_url': canonical(page_url), 'image_url': canonical(image_url),
               'note': note, 'verification': 'exact_model_full_product_visual_review'}
    target = folder(root, row['id'])
    target.mkdir(parents=True, exist_ok=True)
    (target / 'reference.bin').write_bytes(data)
    write_json(target / 'reference.json', {'payload': payload, 'signature': runtime._signature(root, payload)})
    return payload


def read(root, row):
    target = folder(root, row['id'])
    if not (target / 'reference.json').exists():
        return None
    value = json.loads((target / 'reference.json').read_text(encoding='utf-8'))
    payload = value['payload']
    if (not hmac.compare_digest(value['signature'], runtime._signature(root, payload)) or
            payload['audit_id'] != row['id'] or payload['product'] != row['product'] or
            payload['sha256'] != row['sha256'] or payload['verification'] != 'exact_model_full_product_visual_review'):
        raise ValueError('Эталон относится к другой карточке либо изменён')
    data = image_bytes((target / 'reference.bin').read_bytes())
    if digest(data) != payload['reference_sha256']:
        raise ValueError('Фотография эталона изменена')
    return {**payload, 'data': data}
