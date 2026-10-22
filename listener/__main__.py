"""python -m listener — очередь с VPS → Claude / резерв ChatGPT → намерение обратно.

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

from listener import brain, lifecycle, morning, session
from listener.client import ApiClient
from listener.editorial import EditorialWorker
from listener.intent_validation import validated_intent
from listener.pending import PendingTurn
from listener.provider_errors import ProvidersUnavailable
from listener.routing import ModelRouter
from listener.tunnel import Tunnel

log = logging.getLogger("listener")
ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 3
HEARTBEAT_SECONDS = 30


def process_once(api, worker, turn, sess, pending=None) -> bool:
    """Забрать одно сообщение и обработать; True — если было что обрабатывать."""
    saved = pending.load() if pending else None
    if saved is None:
        data = api.next()
        message = data.get("message")
        if not message:
            return False
        payload = {"message": message, "context": data.get("context") or {}}
        saved = {"payload": payload, "intent": None, "editorial": None}
        if pending:
            pending.save(payload)
    payload = saved["payload"]
    intent = saved.get("intent")
    if intent is None:
        intent = turn(payload, sess)
        if pending:
            pending.save(payload, intent)
    editorial = saved.get("editorial")
    if editorial is None:
        result = api.intent(payload["message"]["id"], validated_intent(intent))
        editorial = result.get("editorial") or []
        if pending:
            pending.save(payload, intent, editorial)
    for index, action in enumerate(editorial):
        worker.submit({**action, "_job_key": f"{payload['message']['id']}:{index}"})
    if pending:
        pending.clear()
    return True


def main() -> int:
    log_file = Path(os.environ.get("ORCH_LOG_FILE", ROOT / "runs" / "chat" / "listener.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # только файл: слушатель живёт в скрытой консоли (conhost --headless), вывод туда никто не читает
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
    router = ModelRouter(ROOT, notify=api.reply)
    log.info("резерв диалога Claude → ChatGPT включён; вход ChatGPT: %s",
             "готов" if router.chatgpt.ready() else "ожидается")
    worker = EditorialWorker(brain.run_with_kill, api.reply, root=ROOT, submit_draft=api.draft,
                             claude_available=router.claude_available, on_provider_error=router.record_failure)
    pending = PendingTurn(ROOT / "runs" / "chat" / "pending-turn.json")
    turn = router.run_turn
    last_beat = 0.0
    parent = os.getppid()
    while True:
        if not lifecycle.parent_alive(parent):          # «Остановить» в Планировщике погасил conhost — выходим
            log.info("родительский процесс завершён — слушатель останавливается")
            return 0
        try:
            if tunnel:
                tunnel.ensure()
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                api.heartbeat()
                last_beat = time.time()
            morning.maybe_run(datetime.now(), ROOT / "runs" / "chat" / "morning.json", worker, api.reply)
            sess = session.day_session(datetime.now(), ROOT / "runs" / "chat" / "session.json")
            if not process_once(api, worker, turn, sess, pending=pending):
                time.sleep(POLL_SECONDS)
        except ProvidersUnavailable:
            # Обновляем heartbeat в следующем цикле; роутер сам выдерживает паузы между попытками.
            time.sleep(POLL_SECONDS)
        except Exception:
            log.exception("цикл слушателя")
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
