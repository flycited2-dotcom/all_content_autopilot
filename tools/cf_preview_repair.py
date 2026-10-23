"""Prepare minimal content-factory patches without modifying its checkout."""
import difflib
import hashlib
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CF_ROOT = ROOT.parent / "content-factory"

MIGRATION = '''def _migration_target(response):
    """400 + Telegram parameters is a definite non-delivery, safe to redirect once."""
    if response.status_code != 400:
        return None
    try:
        body = response.json()
        target = (body.get("parameters") or {}).get("migrate_to_chat_id")
        if body.get("ok") is False and type(target) is int and -(2**52) < target < 0:
            return target
    except (ValueError, AttributeError, TypeError):
        pass
    return None


'''

ALERT = '''def preview_delivery_alert(db_path, target, error, send, now=None):
    """Один общий алерт о сбое доставки вместо сообщения на каждый товар."""
    from contextlib import closing
    now = time.time() if now is None else now
    error = re.sub(r"bot\\d+:[A-Za-z0-9_-]+", "bot<redacted>", str(error or "unknown"))[:200]
    with closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS preview_delivery_alerts "
                   "(target TEXT PRIMARY KEY, error TEXT, notified REAL, suppressed INTEGER DEFAULT 0)")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT notified FROM preview_delivery_alerts WHERE target=?", (str(target),)).fetchone()
        if row and now - row[0] < 900:
            db.execute("UPDATE preview_delivery_alerts SET suppressed=suppressed+1 WHERE target=?", (str(target),))
            return False
        db.execute("INSERT OR REPLACE INTO preview_delivery_alerts VALUES(?,?,?,0)", (str(target), error, now))
    send("⚠️ Не удалось доставить превью в группу проверки. "
         "Готовые карточки сохранены, публикация не выполнена. "
         "Повторные одинаковые ошибки объединены на 15 минут. Причина: " + error)
    return True


'''


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Исходник изменился: нужно проверить точку исправления")
    return source.replace(old, new, 1)


def patched_publisher(source):
    if "def _migration_target(" in source:
        raise ValueError("Исправление миграции уже присутствует")
    source = replace_once(source, "def publish_post(", MIGRATION + "def publish_post(")
    source = replace_once(source, "    last_err = None\n    for attempt", "    last_err = None\n    migration_followed = False\n    for attempt")
    anchor = '''            r = _send_once(bot_token, channel_id, image, caption, parse_mode, client,
                           reply_markup=reply_markup)
'''
    source = replace_once(source, anchor, anchor + '''            migrated = _migration_target(r)
            if not migration_followed and migrated is not None and str(migrated) != str(channel_id):
                migration_followed = True
                channel_id = migrated
                r = _send_once(bot_token, channel_id, image, caption, parse_mode, client,
                               reply_markup=reply_markup)
''')
    compile(source, "telegram.py", "exec")
    return source


def patched_excel(source):
    if "def preview_delivery_alert(" in source:
        raise ValueError("Ограничение уведомлений уже присутствует")
    source = replace_once(source, "def make_photo_resolver(", ALERT + "def make_photo_resolver(")
    source = replace_once(source,
        '''            _alert(f"⚠️ Превью «{item.name[:60]}» не отправилось: {res.error}")''',
        '''            preview_delivery_alert(store.path, review, res.error, _alert)''')
    compile(source, "excel_run.py", "exec")
    return source


def review_env(source, old, new):
    matches = list(re.finditer(r"(?m)^[ \t]*(?:export[ \t]+)?TELEGRAM_REVIEW_CHANNEL_ID[ \t]*=[^\r\n]*", source))
    if len(matches) != 1:
        raise ValueError("Нужна ровно одна настройка группы проверки")
    match = matches[0]
    value = match.group().split("=", 1)[1].split("#", 1)[0].strip().strip("\"'")
    if value not in (str(old), str(new)):
        raise ValueError("Настроен другой адрес группы: требуется сверка")
    return source[:match.start()] + f"TELEGRAM_REVIEW_CHANNEL_ID={new}" + source[match.end():]


def prepare(source_root=CF_ROOT):
    folder = ROOT / "runs/2026-10-23/preview-repair/package"
    patches = []
    manifest = {}
    for relative, transform in (("publish/telegram.py", patched_publisher), ("orchestrator/excel_run.py", patched_excel)):
        original = (Path(source_root) / "src/content_factory" / relative).read_text(encoding="utf-8")
        updated = transform(original)
        target = folder / "src/content_factory" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8")
        manifest["src/content_factory/" + relative] = {"before": hashlib.sha256(original.encode()).hexdigest(),
                                                      "after": hashlib.sha256(updated.encode()).hexdigest()}
        patches.extend(difflib.unified_diff(original.splitlines(keepends=True), updated.splitlines(keepends=True),
            fromfile="a/src/content_factory/" + relative, tofile="b/src/content_factory/" + relative))
    output = ROOT / "integrations/content-factory-preview-delivery.patch"
    output.write_text("".join(patches), encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.copyfile(__file__, folder / "cf_preview_repair.py")
    shutil.copyfile(ROOT / "tools/repair_review_chat.py", folder / "repair_review_chat.py")
    return folder


if __name__ == "__main__":
    print(prepare())
