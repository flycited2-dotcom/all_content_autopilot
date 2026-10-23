"""Server repair, read-only by default. Use --apply only after owner approval."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from cf_preview_repair import review_env

OLD = -5428762212
NEW = -1003775164695


class RepairError(RuntimeError):
    pass


def systemctl(*args, check=True):
    result = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RepairError("Не выполнена команда управления сервисом: " + " ".join(args))
    return result


def atomic_write(path, content, mode):
    descriptor, name = tempfile.mkstemp(prefix=".preview-repair-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_telegram(token):
    def call(client, method, **data):
        try:
            response = client.post(f"https://api.telegram.org/bot{token}/{method}", data=data)
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise RepairError("Не удалось проверить Telegram без изменения настроек") from None
    with httpx.Client(timeout=httpx.Timeout(20, connect=3), follow_redirects=False,
                      transport=httpx.HTTPTransport(retries=3)) as client:
        identity = call(client, "getMe")
        if not identity.get("ok") or identity.get("result", {}).get("username", "").lower() != "sendpr1ce_bot":
            raise RepairError("Настроен другой Telegram-бот; исправление остановлено")
        old = call(client, "getChat", chat_id=OLD)
        migrated = (old.get("parameters") or {}).get("migrate_to_chat_id")
        resolved = old.get("result", {}).get("id") if old.get("ok") else None
        # getChat may still return the retired basic group without migration
        # parameters (only sendPhoto reports the redirect). The owner explicitly
        # selected this replacement, so verify both identities and membership.
        old_group = old.get("result") or {}
        selected_old = (resolved == OLD and old_group.get("type") == "group"
                        and old_group.get("title") == "Тест_контент_завод")
        if migrated != NEW and resolved != NEW and not selected_old:
            raise RepairError("Не подтверждён исходный адрес группы проверки")
        current = call(client, "getChat", chat_id=NEW)
        if not current.get("ok") or current.get("result", {}).get("id") != NEW or current["result"].get("title") != "Тест_контент_завод" or current["result"].get("type") != "supergroup":
            raise RepairError("Не подтверждён новый адрес группы проверки")
        membership = call(client, "getChatMember", chat_id=NEW, user_id=identity["result"]["id"])
        if membership.get("result", {}).get("status") not in ("administrator", "creator", "member"):
            raise RepairError("Никита не состоит в новой группе проверки")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--config-only", action="store_true", help="Только адрес, если код сервера отличается от снимка")
    parser.add_argument("--root", type=Path, default=Path("/opt/content-factory"))
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    env_file = root / ".env"
    original_env = env_file.read_text(encoding="utf-8")
    updated_env = review_env(original_env, OLD, NEW)
    from decouple import Config, RepositoryEnv
    token = Config(RepositoryEnv(str(env_file)))("TELEGRAM_BOT_TOKEN")
    check_telegram(token)
    files = []
    if not args.config_only:
        manifest = json.loads((args.package / "manifest.json").read_text(encoding="utf-8"))
        allowed = {"src/content_factory/publish/telegram.py", "src/content_factory/orchestrator/excel_run.py"}
        if set(manifest) != allowed:
            raise RepairError("Неверный состав исправления")
        for relative, hashes in manifest.items():
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise RepairError("Путь исходника вышел за каталог проекта")
            before = target.read_text(encoding="utf-8")
            after = (args.package / relative).read_text(encoding="utf-8")
            actual = hashlib.sha256(before.encode()).hexdigest()
            if actual not in (hashes["before"], hashes["after"]) or hashlib.sha256(after.encode()).hexdigest() != hashes["after"]:
                raise RepairError("Исходники сервера отличаются: сначала сверка, либо только смена адреса")
            compile(after, str(target), "exec")
            files.append((target, before, after, target.stat().st_mode & 0o777))
    plan = {"old_chat": OLD, "new_chat": NEW, "files": [str(f[0]) for f in files], "applied": False}
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    backup = root / "backups" / ("preview-repair-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    backup.chmod(0o700)
    shutil.copyfile(env_file, backup / "env.backup")
    (backup / "env.backup").chmod(0o600)
    env_mode = env_file.stat().st_mode & 0o777
    for target, before, _, _ in files:
        relative = target.relative_to(root)
        saved = backup / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(before, encoding="utf-8")
    timer_active = systemctl("is-active", "cf-excel.timer", check=False).returncode == 0
    changed = False
    try:
        systemctl("stop", "cf-excel.timer")
        systemctl("stop", "cf-excel.service")
        changed = True
        atomic_write(env_file, updated_env, env_mode)
        for target, _, after, mode in files:
            atomic_write(target, after, mode)
        systemctl("restart", "cf-bot.service")
        systemctl("is-active", "cf-bot.service")
    except Exception:
        if changed:
            atomic_write(env_file, original_env, env_mode)
            for target, before, _, mode in files:
                atomic_write(target, before, mode)
            systemctl("restart", "cf-bot.service", check=False)
        raise
    finally:
        if timer_active:
            systemctl("start", "cf-excel.timer")
    print(json.dumps({**plan, "applied": True, "backup": str(backup), "timer_resumed": timer_active}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepairError as error:
        print("Исправление остановлено: " + str(error))
        raise SystemExit(1)
    except Exception as error:
        print("Исправление остановлено: " + type(error).__name__)
        raise SystemExit(1)
