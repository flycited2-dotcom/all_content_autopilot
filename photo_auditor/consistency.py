"""A repeated optimistic verdict cannot erase a recorded, evidenced defect."""
import json


def preserve_defects(report, previous):
    if report['verdict'] != 'approved':
        return report
    current = {s.get('image_sha256'): n for n, s in enumerate(report.get('sources', []), 1)
               if s.get('official') and s.get('model_image_verified') and s.get('image_sha256')}
    corrections = {}
    for old in previous:
        old = json.loads(old) if isinstance(old, str) else old
        if not old:
            continue
        sources = old.get('sources', [])
        for check in old.get('checks', []):
            if check.get('status') != 'mismatch' or check.get('confidence', 0) < .9:
                continue
            ids = []
            for n in check.get('source_ids', []):
                if type(n) is int and 1 <= n <= len(sources):
                    source = sources[n - 1]
                    if source.get('official') and source.get('model_image_verified'):
                        mapped = current.get(source.get('image_sha256'))
                        if mapped:
                            ids.append(mapped)
            if ids:
                corrections[check['aspect']] = {**check, 'source_ids': sorted(set(ids))}
    if not corrections:
        return report
    return {**report, 'verdict': 'regenerate',
            'reason': 'Повторная оценка противоречит сохранённому подтверждённому замечанию по тому же файлу и эталону. '
                      'Товар нужно исправить; прежнее замечание сохранено.',
            'checks': [corrections.get(c['aspect'], c) for c in report['checks']]}
