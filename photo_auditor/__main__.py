import argparse
import json
import logging
import os
import threading
from pathlib import Path

from listener.chatgpt import ChatGPTProvider
from listener.chatgpt_auth import ChatGPTAuth
from orchestrator_bot.telegram_api import TelegramApi
from photo_auditor.credentials import SetupError, configure, credentials_path, load
from photo_auditor.engine import Auditor
from photo_auditor.service import make_server, process_one, run_bot, worker_loop
from photo_auditor.store import AuditStore

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Контроль фотографий @foto_auditor_bot")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("configure", help="Скрыто ввести токен BotFather")
    config.add_argument("--console", action="store_true")
    commands.add_parser("status")
    commands.add_parser("run")
    recheck = commands.add_parser('recheck', help='Повторить проверку сохранённого файла без новой генерации')
    recheck.add_argument('audit_id')
    check = commands.add_parser("check", help="Проверить локальный файл без отправки в Telegram")
    check.add_argument("image", type=Path)
    check.add_argument("--brand", required=True)
    check.add_argument("--model", required=True)
    check.add_argument("--product-id", required=True)
    args = parser.parse_args(argv)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if args.command == "configure":
        print(json.dumps(configure(ROOT, args.console), ensure_ascii=False))
        return 0
    store = AuditStore(ROOT / "runs/photo-auditor")
    if args.command == 'recheck':
        accepted = store.recheck(args.audit_id)
        print(json.dumps({'queued': accepted, 'new_generation': False}))
        return 0 if accepted else 2
    if args.command == "status":
        print(json.dumps({"bot_configured": credentials_path(ROOT).is_file(), "jobs": store.status(),
                          "chatgpt_connected": ChatGPTAuth(ROOT).ready()}, ensure_ascii=False))
        return 0
    auditor = Auditor(ROOT, ChatGPTProvider(ChatGPTAuth(ROOT)))
    if args.command == "check":
        job = store.submit({"product_id": args.product_id, "revision": 0, "brand": args.brand, "model": args.model}, args.image.read_bytes())
        if job["status"] == "queued":
            process_one(store, auditor, audit_id=job["id"])
        result = store.get(job["id"])
        print(json.dumps({"id": result["id"], "status": result["status"],
                          "report": str(store.root / job["id"] / "report.json")}, ensure_ascii=False))
        return 0 if result["report"] else 2
    config = load(ROOT)
    server = make_server(store, config["api_token"])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(store.root / "service.log", encoding="utf-8")])
    stop = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=worker_loop, args=(store, auditor, stop), daemon=True).start()
    tg = TelegramApi(config["bot_token"])
    try:
        run_bot(store, tg, int(os.environ.get("PHOTO_AUDIT_OWNER", "1264067528")), stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SetupError as exc:
        print("Фотоаудитор: " + str(exc))
        raise SystemExit(1)
    except Exception as exc:
        # Never show traceback/URLs from Telegram/OAuth clients.
        print("Фотоаудитор: действие не завершено (" + type(exc).__name__ + "). Проверьте подключение и журнал.")
        raise SystemExit(1)
