"""Фоновые редакционные прогоны (15–20 мин), чтобы чат не ждал их завершения."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

from listener.brain import SANDBOX, claude_bin, claude_error
from listener.pending import write_json
from listener.provider_errors import ProviderError

log = logging.getLogger("listener")


class _Deferred(Exception):
    """Обработчик временно недоступен; задание остаётся первым в очереди."""


class EditorialWorker:
    def __init__(self, runner, post, root=Path("."), today=lambda: date.today().isoformat(), submit_draft=None,
                 claude_available=lambda: True, on_provider_error=None, poll_seconds=1.0, retry_delay=30.0,
                 durable_delivery=None):
        self.runner, self.post, self.root, self.today = runner, post, Path(root), today
        self.submit_draft = submit_draft             # этап 3: принятая версия → сервер (картинка, кнопка ✅)
        self.claude_available, self.on_provider_error = claude_available, on_provider_error
        self.durable_delivery = bool(submit_draft) and (on_provider_error is not None if durable_delivery is None
                                                       else durable_delivery)
        self.poll_seconds, self.retry_delay = poll_seconds, retry_delay
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self.marker = self.root / "runs" / "chat" / "editorial-job.json"
        self.queue_path = self.marker.with_name("editorial-queue.json")
        self._pending, self._done_keys = self._load_queue()
        if self._pending:
            self._idle.clear()
            self._post("↻ Редакционные задания были прерваны перезапуском ПК-слушателя — продолжу по очереди")
        else:
            self._idle.set()
            self.marker.unlink(missing_ok=True)       # завершённая очередь важнее старой отметки
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @staticmethod
    def _valid_job(job):
        if not isinstance(job, dict) or job.get("type") not in ("run_editorial", "revise_editorial"):
            return False
        delivery = job.get("_delivery")
        if delivery is not None and (not isinstance(delivery, dict) or
                not isinstance(delivery.get("drafts"), list) or
                not all(isinstance(d, dict) and isinstance(d.get("id"), str) for d in delivery["drafts"]) or
                not isinstance(delivery.get("next"), int) or
                not 0 <= delivery["next"] <= len(delivery["drafts"])):
            return False
        return True

    def _load_queue(self):
        try:
            saved = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            interrupted = self._load_marker()
            jobs = [interrupted] if interrupted else []
            if jobs:
                write_json(self.queue_path, {"jobs": jobs, "done": []})
            return jobs, []
        # Повреждённую очередь не подменяем пустой: иначе задания незаметно потеряются.
        if not isinstance(saved, dict) or not isinstance(saved.get("jobs"), list) or \
                not all(self._valid_job(job) for job in saved["jobs"]) or \
                not isinstance(saved.get("done", []), list) or \
                not all(isinstance(key, str) for key in saved.get("done", [])):
            raise ValueError("Повреждена очередь редакционных заданий")
        return saved["jobs"], saved.get("done", [])[-200:]

    def _load_marker(self) -> dict | None:
        try:
            job = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return job if isinstance(job, dict) and job.get("type") in ("run_editorial", "revise_editorial") else None

    def submit(self, action: dict) -> None:
        if not self._valid_job(action):
            raise ValueError("Неизвестное редакционное задание")
        # Собственная копия: вызывающий поток не может изменить уже сохранённую задачу.
        action = json.loads(json.dumps(action, ensure_ascii=False))
        # Этапы генерации/доставки выставляет только worker, не текст модели или API.
        action = {key: value for key, value in action.items() if not key.startswith("_") or key == "_job_key"}
        key = action.get("_job_key")
        if key is not None and (not isinstance(key, str) or not key or len(key) > 200):
            raise ValueError("Неверный ключ редакционного задания")
        with self._condition:
            if self._stop.is_set():
                raise RuntimeError("Редакционный обработчик остановлен")
            if key and (key in self._done_keys or any(job.get("_job_key") == key for job in self._pending)):
                return
            pending = self._pending + [action]
            write_json(self.queue_path, {"jobs": pending, "done": self._done_keys})
            self._pending = pending
            self._idle.clear()
            self._condition.notify()

    def join_idle(self, timeout=None) -> bool:
        return self._idle.wait(timeout)

    def close(self, timeout=5):
        """Завершить ожидание, сохранив очередь; выполняющийся runner не прерывается."""
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout)

    def _finish(self, action, success):
        with self._condition:
            pending = self._pending[1:]
            done = self._done_keys
            key = action.get("_job_key")
            if success and key:
                done = (done + [key])[-200:]
            write_json(self.queue_path, {"jobs": pending, "done": done})
            self._pending, self._done_keys = pending, done
            try:
                self.marker.unlink(missing_ok=True)
            except OSError:
                # Очередь уже зафиксирована. Повтор _finish мог бы снять следующую задачу.
                log.warning("не удалось убрать старую отметку редакционного задания")
            if not pending:
                self._idle.set()

    def _persist_job(self, action, **updates):
        """Фиксировать этап до внешнего эффекта; сбой диска оставляет задачу ожидающей."""
        with self._condition:
            changed = {**action, **updates}
            pending = [changed, *self._pending[1:]]
            try:
                write_json(self.queue_path, {"jobs": pending, "done": self._done_keys})
            except OSError:
                log.warning("не удалось сохранить этап редакционного задания")
                raise _Deferred from None
            action.clear()
            action.update(changed)
            pending[0] = action
            self._pending = pending

    def _stage_delivery(self, action, paths):
        drafts = []
        for path in paths:
            draft = json.loads(Path(path).read_text(encoding="utf-8"))
            if action.get("at"):
                draft.update(at=action["at"], day=action.get("day"))
            drafts.append(draft)
        # Снимки защищают повтор от последующих правок файлов черновиков.
        self._persist_job(action, _delivery={"drafts": drafts, "next": 0})
        return self._deliver_pending(action)

    def _deliver_pending(self, action):
        delivery = action["_delivery"]
        while delivery["next"] < len(delivery["drafts"]):
            draft = delivery["drafts"][delivery["next"]]
            try:
                self.submit_draft(json.loads(json.dumps(draft, ensure_ascii=False)))
            except Exception as exc:
                log.warning("доставка принятого черновика отложена: %s", exc.__class__.__name__)
                raise _Deferred from None
            # Потеря ACK или сбой этого сохранения повторяет тот же id через идемпотентный /draft.
            delivery = {**delivery, "next": delivery["next"] + 1}
            self._persist_job(action, _delivery=delivery)
        return True

    def _loop(self):
        while not self._stop.is_set():
            with self._condition:
                while not self._pending and not self._stop.is_set():
                    self._condition.wait()
                if self._stop.is_set():
                    return
                action = self._pending[0]
            try:
                write_json(self.marker, action)
            except OSError:
                log.warning("не удалось сохранить отметку редакционного задания")
                self._stop.wait(self.poll_seconds)
                continue
            notified = False
            not_before = 0.0
            success = False
            while not self._stop.is_set():
                delivery_only = self.durable_delivery and "_delivery" in action
                if delivery_only:
                    available = True                # доставка готового текста не зависит от лимита модели
                else:
                    try:
                        available = self.claude_available()
                    except Exception:
                        log.warning("не удалось проверить доступность редакционного обработчика")
                        available = False
                if not available or time.monotonic() < not_before:
                    if not notified:
                        self._post("⏳ Готовые черновики сохранены. Повторю доставку на сервер." if delivery_only else
                                   "⏳ Редакционное задание сохранено. Продолжу, когда Claude снова станет доступен.")
                        notified = True
                    self._stop.wait(self.poll_seconds)
                    continue
                try:
                    if delivery_only:
                        success = self._deliver_pending(action)
                    elif action["type"] == "run_editorial":
                        success = self._run_editorial(action)
                    else:
                        success = self._revise(action)
                    break
                except _Deferred:
                    # Пауза защищает от повтора, даже если callback не смог сохранить cooldown.
                    not_before = time.monotonic() + self.retry_delay
                    continue
                except Exception:
                    log.exception("редакционное задание не удалось")
                    self._post("❌ Редакционное задание не удалось — подробности в журнале ПК")
                    break
            else:
                return                              # остаются и очередь, и marker
            # Если сохранение результата временно не удалось, не выполняем runner второй раз.
            while True:
                try:
                    self._finish(action, success)
                    break
                except OSError:
                    log.warning("не удалось сохранить завершение редакционного задания")
                    if self._stop.wait(self.poll_seconds):
                        return

    def _post(self, text):
        try:
            self.post(text)
        except Exception:                            # туннель/сервер недоступен — поток не должен умереть
            log.exception("не удалось отправить в чат")

    def _claude(self, prompt):
        cmd = [claude_bin(), "-p", prompt, *SANDBOX, "--output-format", "json"]
        try:
            proc = self.runner(cmd, cwd=str(self.root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               encoding="utf-8", timeout=3600)
        except subprocess.TimeoutExpired:
            if self.on_provider_error is None:
                raise
            error = ProviderError("claude", "timeout", "Редакционный обработчик не ответил вовремя")
        else:
            error = claude_error(proc)
            if self.on_provider_error is None or error is None or \
                    error.kind not in ("usage_limit", "auth", "network", "timeout", "rate_limit"):
                return proc
        try:
            self.on_provider_error(error)
        except Exception:
            log.warning("не удалось сохранить недоступность редакционного обработчика")
        raise _Deferred from None

    def _run_editorial(self, action):
        generation = action.get("_generation") if self.durable_delivery else None
        if generation is None:
            day = self.today()
            before = [p.name for p in (self.root / "runs" / day / "drafts").glob("*.json")]
            generation = {"day": day, "before": before}
            if self.durable_delivery:
                self._persist_job(action, _generation=generation)
        day = generation["day"]
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        channels = ", ".join(action.get("channels") or [])
        (inbox / "brief.md").write_text(action["brief"] + (f"\nКаналы: {channels}" if channels else "") + "\n",
                                        encoding="utf-8")
        run_dir = self.root / "runs" / day
        run_dir.mkdir(parents=True, exist_ok=True)
        before = set(generation["before"])            # baseline сохраняется до первого вызова модели
        started = time.time()
        proc = self._claude(f"/morning-run {day}")
        if proc.returncode != 0:
            self._post(f"❌ Редакционный прогон не удался (код {proc.returncode})")
            return False
        summary = run_dir / "summary.md"
        if not summary.exists() or summary.stat().st_mtime < started:
            self._post("❌ Редакционный прогон завершился без нового итога")
            return False
        self._post(summary.read_text(encoding="utf-8"))
        accepted = [draft for draft in sorted((run_dir / "drafts").glob("*.json"))
                    if draft.name not in before and self._accepted(run_dir, draft.stem)]
        if self.durable_delivery:
            return self._stage_delivery(action, accepted)
        for draft in accepted:
            self._deliver(draft, action)
        return True

    def _draft_day(self, draft_id) -> str:
        found = sorted((self.root / "runs").glob(f"*/drafts/{draft_id}.json"))
        return found[-1].parent.parent.name if found else self.today()

    def _revise(self, action):
        base = re.sub(r"-v\d+$", "", action["draft_id"])
        generation = action.get("_generation") if self.durable_delivery else None
        if generation is None:
            day = self._draft_day(action["draft_id"])    # ответ утром на вчерашний черновик
            drafts = self.root / "runs" / day / "drafts"
            generation = {"day": day, "before": {p.name: p.stat().st_mtime_ns
                                                   for p in drafts.glob(f"{base}-v*.json")}}
            if self.durable_delivery:
                self._persist_job(action, _generation=generation)
        day = generation["day"]
        drafts = self.root / "runs" / day / "drafts"
        before = generation["before"]
        notes = self.root / "runs" / "chat" / f"notes-{action['draft_id']}.txt"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(action["notes"], encoding="utf-8")
        proc = self._claude(f"/revise-draft {day} {action['draft_id']} runs/chat/{notes.name}")
        if proc.returncode != 0:
            self._post(f"❌ Правка черновика не удалась (код {proc.returncode})")
            return False
        versions = sorted(drafts.glob(f"{base}-v*.json"), key=lambda p: int(p.stem.rsplit("-v", 1)[1]))
        if not versions or before.get(versions[-1].name) == versions[-1].stat().st_mtime_ns:
            self._post("❌ Новая версия черновика не появилась")
            return False
        verdict = self._verdict(self.root / "runs" / day, versions[-1].stem)
        if verdict is not None and verdict.get("ok"):
            if self.durable_delivery:
                return self._stage_delivery(action, [versions[-1]])
            self._deliver(versions[-1])
            return True
        text = self._draft_text(versions[-1])
        if verdict is not None:                      # не принятая контролёром версия — только текстом, без ✅
            notes = (verdict.get("judgement") or {}).get("notes") or []
            text = f"⚠️ Контролёр не принял: {notes[-1] if notes else 'без пояснения'}\n\n{text}"
        self._post(text)
        return True

    def _deliver(self, path, action=None):
        """Принятая версия: на сервер (картинка и кнопка «Опубликовать»); без сервера — текстом, как раньше.
        Время выхода из задачи («пост на вечер») едет вместе с черновиком."""
        if self.submit_draft is not None:
            try:
                draft = json.loads(Path(path).read_text(encoding="utf-8"))
                if action and action.get("at"):
                    draft.update(at=action["at"], day=action.get("day"))
                self.submit_draft(draft)
                return
            except Exception:
                log.exception("черновик %s не принят сервером — отправляю текстом", Path(path).stem)
        self._post(self._draft_text(path))

    @staticmethod
    def _verdict(run_dir, draft_id) -> dict | None:
        try:
            return json.loads((run_dir / "verdicts" / f"{draft_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

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
