"""Фоновые редакционные прогоны (15–20 мин), чтобы чат не ждал их завершения."""
from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
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
                self.post("❌ Редакционное задание не удалось — подробности в журнале ПК")
            finally:
                if self.jobs.empty():
                    self._idle.set()

    def _claude(self, prompt):
        cmd = [claude_bin(), "-p", prompt, *SANDBOX, "--output-format", "json"]
        return self.runner(cmd, cwd=str(self.root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", timeout=3600)

    def _run_editorial(self, action):
        day = self.today()
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "brief.md").write_text(action["brief"] + "\n", encoding="utf-8")
        run_dir = self.root / "runs" / day
        run_dir.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in (run_dir / "drafts").glob("*.json")}   # утренние черновики не пересылать
        self._claude(f"/morning-run {day}")
        summary = run_dir / "summary.md"
        self.post(summary.read_text(encoding="utf-8") if summary.exists() else "Редакционный прогон завершён без итога")
        for draft in sorted((run_dir / "drafts").glob("*.json")):
            if draft.name not in before and self._accepted(run_dir, draft.stem):
                self.post(self._draft_text(draft))

    def _revise(self, action):
        day = self.today()
        notes = self.root / "runs" / "chat" / f"notes-{action['draft_id']}.txt"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(action["notes"], encoding="utf-8")
        self._claude(f"/revise-draft {day} {action['draft_id']} runs/chat/{notes.name}")
        base = re.sub(r"-v\d+$", "", action["draft_id"])
        drafts = self.root / "runs" / day / "drafts"
        versions = sorted(drafts.glob(f"{base}-v*.json"), key=lambda p: int(p.stem.rsplit("-v", 1)[1]))
        self.post(self._draft_text(versions[-1]) if versions else "❌ Новая версия черновика не появилась")

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
