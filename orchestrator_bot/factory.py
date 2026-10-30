"""Вызовы content-factory через `python -m content_factory.agent_cli` (venv и cwd content-factory)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import threading
from pathlib import Path



class FactoryError(RuntimeError):
    pass


HINT_TTL = 600            # подсказка из каталога: разбор всех прайсов дорогой, держим 10 минут


class Factory:
    def __init__(self, cf_python: str, cf_root: str, runner=subprocess.run, timeout: int = 120,
                 clock=time.time):
        self.cf_python, self.cf_root, self.runner, self.timeout = cf_python, cf_root, runner, timeout
        self.clock = clock
        self._hint: tuple[float, dict] | None = None
        self._publish_lock=threading.RLock()

    def _run(self, *args) -> dict:
        catalog = args[0] in ('hint', 'resolve', 'enqueue', 'enqueue-supplier', 'supplier-photo-status', 'resume-photo', 'correct-photo', 'update-card-price', 'photo-review-scope', 'publish-audited', 'batch-status', 'publish-keys', 'cancel')
        module = 'orchestrator_bot.catalog_cli' if catalog else 'content_factory.agent_cli'
        cmd = [self.cf_python, "-m", module, *args]
        # content-factory не установлен пакетом: его юниты задают PYTHONPATH=<root>/src — делаем так же
        python_path = [f'{self.cf_root}/src']
        if catalog:
            python_path.append(str(Path(__file__).resolve().parents[1]))
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(python_path)}
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
        if action.get('selection'):
            args += ['--selection', action['selection']]
        if action.get('keys') is not None:
            args += ['--keys-json', json.dumps(action['keys'], ensure_ascii=False)]
        if action.get("btu"):
            args += ["--btu", "-".join(str(b) for b in action["btu"])]
        if action.get("min_temp"):
            args += ["--min-temp", str(action["min_temp"])]
        args += [f"--{flag.replace('_', '-')}" for flag in ("inverter", "heat_pump", "full_dc") if action.get(flag)]
        return args

    def hint(self) -> dict:
        now = self.clock()
        if self._hint is None or now - self._hint[0] > HINT_TTL:
            self._hint = (now, self._run("hint"))
        return self._hint[1]

    def resolve(self, action: dict) -> dict:
        return self._run("resolve", *self._select_args(action))

    def enqueue(self, action: dict) -> dict:
        # Время из слов владельца — время ВЫХОДА партии (владелец, 2026-10-21), а не старта генерации:
        # карточки делаются сразу, партия публикуется в названное время (feed.publish_at).
        return self._run("enqueue", *self._select_args(action))

    def enqueue_supplier(self, action: dict, source_id: str, attachments=(), *, new_only=False, document=None) -> dict:
        # Images travel through a temporary file: Windows command lines cannot
        # safely carry large base64 payloads. Close it before the subprocess opens it.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fh:
            path = fh.name
            bundle = {"action": action, "source_id": source_id, "attachments": list(attachments)}
            if document is not None:
                bundle['document'] = document
            if new_only:
                bundle['new_only'] = True
            json.dump(bundle, fh, ensure_ascii=False)
        try:
            return self._run("enqueue-supplier", "--supplier-file", path)
        finally:
            os.unlink(path)

    def reconcile_photo(self, decision):
        return self._run('resume-photo', '--audit-json', json.dumps(decision, ensure_ascii=False))

    def supplier_photo_status(self, inventory):
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=False) as fh:
            path = fh.name
            json.dump(inventory, fh, ensure_ascii=False)
        try:
            return self._run('supplier-photo-status', '--supplier-file', path)
        finally:
            os.unlink(path)

    def correct_photo(self, decision):
        return self._run('correct-photo', '--feedback-json', json.dumps(decision, ensure_ascii=False))

    def update_card_price(self, decision):
        return self._run('update-card-price', '--feedback-json', json.dumps(decision, ensure_ascii=False))

    def photo_review_key(self, job_id):
        return self._run('photo-review-scope', '--job-id', str(job_id)).get('item_key')

    def cancel(self, keys: list[str]) -> dict:
        return self._run('cancel', '--keys-json', json.dumps(keys, ensure_ascii=False))

    def workers(self) -> dict:
        return self._run("workers")

    def start_workers(self) -> dict:
        return self._run("start-workers")

    def status(self, since: float) -> dict:
        return self._run("status", "--since", str(since))

    # ── единая лента ──
    def feed(self) -> dict:
        return self._run("feed")

    def approve_fresh(self) -> dict:
        return self._run("approve-fresh")

    def expire_stale(self) -> dict:
        return self._run("expire-stale")

    def vk_approve(self) -> dict:
        return self._run("vk-approve")

    def batch_status(self, keys: list[str]) -> dict:
        return self._run('batch-status', '--keys-json', json.dumps(keys, ensure_ascii=False))

    def publish_keys(self, keys: list[str]) -> dict:
        with self._publish_lock:
            return self._run('publish-keys', '--keys-json', json.dumps(keys, ensure_ascii=False))

    def publish_audited(self, decision) -> dict:
        with self._publish_lock:
            return self._run('publish-audited','--audit-json',json.dumps(decision,ensure_ascii=False))

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

    def compose(self, key: str, channel: str, text: str) -> str:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as fh:
            fh.write(text)
        try:
            return self._run("compose", "--key", key, "--channel", channel, "--caption-file", fh.name)["text"]
        finally:
            os.unlink(fh.name)

    def publish_tg(self, key: str, caption: str, image: str) -> dict:
        return self._publish("publish-tg", key, caption, image)

    def plan_vk(self, key: str, caption: str, image: str) -> dict:
        return self._publish("plan-vk", key, caption, image)
