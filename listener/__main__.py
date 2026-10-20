"""python -m listener — слушатель на ПК: очередь с VPS → Claude → намерение обратно.

Окружение: ORCH_API_TOKEN (обязательно), ORCH_API_URL (http://127.0.0.1:8790),
ORCH_TUNNEL_KEY (путь к ключу туннеля), ORCH_SSH_HOST (212.116.115.150), ORCH_SSH_PORT (2222).
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from listener import brain, lifecycle, session
from listener.client import ApiClient
from listener.editorial import EditorialWorker
from listener.tunnel import Tunnel

log = logging.getLogger("listener")
ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 3
HEARTBEAT_SECONDS = 30


def process_once(api, worker, turn, sess) -> bool:
    """Забрать одно сообщение и обработать; True — если было что обрабатывать."""
    data = api.next()
    message = data.get("message")
    if not message:
        return False
    intent = turn({"message": message, "context": data.get("context") or {}}, sess)
    result = api.intent(message["id"], intent)
    for action in result.get("editorial") or []:
        worker.submit(action)
    return True


def main() -> int:
    log_file = Path(os.environ.get("ORCH_LOG_FILE", ROOT / "runs" / "chat" / "listener.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # только файл: под pythonw (задача Планировщика без окна) stdout/stderr отсутствуют
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_file, encoding="utf-8")])
    for name in ("httpx", "httpcore"):          # не писать каждый опрос раз в 3 с
        logging.getLogger(name).setLevel(logging.WARNING)
    lock = lifecycle.acquire_instance_lock()
    if lock is None:
        log.info("слушатель уже запущен — выход")
        return 0
    lifecycle.kill_stale_tunnels()
    token = os.environ["ORCH_API_TOKEN"]
    api = ApiClient(os.environ.get("ORCH_API_URL", "http://127.0.0.1:8790"), token)
    tunnel = None
    key = os.environ.get("ORCH_TUNNEL_KEY") or str(Path.home() / ".ssh" / "id_agents_tunnel")
    if Path(key).exists():
        tunnel = Tunnel(["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                         "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                         "-i", key, "-p", os.environ.get("ORCH_SSH_PORT", "2222"),
                         "-L", "8790:127.0.0.1:8790", f"root@{os.environ.get('ORCH_SSH_HOST', '212.116.115.150')}"])
        atexit.register(tunnel.close)
    log.info("слушатель запущен")
    worker = EditorialWorker(brain.run_with_kill, api.reply, root=ROOT)
    turn = lambda payload, sess: brain.run_turn(payload, sess, root=ROOT)  # noqa: E731
    last_beat = 0.0
    while True:
        try:
            if tunnel:
                tunnel.ensure()
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                api.heartbeat()
                last_beat = time.time()
            sess = session.day_session(datetime.now(), ROOT / "runs" / "chat" / "session.json")
            if not process_once(api, worker, turn, sess):
                time.sleep(POLL_SECONDS)
        except Exception:
            log.exception("цикл слушателя")
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
