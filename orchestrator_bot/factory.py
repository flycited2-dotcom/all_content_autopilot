"""Вызовы content-factory через `python -m content_factory.agent_cli` (venv и cwd content-factory)."""
from __future__ import annotations

import json
import subprocess

from orchestrator_bot.intents import due_text


class FactoryError(RuntimeError):
    pass


class Factory:
    def __init__(self, cf_python: str, cf_root: str, runner=subprocess.run, timeout: int = 120):
        self.cf_python, self.cf_root, self.runner, self.timeout = cf_python, cf_root, runner, timeout

    def _run(self, *args) -> dict:
        cmd = [self.cf_python, "-m", "content_factory.agent_cli", *args]
        proc = self.runner(cmd, cwd=self.cf_root, capture_output=True, text=True,
                           encoding="utf-8", timeout=self.timeout)
        if proc.returncode != 0:
            raise FactoryError(f"agent_cli {args[0]}: код {proc.returncode}: {(proc.stderr or proc.stdout or '')[-300:]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise FactoryError(f"agent_cli {args[0]}: не JSON: {exc}") from exc

    @staticmethod
    def _select_args(action: dict) -> list[str]:
        args = ["--category", action["category"]]
        if action.get("count"):
            args += ["--count", str(action["count"])]
        if action.get("quotas"):
            args += ["--quotas", json.dumps(action["quotas"], ensure_ascii=False)]
        if action.get("source"):
            args += ["--source", action["source"]]
        return args

    def hint(self) -> dict:
        return self._run("hint")

    def resolve(self, action: dict) -> dict:
        return self._run("resolve", *self._select_args(action))

    def enqueue(self, action: dict) -> dict:
        args = self._select_args(action)
        due = due_text(action)
        if due:
            args += ["--due", due]
        return self._run("enqueue", *args)

    def cancel(self, keys: list[str]) -> dict:
        return self._run("cancel", "--keys", ",".join(keys))

    def status(self, since: float) -> dict:
        return self._run("status", "--since", str(since))
