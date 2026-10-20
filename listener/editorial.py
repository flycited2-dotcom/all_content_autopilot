"""Фоновые редакционные прогоны (15–20 мин), чтобы чат не ждал их завершения."""
from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

from listener.brain import SANDBOX, claude_bin

log = logging.getLogger("listener")


class EditorialWorker:
    def __init__(self, runner, post, root=Path("."), today=lambda: date.today().isoformat()):
        self.runner, self.post, self.root, self.today = runner, post, Path(root), today
        self.jobs: queue.Queue = queue.Queue()
        self._idle = threading.Event()
        self._idle.set()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, action: dict) -> None:
        self._idle.clear()
        self.jobs.put(action)

    def join_idle(self, timeout=None) -> bool:
        return self._idle.wait(timeout)

    def _loop(self):
        while True:
            action = self.jobs.get()
            try:
                if action["type"] == "run_editorial":
                    self._run_editorial(action)
                elif action["type"] == "revise_editorial":
                    self._revise(action)
            except Exception:
                log.exception("редакционное задание не удалось")
                self._post("❌ Редакционное задание не удалось — подробности в журнале ПК")
            finally:
                if self.jobs.empty():
                    self._idle.set()

    def _post(self, text):
        try:
            self.post(text)
        except Exception:                            # туннель/сервер недоступен — поток не должен умереть
            log.exception("не удалось отправить в чат")

    def _claude(self, prompt):
        cmd = [claude_bin(), "-p", prompt, *SANDBOX, "--output-format", "json"]
        return self.runner(cmd, cwd=str(self.root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", timeout=3600)

    def _run_editorial(self, action):
        day = self.today()
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        channels = ", ".join(action.get("channels") or [])
        (inbox / "brief.md").write_text(action["brief"] + (f"\nКаналы: {channels}" if channels else "") + "\n",
                                        encoding="utf-8")
        run_dir = self.root / "runs" / day
        run_dir.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in (run_dir / "drafts").glob("*.json")}   # утренние черновики не пересылать
        started = time.time()
        proc = self._claude(f"/morning-run {day}")
        if proc.returncode != 0:
            self._post(f"❌ Редакционный прогон не удался (код {proc.returncode}): {(proc.stderr or '')[-200:]}")
            return
        summary = run_dir / "summary.md"
        if not summary.exists() or summary.stat().st_mtime < started:
            self._post("❌ Редакционный прогон завершился без нового итога")
            return
        self._post(summary.read_text(encoding="utf-8"))
        for draft in sorted((run_dir / "drafts").glob("*.json")):
            if draft.name not in before and self._accepted(run_dir, draft.stem):
                self._post(self._draft_text(draft))

    def _draft_day(self, draft_id) -> str:
        found = sorted((self.root / "runs").glob(f"*/drafts/{draft_id}.json"))
        return found[-1].parent.parent.name if found else self.today()

    def _revise(self, action):
        day = self._draft_day(action["draft_id"])        # ответ утром на вчерашний черновик
        notes = self.root / "runs" / "chat" / f"notes-{action['draft_id']}.txt"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(action["notes"], encoding="utf-8")
        proc = self._claude(f"/revise-draft {day} {action['draft_id']} runs/chat/{notes.name}")
        if proc.returncode != 0:
            self._post(f"❌ Правка черновика не удалась (код {proc.returncode})")
            return
        base = re.sub(r"-v\d+$", "", action["draft_id"])
        drafts = self.root / "runs" / day / "drafts"
        versions = sorted(drafts.glob(f"{base}-v*.json"), key=lambda p: int(p.stem.rsplit("-v", 1)[1]))
        self._post(self._draft_text(versions[-1]) if versions else "❌ Новая версия черновика не появилась")

    @staticmethod
    def _accepted(run_dir, draft_id) -> bool:
        verdict = run_dir / "verdicts" / f"{draft_id}.json"
        try:
            return bool(json.loads(verdict.read_text(encoding="utf-8")).get("ok"))
        except (OSError, ValueError):
            return False

    @staticmethod
    def _draft_text(path) -> str:
        draft = json.loads(Path(path).read_text(encoding="utf-8"))
        return f"📝 {draft.get('title', '')}\n\n{draft.get('text', '')}\n\nid: {draft.get('id', Path(path).stem)}"
