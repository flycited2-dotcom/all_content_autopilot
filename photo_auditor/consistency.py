"""A repeated optimistic verdict cannot erase a recorded, evidenced defect."""
import json
from photo_auditor.evidence import eligible, covers


def preserve_defects(report, previous):
    if report['verdict'] != 'approved':
        return report
    current_sources = report.get('sources', [])
    current_ids, current_basis = eligible(current_sources, report.get('manufacturer_unavailable', False))
    current = {s.get('image_sha256'): n for n, s in enumerate(current_sources, 1)
               if n in current_ids and s.get('image_sha256')}
    corrections = {}
    for old in previous:
        old = json.loads(old) if isinstance(old, str) else old
        if not old:
            continue
        sources = old.get('sources', [])
        old_ids, old_basis = eligible(sources, old.get('manufacturer_unavailable', False))
        for check in old.get('checks', []):
            if (check.get('status') != 'mismatch' or check.get('confidence', 0) < (.9 if old_basis == 'official' else .95) or
                    not covers(check.get('source_ids', []), old_ids, sources, old_basis)):
                continue
            ids = []
            for n in check.get('source_ids', []):
                if type(n) is int and 1 <= n <= len(sources):
                    source = sources[n - 1]
                    if n in old_ids:
                        mapped = current.get(source.get('image_sha256'))
                        if mapped:
                            ids.append(mapped)
            if ids and covers(ids, current_ids, current_sources, current_basis):
                corrections[check['aspect']] = {**check, 'source_ids': sorted(set(ids))}
    if not corrections:
        return report
    return {**report, 'verdict': 'regenerate',
            'reason': 'Повторная оценка противоречит сохранённому подтверждённому замечанию по тому же файлу и эталону. '
                      'Товар нужно исправить; прежнее замечание сохранено.',
            'checks': [corrections.get(c['aspect'], c) for c in report['checks']]}
