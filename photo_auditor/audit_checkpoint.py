"""Short-lived signed visual evidence; content review never inherits an approval."""
import hmac
import json
import math
from pathlib import Path
import re
import time

from listener.pending import write_json
from photo_auditor import runtime
from photo_auditor.images import digest, image_bytes

POLICY_VERSION = 'visible-product-3'
MAX_AGE_SECONDS = 3600
MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
FILENAME = 'visual-checkpoint.json'
_HEX = re.compile(r'[a-f0-9]{64}')
_ASPECTS = {'identity', 'design', 'shape', 'proportions', 'color', 'details'}


def _ordinary(path):
    return path.is_file() and not path.is_symlink()


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _visual(report):
    if not isinstance(report, dict):
        raise ValueError('Missing visual report')
    value = _copy(report)
    value.pop('content_review', None)
    if value.get('verdict') != 'approved' or value.get('policy_version') != POLICY_VERSION:
        raise ValueError('Visual inspection was not approved under the current policy')
    passes = value.get('comparison_passes')
    if not isinstance(passes, list) or len(passes) != 2:
        raise ValueError('Two independent visual passes are required')
    for comparison in [value, *passes]:
        checks = comparison.get('checks') if isinstance(comparison, dict) else None
        if (not isinstance(checks, list) or len(checks) != len(_ASPECTS) or
                any(not isinstance(check, dict) for check in checks) or
                {check.get('aspect') for check in checks} != _ASPECTS or
                any(check.get('status') != 'match' for check in checks)):
            raise ValueError('Every visible product aspect must match in both passes')
    return value


def _payload(product, folder, brief, report, created):
    folder = Path(folder)
    if (not folder.is_dir() or folder.is_symlink() or
            (hasattr(folder, 'is_junction') and folder.is_junction())):
        raise ValueError('Evidence folder must be an ordinary directory')
    if (not isinstance(product, dict) or set(product) != {'product_id', 'brand', 'model', 'revision'} or
            type(product['revision']) is not int or not 0 <= product['revision'] <= 2 or
            any(not isinstance(product[key], str) or not product[key] for key in ('product_id', 'brand', 'model'))):
        raise ValueError('Exact product identity is required')
    visual = _visual(report)
    generated = folder / 'generated.bin'
    if not _ordinary(generated):
        raise ValueError('Missing immutable product image')
    generated_sha256 = digest(image_bytes(generated.read_bytes()))
    sources = visual.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 5:
        raise ValueError('One to five verified source files are required')
    fingerprints = []
    for expected_id, source in enumerate(sources, 1):
        if (not isinstance(source, dict) or type(source.get('id')) is not int or
                source['id'] != expected_id or
                any(not isinstance(source.get(key), str) or not _HEX.fullmatch(source[key])
                    for key in ('image_sha256', 'page_sha256'))):
            raise ValueError('Source identities or fingerprints are invalid')
        photo, page = folder / f'source-{expected_id}.bin', folder / f'source-{expected_id}.html'
        if not _ordinary(photo) or not _ordinary(page):
            raise ValueError('Missing ordinary evidence files')
        photo_sha256 = digest(image_bytes(photo.read_bytes()))
        page_sha256 = digest(page.read_bytes())
        if (photo_sha256, page_sha256) != (source['image_sha256'], source['page_sha256']):
            raise ValueError('Source evidence no longer matches its visual report')
        fingerprints.append({'id': expected_id, 'image_sha256': photo_sha256, 'page_sha256': page_sha256})
    return {'version': 1, 'created': created, 'policy_version': POLICY_VERSION,
            'product': _copy(product), 'generated_sha256': generated_sha256,
            'brief_sha256': runtime.brief_digest(brief), 'report': visual, 'sources': fingerprints}


def save(root, product, folder, brief, report):
    """Return whether a two-pass approved visual result was saved; never approve content."""
    try:
        path = Path(folder) / FILENAME
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return False
        payload = _payload(product, folder, brief, report, time.time())
        signed = {'payload': payload, 'signature': runtime._signature(root, payload)}
        if len(json.dumps(signed, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_CHECKPOINT_BYTES:
            return False
        write_json(path, signed)
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return False


def load(root, product, folder, brief):
    """Read-only, fail-closed reuse of the same recent visual evidence and generation brief."""
    try:
        path = Path(folder) / FILENAME
        if not _ordinary(path) or path.stat().st_size > MAX_CHECKPOINT_BYTES:
            return None
        signed = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(signed, dict) or set(signed) != {'payload', 'signature'}:
            return None
        payload, signature = signed['payload'], signed['signature']
        if (not isinstance(payload, dict) or
                not isinstance(signature, str) or not _HEX.fullmatch(signature) or
                not hmac.compare_digest(signature, runtime._signature(root, payload))):
            return None
        created = payload.get('created')
        if (type(created) not in (int, float) or not math.isfinite(created) or
                not 0 <= time.time() - created <= MAX_AGE_SECONDS):
            return None
        report = payload.get('report')
        if not isinstance(report, dict) or 'content_review' in report:
            return None
        expected = _payload(product, folder, brief, report, created)
        if payload != expected:
            return None
        return _copy(report)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return None
