"""Apply the owner's dark text style and version the three current Xiaomi briefs."""
import argparse
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from listener.pending import write_json
from photo_auditor import presentation, runtime
from photo_auditor.images import digest, image_bytes
from photo_auditor.store import AuditStore
from tools.install_photo_audit import AGENT, atomic

RUN = ROOT / 'runs/2026-10-27/dark-text-style'
TARGETS = {3816: 'MI TV A PRO 2026 L65MB-APRU QLED 4K SmartTV Android',
           3818: 'TV S Mini LED 55 2026 L55MC-SRU 4K SmartTV Android',
           3819: 'MI TV A PRO 2026 L43MB-APRU QLED 4K SmartTV Android'}


def dark_prompt(original):
    marker = 'Нужно сохранить тот же фирменный визуальный стиль:'
    if original.count(marker) != 1 or original.count('{{SPECS}}') != 1:
        raise ValueError('The reviewed KBT prompt changed')
    body = original.split(marker, 1)[1]
    lines = []
    for line in body.splitlines():
        if 'эталон' in line.casefold() or 'Фото 1' in line or 'Фото 2' in line:
            if line.strip().startswith('— нельзя переносить бренд'):
                lines.append('— нельзя переносить бренд из другой карточки или постороннего примера')
            continue
        lines.append(line)
    body = '\n'.join(lines)
    for source, target in (('с Фото 3', 'с исходного фото товара'),
                           ('на Фото 3', 'на исходном фото товара'),
                           ('Фото 3', 'исходное фото товара')):
        body = body.replace(source, target)
    header = ('СТИЛЬ КАРТОЧКИ: ТЁМНЫЙ ПО ТЕКСТОВОМУ ПРОМПТУ.\n'
              'Владелец выбрал этот вариант оформления 27.10.2026. '
              'Следуй только описанному ниже тёмному стилю. '
              'Светлый интерьер, синие плашки и композицию прошлых примеров не использовать.\n'
              'Единственное приложенное изображение — исходный товар, а не образец оформления. '
              'Сохрани точную модель, бренд, конструкцию и пропорции этого товара.\n'
              'ЗАДАЧА: готовая квадратная карточка 1:1 в высоком качестве 2K. '
              'Тёмно-графитовый студийный фон, товар слева, полное название и преимущества справа; '
              'бело-серый текст, тёплые золотисто-бежевые акценты и стеклянные плашки.\n\n'
              'ФИРМЕННЫЙ ВИЗУАЛЬНЫЙ СТИЛЬ:\n')
    result = header + body.strip() + '\n'
    if any(text in result for text in ('Фото 1', 'Фото 2', 'Фото 3', 'эталон')):
        raise ValueError('Conflicting reference instructions remain')
    return result


def dark_config(source):
    start = source.index('    "kbt": Mode(')
    end = source.index('    "ready_light": Mode(', start)
    block = source[start:end]
    anchor = '        reference_files=_mode_refs("kbt"),'
    if block.count(anchor) != 1 or 'requires_reference=' in block:
        raise ValueError('KBT mode changed; review it before installation')
    changed = block.replace(anchor, '        reference_files=[],\n        requires_reference=False,')
    result = source[:start] + changed + source[end:]
    compile(result, 'config.py', 'exec')
    return result


def server_snapshot():
    script = r'''
import sqlite3,json
db=sqlite3.connect('file:/opt/content-factory/state/content_factory.db?mode=ro',uri=True,timeout=20)
db.row_factory=sqlite3.Row
db.execute('ATTACH DATABASE ? AS q',('file:/root/ritualb2b/queue.db?mode=ro',))
db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
ids=(3816,3818,3819)
value={'jobs':[dict(r) for r in db.execute('SELECT * FROM q.jobs WHERE id IN (?,?,?) ORDER BY id',ids)],
 'items':[dict(r) for r in db.execute('SELECT * FROM excel_items WHERE card_job IN (?,?,?) ORDER BY card_job',ids)],
 'active_jobs':db.execute("SELECT count(*) FROM q.jobs WHERE status IN ('pending','processing')").fetchone()[0]}
print(json.dumps(value,ensure_ascii=False))
'''
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'sprintbox-itp', 'python3 -'],
                            input=script.encode(), capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Cannot read the current factory jobs')
    value = json.loads(result.stdout)
    if len(value['jobs']) != 3 or len(value['items']) != 3 or value['active_jobs']:
        raise RuntimeError('Factory is active or a current card was replaced')
    items = {r['card_job']: r for r in value['items']}
    for job in value['jobs']:
        if (job['id'] not in TARGETS or job['mode'] != 'kbt' or job['brand'] != 'Xiaomi' or
                TARGETS[job['id']] != job['model'] or job['status'] != 'failed' or job['output_filename'] is not None or
                'PHOTO_AUDIT_HOLD' not in (job['error_text'] or '') or
                items[job['id']]['status'] != 'failed' or
                (items[job['id']]['brand'], items[job['id']]['model']) != (job['brand'], job['model'])):
            raise RuntimeError('A card no longer matches its current failed audit hold')
    return value


def local_snapshot(server):
    store = AuditStore(ROOT / 'runs/photo-auditor')
    values = []
    for job in server['jobs']:
        row = store.latest(f"fotogen-{job['id']}")
        if not row or row['status'] != 'review' or row['revision'] != 0:
            raise RuntimeError('A saved card is no longer the reviewed revision')
        if any(row['product'][key] != job[key] for key in ('brand', 'model')):
            raise RuntimeError('Saved nomenclature differs from the factory')
        scope = runtime._read(ROOT, job['id'])
        if scope['mode'] != 'kbt' or any(scope[key] != job[key] for key in ('brand', 'model')):
            raise RuntimeError('Signed scope changed')
        if digest(image_bytes((store.root / row['id'] / 'generated.bin').read_bytes())) != row['sha256']:
            raise RuntimeError('Saved card changed')
        values.append(row)
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    config_path, prompt_path = AGENT / 'config.py', AGENT / 'prompts/kbt.txt'
    if config_path.is_symlink() or prompt_path.is_symlink():
        raise RuntimeError('Expected ordinary configuration files')
    original_config, original_prompt = config_path.read_bytes(), prompt_path.read_bytes()
    new_config = dark_config(original_config.decode('utf-8-sig').replace('\r\n', '\n')).encode('utf-8')
    new_prompt = dark_prompt(original_prompt.decode('utf-8-sig')).encode('utf-8')
    server = server_snapshot()
    local = local_snapshot(server)
    sha = lambda b: hashlib.sha256(b).hexdigest()
    snapshots = {}
    for job_id in TARGETS:
        for kind in ('brief', 'presentation'):
            path = runtime._path(ROOT, job_id).with_name(f'{job_id}.{kind}.json')
            snapshots[str(path)] = path.read_bytes().hex() if path.exists() else None
    for row in local:
        path = ROOT / 'runs/photo-auditor' / row['id'] / 'visual-checkpoint.json'
        snapshots[str(path)] = path.read_bytes().hex() if path.exists() else None
    plan = {'decision': 'owner selected dark style by textual prompt', 'server': server,
            'local': local, 'snapshots': snapshots,
            'config_before': sha(original_config), 'prompt_before': sha(original_prompt),
            'config_after': sha(new_config), 'prompt_after': sha(new_prompt)}
    if not args.apply:
        RUN.mkdir(parents=True, exist_ok=True)
        (RUN / 'config.py').write_bytes(new_config)
        (RUN / 'kbt.txt').write_bytes(new_prompt)
        patch = ''.join(difflib.unified_diff(original_config.decode('utf-8-sig').replace('\r\n', '\n').splitlines(True),
            new_config.decode().splitlines(True), fromfile='a/config.py', tofile='b/config.py'))
        patch += ''.join(difflib.unified_diff(original_prompt.decode('utf-8-sig').replace('\r\n', '\n').splitlines(True),
            new_prompt.decode().splitlines(True), fromfile='a/prompts/kbt.txt', tofile='b/prompts/kbt.txt'))
        (ROOT / 'integrations/kbt-dark-text-style.patch').write_text(patch, encoding='utf-8', newline='\n')
        write_json(RUN / 'plan.json', plan)
        print(json.dumps({'apply': False, 'mode': 'kbt', 'style': 'dark_text', 'jobs': list(TARGETS),
                          'light_style_references': 0, 'prompt_after': sha(new_prompt)}))
        return
    if (json.loads((RUN / 'plan.json').read_text(encoding='utf-8')) != plan or
            (RUN / 'config.py').read_bytes() != new_config or (RUN / 'kbt.txt').read_bytes() != new_prompt):
        raise RuntimeError('The reviewed installation plan changed')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 8793)) == 0:
            raise RuntimeError('Stop the idle auditor before updating signed briefs')
    if list((AGENT / 'logs/upload_outbox').rglob('*.json')):
        raise RuntimeError('Deliver the pending outbox before switching')
    backup = RUN / 'backup'
    backup.mkdir()
    (backup / 'config.py').write_bytes(original_config)
    (backup / 'kbt.txt').write_bytes(original_prompt)
    write_json(backup / 'signed-snapshots.json', snapshots)
    with sqlite3.connect(ROOT / 'runs/photo-auditor/audit.sqlite3') as current, sqlite3.connect(backup / 'audit.sqlite3') as saved:
        current.backup(saved)
    try:
        atomic(config_path, new_config)
        atomic(prompt_path, new_prompt)
        spec = importlib.util.spec_from_file_location('_dark_style_config', config_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cfg = module.get_mode('kbt')
        if cfg.reference_files or cfg.requires_reference or not cfg.prompt:
            raise RuntimeError('The new mode still requires conflicting image references')
        store = AuditStore(ROOT / 'runs/photo-auditor')
        migrated = []
        for job, old in zip(server['jobs'], local):
            runtime.register_job(ROOT, job['id'], 'kbt', job['brand'], job['model'], job['specs'])
            # The owner's explicit choice versions these pending presentations; originals are backed up.
            path = presentation._path(ROOT, job['id'])
            path.unlink(missing_ok=True)
            presentation.capture(ROOT, job['id'], cfg.render_prompt(job['specs'], brand=job['brand'], model=job['model']), [])
            if not store.recheck(old['id']):
                raise RuntimeError('Could not queue the same saved card after the style change')
            migrated.append({'job_id': job['id'], 'audit_id': old['id'], 'revision': old['revision']})
    except Exception as exc:
        atomic(config_path, original_config)
        atomic(prompt_path, original_prompt)
        for name, before in snapshots.items():
            path = Path(name)
            if before is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, bytes.fromhex(before))
        with sqlite3.connect(backup / 'audit.sqlite3') as saved, sqlite3.connect(ROOT / 'runs/photo-auditor/audit.sqlite3') as current:
            saved.backup(current)
        write_json(RUN / 'rollback.json', {'restored': True, 'error_type': type(exc).__name__})
        raise RuntimeError('Style installation failed; previous configuration and audit state restored') from None
    write_json(RUN / 'installed.json', {'style': 'dark_text', 'mode': 'kbt', 'backup': str(backup),
               'jobs': migrated, 'config_sha256': sha(config_path.read_bytes()), 'prompt_sha256': sha(prompt_path.read_bytes())})
    print(json.dumps({'installed': True, 'jobs': migrated, 'new_jobs_created': False, 'publication': False}))


if __name__ == '__main__':
    main()
