"""Operating mode: the auditor either gates uploads or only advises the owner.

advisory — the auditor diagnoses and reports, but never stops the factory:
only a confirmed visible discrepancy triggers a bounded repair; doubtful
evidence, provider outages and timeouts release the file with notes.
Publication still happens only after the owner's ✅ on the server.
gate — the historical strict mode (no file without an automatic approval).
"""
from pathlib import Path

import yaml

MODES = ('advisory', 'gate')
DEFAULTS = {'mode': 'gate', 'repairs': 1, 'wait_seconds': 900}


def settings(root):
    """A missing file keeps the historical gate; the project file enables advisory."""
    path = Path(root) / 'rules/photo-auditor-mode.yaml'
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, yaml.YAMLError):
        # A damaged switch must not silently weaken or stop the factory: keep gate.
        return dict(DEFAULTS)
    if not isinstance(value, dict) or value.get('mode') not in MODES:
        return dict(DEFAULTS)
    repairs, wait = value.get('repairs', DEFAULTS['repairs']), value.get('wait_seconds', DEFAULTS['wait_seconds'])
    from photo_auditor.store import MAX_REGENERATIONS
    if type(repairs) is not int or not 0 <= repairs <= MAX_REGENERATIONS:
        repairs = DEFAULTS['repairs']
    if type(wait) not in (int, float) or not 60 <= wait <= 3600:
        wait = DEFAULTS['wait_seconds']
    return {'mode': value['mode'], 'repairs': repairs, 'wait_seconds': wait}


def advisory(root):
    return settings(root)['mode'] == 'advisory'
