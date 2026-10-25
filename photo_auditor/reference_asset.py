"""Use a fetched, model-bound manufacturer asset for a deterministic composition."""
from pathlib import Path
import io
from PIL import Image

from photo_auditor.images import digest, image_bytes


def typed(path):
    path = Path(path)
    data = image_bytes(path.read_bytes())
    with Image.open(io.BytesIO(data)) as image:
        suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[image.format]
    if path.suffix.casefold() in {'.png', '.jpg', '.jpeg', '.webp'}:
        return path
    target = path.with_suffix(suffix)
    target.write_bytes(data)
    return target


def from_audit(root, audit):
    report = audit.get('report') or {}
    sources = report.get('sources', [])
    consensus = False
    if report.get('evidence_basis') == 'secondary_consensus' and report.get('verdict') in ('approved', 'regenerate'):
        from photo_auditor.evidence import eligible
        _, basis = eligible(sources, report.get('manufacturer_unavailable', False))
        consensus = basis == 'secondary_consensus'
    for source in sources:
        if not ((source.get('official') or consensus) and source.get('model_image_verified') and
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
