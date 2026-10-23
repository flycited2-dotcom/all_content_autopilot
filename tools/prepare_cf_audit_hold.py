"""Prepare and verify one terminal audit-error change; never installs it."""
import difflib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/2026-10-23/autopilot'
REMOTE_SOURCE = '/opt/content-factory/src/content_factory/orchestrator/excel_pipeline.py'


def patched(source):
    old = '        if item.tries + 1 >= MAX_TRIES:\n'
    new = '''        # A photo-auditor hold is terminal until the owner resolves it.
        # Keep legacy messages for agents already running during rollout.
        audit_hold = any(marker in (err or "") for marker in (
            "PHOTO_AUDIT_HOLD:", "Фото остановлено для ручной проверки:",
            "Контроль ещё не завершён;", "Служба контроля недоступна; фото не выгружено"))
        if audit_hold or item.tries + 1 >= MAX_TRIES:
'''
    if source.count(old) != 1:
        raise ValueError('Unexpected content-factory source; refused to patch')
    result = source.replace(old, new)
    compile(result, REMOTE_SOURCE, 'exec')
    return result


def verify(source_path):
    spec = importlib.util.spec_from_file_location('cf_audit_trial', source_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as directory:
        for stage in ('research', 'card'):
            for held in (False, True):
                store = module.ExcelStore(Path(directory) / f'{stage}-{held}.sqlite3')
                key = f'excel|midea|{stage}-{held}'
                store.select_items([(key, 'Midea', 'EXACT-1', 'Test product', 100)])
                store.update(key, status=stage, **{stage + '_job': 123})
                error = 'Фото остановлено для ручной проверки: test' if held else 'temporary network failure'
                module.tick(store, lambda *a, **k: None,
                    lambda *a: ('failed', None, None, error), lambda *a, **k: None,
                    lambda *a: True, max_new=0)
                assert store.get(key).status == ('failed' if held else 'new')
                if held:
                    module.tick(store, lambda *a, **k: (_ for _ in ()).throw(AssertionError('Paid retry')),
                        lambda *a: ('failed', None, None, error), lambda *a, **k: None,
                        lambda *a: True)
                    assert store.get(key).status == 'failed'


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                             'sprintbox-itp', 'cat ' + REMOTE_SOURCE], capture_output=True, check=True)
    original = result.stdout.decode('utf-8').replace('\r\n', '\n')
    updated = patched(original)
    (RUN / 'cf-excel-before.py').write_bytes(result.stdout)
    after = RUN / 'cf-excel-after.py'
    after.write_text(updated, encoding='utf-8')
    verify(after)
    patch = ''.join(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
                    fromfile='a/src/content_factory/orchestrator/excel_pipeline.py',
                    tofile='b/src/content_factory/orchestrator/excel_pipeline.py'))
    (ROOT / 'integrations/cf-photo-audit-hold.patch').write_text(patch, encoding='utf-8')
    print('Prepared one audit-hold guard; research/card holds stop, temporary failures retain retry (4 cases).')


if __name__ == '__main__':
    main()
