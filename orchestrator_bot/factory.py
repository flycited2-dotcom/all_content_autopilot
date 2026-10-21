"""Вызовы content-factory через `python -m content_factory.agent_cli` (venv и cwd content-factory)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

from orchestrator_bot.intents import due_text


class FactoryError(RuntimeError):
    pass


HINT_TTL = 600            # подсказка из каталога: разбор всех прайсов дорогой, держим 10 минут


class Factory:
    def __init__(self, cf_python: str, cf_root: str, runner=subprocess.run, timeout: int = 120,
                 clock=time.time):
        self.cf_python, self.cf_root, self.runner, self.timeout = cf_python, cf_root, runner, timeout
        self.clock = clock
        self._hint: tuple[float, dict] | None = None

    def _run(self, *args) -> dict:
        cmd = [self.cf_python, "-m", "content_factory.agent_cli", *args]
        # content-factory не установлен пакетом: его юниты задают PYTHONPATH=<root>/src — делаем так же
        env = {**os.environ, "PYTHONPATH": f"{self.cf_root}/src"}
        proc = self.runner(cmd, cwd=self.cf_root, capture_output=True, text=True,
                           encoding="utf-8", timeout=self.timeout, env=env)
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
        now = self.clock()
        if self._hint is None or now - self._hint[0] > HINT_TTL:
            self._hint = (now, self._run("hint"))
        return self._hint[1]

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

    def workers(self) -> dict:
        return self._run("workers")

    def start_workers(self) -> dict:
        return self._run("start-workers")

    def status(self, since: float) -> dict:
        return self._run("status", "--since", str(since))

    # ── этап 3: посты оркестратора ──
    def visual_submit(self, scene: str, request_key: str, new: bool = False) -> dict:
        return self._run("visual-submit", "--scene", scene, "--request-key", request_key, *(["--new"] if new else []))

    def visual_status(self, job_id: int) -> dict:
        return self._run("visual-status", "--job", str(job_id))

    def _publish(self, command: str, key: str, caption: str, image: str) -> dict:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as fh:
            fh.write(caption)
        try:
            return self._run(command, "--key", key, "--caption-file", fh.name, "--image", image)
        finally:
            os.unlink(fh.name)

    def publish_tg(self, key: str, caption: str, image: str) -> dict:
        return self._publish("publish-tg", key, caption, image)

    def plan_vk(self, key: str, caption: str, image: str) -> dict:
        return self._publish("plan-vk", key, caption, image)
