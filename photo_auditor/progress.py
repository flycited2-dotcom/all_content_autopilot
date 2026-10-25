"""Restart checkpoints. Unknown paid generation outcomes never start another render."""
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image

from listener.pending import write_json
from photo_auditor.images import digest, image_bytes


def folder(root, product_id):
    if not isinstance(product_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', product_id):
        raise ValueError('Неверный идентификатор восстановления')
    key = hashlib.sha256(product_id.encode()).hexdigest()[:32]
    return Path(root) / 'runs/photo-auditor/recovery' / key


def starting(root, product_id, brand, model, revision, *, paid=True):
    path = folder(root, product_id)
    write_json(path / 'state.json', {'product_id': product_id, 'brand': brand, 'model': model,
                                   'revision': revision, 'phase': 'generating' if paid else 'composing'})


def rejected(root, product_id, brand, model):
    """An explicit quota rejection confirms that the initial render did not start."""
    write_json(folder(root, product_id) / 'state.json', {'product_id': product_id,
        'brand': brand, 'model': model, 'revision': 0, 'phase': 'rejected'})


def _save_supplier(root, product_id, brand, model, source):
    data = image_bytes(Path(source).read_bytes())
    path = folder(root, product_id)
    path.mkdir(parents=True, exist_ok=True)
    temporary = path / 'supplier.bin.tmp'
    temporary.write_bytes(data)
    temporary.replace(path / 'supplier.bin')
    write_json(path / 'supplier.json', {'product_id': product_id, 'brand': brand, 'model': model,
                                      'sha256': digest(data)})


def _supplier(root, product_id, brand, model, latest):
    from photo_auditor.integration import AuditHold
    path = folder(root, product_id)
    manifest = path / 'supplier.json'
    if manifest.exists():
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if any(value[k] != v for k, v in [('product_id', product_id), ('brand', brand), ('model', model)]):
            raise AuditHold('Номенклатура исходного фото изменилась')
        if digest(image_bytes((path / 'supplier.bin').read_bytes())) != value['sha256']:
            raise AuditHold('Сохранённый исходник изменился')
        return path / 'supplier.bin'
    if latest:
        old = Path(root) / 'runs/photo-auditor' / latest['id'] / 'supplier.bin'
        if old.exists():
            _save_supplier(root, product_id, brand, model, old)
            return path / 'supplier.bin'
    return None


def generated(root, product_id, brand, model, revision, source, supplier=None):
    data = image_bytes(Path(source).read_bytes())
    path = folder(root, product_id)
    path.mkdir(parents=True, exist_ok=True)
    if supplier is not None and not (path / 'supplier.json').exists():
        _save_supplier(root, product_id, brand, model, supplier)
    with Image.open(io.BytesIO(data)) as image:
        suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[image.format]
    image_path = path / f'revision-{revision}{suffix}'
    temporary = image_path.with_suffix(image_path.suffix + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(image_path)
    write_json(path / 'state.json', {'product_id': product_id, 'brand': brand, 'model': model,
        'revision': revision, 'phase': 'generated', 'sha256': digest(data), 'suffix': suffix})


def restore(root, product_id, brand, model):
    from photo_auditor.integration import AuditHold
    try:
        return _restore(root, product_id, brand, model)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AuditHold('Контрольная точка повреждена; повторная генерация остановлена') from exc


def _restore(root, product_id, brand, model):
    from photo_auditor.integration import AuditHold
    from photo_auditor.store import AuditStore
    path = folder(root, product_id)
    state = json.loads((path / 'state.json').read_text(encoding='utf-8')) if (path / 'state.json').exists() else None
    if state is not None and (not isinstance(state, dict) or
            state.get('phase') not in ('generating', 'composing', 'generated', 'rejected') or
            type(state.get('revision')) is not int or not 0 <= state['revision'] <= 2):
        raise AuditHold('Неверная контрольная точка; повторная генерация остановлена')
    database = Path(root) / 'runs/photo-auditor/audit.sqlite3'
    latest = AuditStore(database.parent).latest(product_id) if database.is_file() else None
    identity = lambda b, m: (b.strip().casefold(), m.strip().casefold())
    expected = identity(brand, model)
    for value in (state, latest['product'] if latest else None):
        if value and (value.get('product_id') != product_id or identity(value['brand'], value['model']) != expected):
            raise AuditHold('Изменилась номенклатура при восстановлении')
    if state and state['phase'] == 'rejected' and latest is None:
        return None
    if state and state['phase'] == 'composing' and latest is None:
        return None  # Native layout has no unknown paid operation to duplicate.
    if state and state['phase'] == 'generating' and (not latest or latest['revision'] < state['revision']):
        raise AuditHold('Не подтверждён результат начатой генерации; требуется сверить сохранённый файл')
    if state and state['phase'] == 'generated' and (not latest or state['revision'] >= latest['revision']):
        if type(state['revision']) is not int or not 0 <= state['revision'] <= 2 or state['suffix'] not in ('.png', '.jpg', '.webp'):
            raise AuditHold('Неверная контрольная точка')
        image_path = path / f"revision-{state['revision']}{state['suffix']}"
        if digest(image_bytes(image_path.read_bytes())) != state['sha256']:
            raise AuditHold('Сохранённая карточка изменилась')
        return {'path': image_path, 'revision': state['revision'],
                'supplier': _supplier(root, product_id, brand, model, latest)}
    if latest:
        source = database.parent / latest['id'] / 'generated.bin'
        if digest(image_bytes(source.read_bytes())) != latest['sha256']:
            raise AuditHold('Снимок проверки изменился')
        generated(root, product_id, brand, model, latest['revision'], source)
        return restore(root, product_id, brand, model)
    return None
