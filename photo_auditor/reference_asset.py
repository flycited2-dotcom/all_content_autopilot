"""Use a fetched, model-bound manufacturer asset for a deterministic composition."""
from pathlib import Path

from photo_auditor.images import digest, image_bytes


def from_audit(root, audit):
    for source in (audit.get('report') or {}).get('sources', []):
        if not (source.get('official') and source.get('model_image_verified') and
                source.get('model') == audit['product']['model'] and
                type(source.get('id')) is int and 1 <= source['id'] <= 5):
            continue
        path = Path(root) / 'runs/photo-auditor' / audit['id'] / f"source-{source['id']}.bin"
        if path.is_file() and digest(image_bytes(path.read_bytes())) == source.get('image_sha256'):
            return path
    return None


def latest(root, product_id, brand, model):
    from photo_auditor.store import AuditStore
    row = AuditStore(Path(root) / 'runs/photo-auditor').latest(product_id)
    if row and (row['product']['brand'], row['product']['model']) == (brand, model):
        return from_audit(root, row)
    return None
