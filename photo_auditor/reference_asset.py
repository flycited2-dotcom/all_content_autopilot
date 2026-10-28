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
    checks = report.get('source_checks')
    # Page provenance identifies the SKU, not whether its image shows the main
    # appliance. Legacy reports and incomplete visual checks cannot authorize repair.
    if not isinstance(sources, list) or not sources or not isinstance(checks, list) or len(checks) != len(sources):
        return None
    source_ids = [source.get('id') for source in sources if isinstance(source, dict)]
    if (len(source_ids) != len(sources) or
            any(type(source_id) is not int or not 1 <= source_id <= 5 for source_id in source_ids) or
            len(set(source_ids)) != len(source_ids)):
        return None
    validated = {}
    for check in checks:
        if (not isinstance(check, dict) or
                set(check) != {'id', 'main_product_visible', 'suitable_for_repair', 'reason'} or
                type(check.get('id')) is not int or check['id'] not in source_ids or check['id'] in validated or
                type(check.get('main_product_visible')) is not bool or
                type(check.get('suitable_for_repair')) is not bool or
                check['suitable_for_repair'] and not check['main_product_visible'] or
                not isinstance(check.get('reason'), str) or not 0 < len(check['reason'].strip()) <= 2000):
            return None
        validated[check['id']] = check
    consensus = False
    if report.get('evidence_basis') in {'secondary_consensus', 'catalog_reference'} and report.get('verdict') in ('approved', 'regenerate'):
        from photo_auditor.evidence import eligible
        # A replacement must still be a visually validated main-product asset.
        primary_sources = [{**source, 'main_product_visible': bool(
            validated[source['id']]['main_product_visible'] and
            validated[source['id']]['suitable_for_repair'] and
            source.get('model') == audit['product']['model'])} for source in sources]
        _, basis = eligible(primary_sources, report.get('manufacturer_unavailable', False))
        consensus = basis == 'catalog_reference'
    for source in sources:
        visual = validated[source['id']]
        if not (visual['main_product_visible'] and visual['suitable_for_repair'] and
                (source.get('official') or consensus) and source.get('model_image_verified') and
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
