"""python -m orchestrator_bot run | report"""
from __future__ import annotations

import logging
import sys
import threading
import time

from orchestrator_bot import bot, report
from orchestrator_bot.api import make_server, start_of_day
from orchestrator_bot.config import Settings
from orchestrator_bot.factory import Factory
from orchestrator_bot.store import Store
from orchestrator_bot.telegram_api import TelegramApi
from orchestrator_bot.transcribe import Transcriber


def _report_text(store, factory) -> str:
    now = time.time()
    since = start_of_day(now)
    try:
        status = factory.status(since)
    except Exception:
        status = {}
    try:
        worker_state = factory.workers()
    except Exception:
        worker_state = None
    return report.build_report(status, store.actions_since(since), store.last_heartbeat(), now, workers=worker_state)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for name in ("httpx", "httpcore"):            # их INFO пишет URL Bot API — а в нём токен бота
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv=None) -> int:
    configure_logging()
    mode = (argv or sys.argv[1:] or ["run"])[0]
    settings = Settings.from_env()
    store = Store(settings.db_path)
    factory = Factory(settings.cf_python, settings.cf_root)
    tg = TelegramApi(settings.bot_token)
    if mode == "report":
        tg.send(settings.owner_chat_id, _report_text(store, factory))
        return 0
    if mode != "run":
        print("usage: python -m orchestrator_bot run|report", file=sys.stderr)
        return 2
    send = lambda text, keyboard=None: tg.send(settings.owner_chat_id, text, keyboard)  # noqa: E731
    server = make_server(store, factory, send, settings.api_token, settings.api_port, settings.doubt_threshold)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deps = bot.Deps(store=store, tg=tg, transcriber=Transcriber(settings.whisper_model), factory=factory,
                    owner_chat_id=settings.owner_chat_id, threshold=settings.doubt_threshold,
                    report_fn=lambda: _report_text(store, factory))
    bot.run_forever(deps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
