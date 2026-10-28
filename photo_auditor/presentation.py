"""Immutable copies of the same prompt and style references the generator uses."""
import json
from pathlib import Path

from listener.pending import write_json
from photo_auditor.images import digest, image_bytes


def _path(root, job_id):
    from photo_auditor.runtime import _path as scope_path
    return scope_path(root, job_id).with_name(f'{job_id}.presentation.json')


def capture(root, job_id, prompt, references):
    from photo_auditor.integration import AuditHold
    from photo_auditor.runtime import CARD_MODES, _read, _signature
    scope = _read(root, job_id)
    if not scope['required'] or scope['mode'] not in CARD_MODES:
        return
    if not isinstance(prompt, str) or not 0 < len(prompt) <= 60000 or len(references) > 8:
        raise AuditHold('Нет корректных правил оформления задания')
    assets = []
    for reference in references:
        path = Path(reference)
        data = image_bytes(path.read_bytes())
        fingerprint = digest(data)
        frozen = Path(root) / 'runs/photo-auditor/brief-assets' / (fingerprint + '.bin')
        if frozen.exists() and frozen.read_bytes() != data:
            raise AuditHold('Сохранённый эталон оформления изменился')
        if not frozen.exists():
            frozen.parent.mkdir(parents=True, exist_ok=True)
            frozen.write_bytes(data)
        assets.append({'name': path.name, 'sha256': fingerprint})
    payload = {'job_id': job_id, 'prompt': prompt, 'references': assets}
    signed = {'payload': payload, 'signature': _signature(root, payload)}
    path = _path(root, job_id)
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')) != signed:
            raise AuditHold('Изменились правила или эталоны оформления уже начатого задания')
    else:
        write_json(path, signed)


def read(root, job_id):
    from photo_auditor.integration import AuditHold
    from photo_auditor.runtime import _signature
    import hmac
    import re
    path = _path(root, job_id)
    if not path.exists():
        return None
    try:
        signed = json.loads(path.read_text(encoding='utf-8'))
        payload = signed['payload']
        if (set(payload) != {'job_id', 'prompt', 'references'} or payload['job_id'] != job_id or
                not hmac.compare_digest(signed['signature'], _signature(root, payload)) or
                not isinstance(payload['prompt'], str) or not 0 < len(payload['prompt']) <= 60000 or
                not isinstance(payload['references'], list) or len(payload['references']) > 8):
            raise ValueError('Invalid presentation')
        for asset in payload['references']:
            if (set(asset) != {'name', 'sha256'} or not isinstance(asset['name'], str) or
                    not isinstance(asset['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', asset['sha256'])):
                raise ValueError('Invalid presentation asset')
            data = (Path(root) / 'runs/photo-auditor/brief-assets' / (asset['sha256'] + '.bin')).read_bytes()
            if digest(image_bytes(data)) != asset['sha256']:
                raise ValueError('Changed presentation asset')
        return payload
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuditHold('Не подтверждены сохранённые правила и эталоны оформления') from exc


def images(root, brief):
    return [(Path(root) / 'runs/photo-auditor/brief-assets' / (asset['sha256'] + '.bin')).read_bytes()
            for asset in brief.get('presentation', {}).get('references', [])]
