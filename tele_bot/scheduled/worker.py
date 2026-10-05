"""Two-thread scheduler/worker backed by atomic SQLite occurrence claims."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from tele_bot.models import OutgoingMessage
from tele_bot.scheduled.store import ScheduleStore, now_utc
from tele_bot.tools.codex_runner import MAX_CODEX_TIMEOUT_SECONDS

LOG = logging.getLogger(__name__)


class ExecutionCancelled(Exception):
    pass


class ServiceStopping(Exception):
    pass


class ScheduleWorker:
    def __init__(self, store: ScheduleStore, send=None, *, poll_seconds: float = 2,
                 execution_timeout: int = MAX_CODEX_TIMEOUT_SECONDS, runner=None) -> None:
        self.store = store
        self.send = send
        self.poll_seconds = poll_seconds
        self.execution_timeout = max(1, min(execution_timeout, MAX_CODEX_TIMEOUT_SECONDS))
        self.runner = runner or self._subprocess_runner
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self._lease_file = None

    def start(self) -> None:
        if self.threads:
            return
        if not self._acquire_lease():
            LOG.warning("another process owns the scheduled-task worker lease")
            return
        for row in self.store.recover():
            self._notify(row, f"任务 {row['task_id']} 的执行 {row['run_id']} 因服务中断而失败。")
        for name, target in (("schedule-clock", self._clock_loop), ("schedule-executor", self._execute_loop)):
            thread = threading.Thread(name=name, target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=15)
        if all(not thread.is_alive() for thread in self.threads):
            self.threads.clear()
            self._release_lease()

    def _acquire_lease(self) -> bool:
        path = Path(self.store.path + ".worker.lock")
        lease = path.open("a+b")
        try:
            lease.seek(0)
            if not lease.read(1):
                lease.seek(0)
                lease.write(b"0")
                lease.flush()
            lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lease.close()
            return False
        self._lease_file = lease
        return True

    def _release_lease(self) -> None:
        lease = self._lease_file
        if lease is None:
            return
        lease.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        lease.close()
        self._lease_file = None

    def _clock_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                LOG.exception("scheduled clock failed")
            self.stop_event.wait(self.poll_seconds)

    def tick(self, current: datetime | None = None) -> None:
        current = current or now_utc()
        if self.send is not None:
            for notice in self.store.pending_notifications(current):
                self._deliver(notice["id"])
        for row in self.store.expire_authorizations(current):
            self._notify(row, f"任务 {row['task_id']} 的本次授权已过期，已跳过。")
        for task in self.store.due(current):
            occurrence = self.store.trigger(task["id"], task["next_at"], current)
            if not occurrence:
                continue
            run_id = occurrence["run_id"]
            task_id = task["id"]
            state = occurrence["run_status"]
            feedback_task = {**task, "run_id": run_id}
            if state == "awaiting_auth":
                scope = json.loads(task["scope_json"])
                self.store.event("authorization_requested", task_id, run_id, task["owner_id"], "pending")
                self._notify(feedback_task, f"任务 {task_id} 已到计划时间。内容：{task['content']}\n"
                             f"允许工具：{scope['tools']}；目录：{scope['directories']}。\n"
                             f"本次授权截止：{occurrence['auth_expires_at']}。\n"
                             f"回复：授权 {run_id}，或：拒绝 {run_id}", kind="auth_request")
            elif state == "reminded":
                self._notify(feedback_task, f"定时提醒 {task_id}：{task['content']}")
            elif state == "queued":
                self._notify(feedback_task, f"任务 {task_id} 已到计划时间，进入自动执行队列。")
            elif state == "skipped":
                self._notify(feedback_task, f"任务 {task_id} 本次已跳过：{occurrence['reason']}。")

    def _execute_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                worked = self.execute_one()
            except Exception:  # noqa: BLE001
                LOG.exception("scheduled executor failed")
                worked = False
            if not worked:
                self.stop_event.wait(self.poll_seconds)

    def execute_one(self) -> bool:
        run = self.store.claim_run()
        if not run:
            return False
        payload = {"run_id": run["id"], "task_id": run["task_id"],
                   "owner_id": run["owner_id"], "chat_id": run["chat_id"],
                   "content": run["content"], "planned_at": run["planned_at"],
                   "scope": json.loads(run["scope_json"])}
        if self.send is not None:
            self._notify({**run, "run_id": run["id"]},
                         f"任务 {run['task_id']} 开始执行，请稍候。", kind="started")
        try:
            outcome = self.runner(payload, self.execution_timeout)
            if isinstance(outcome, dict):
                status = outcome["status"]
                result = outcome["result"]
            else:
                status, result = "success", str(outcome)
            error = ""
        except subprocess.TimeoutExpired:
            status, result, error = "timeout", "执行超过时间限制。", "execution timeout"
        except ExecutionCancelled:
            status, result, error = "cancelled", "执行期间任务被取消。", ""
        except ServiceStopping:
            return True  # recovery marks the unfinished run after restart
        except Exception as exc:  # noqa: BLE001
            status, result, error = "failed", "执行失败。", str(exc)
        row = self.store.finish_run(run["id"], status, result, error)
        if row:
            next_at = f"\n下次触发：{row['next_at']}" if row["next_at"] else ""
            self._notify(row, f"任务 {run['task_id']} 本次执行{status}。\n结果：{result}"
                         + (f"\n错误：{error}" if error else "") + next_at)
        return True

    def _subprocess_runner(self, payload: dict, timeout: int) -> dict:
        project_root = Path(__file__).resolve().parents[2]
        process = subprocess.Popen(
            [sys.executable, "-m", "tele_bot.scheduled.execution"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=project_root,
        )
        input_text = json.dumps(payload, ensure_ascii=False)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.stop_event.is_set():
                    raise ServiceStopping()
                if self.store.is_run_cancelled(payload["run_id"]):
                    raise ExecutionCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                try:
                    stdout, stderr = process.communicate(input=input_text, timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    input_text = None  # communicate may only receive input on its first call
                    continue
        except (ServiceStopping, ExecutionCancelled, subprocess.TimeoutExpired):
            process.kill()
            process.communicate()
            raise
        try:
            data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError((stderr or stdout)[-1000:] or "executor returned no result") from exc
        if process.returncode or not data.get("ok"):
            raise RuntimeError(data.get("error") or stderr[-1000:])
        return {"status": data["status"], "result": str(data["result"])}

    def _notify(self, task: dict, text: str, kind: str = "generic") -> None:
        notice = self.store.enqueue_notification(task, text, kind)
        if self.send is None:
            self.store.event("feedback_unavailable", notice["task_id"], notice["run_id"],
                             notice["user_id"], "pending", text[:500])
            return
        self._deliver(notice["id"])

    def _deliver(self, notice_id: str) -> None:
        notice = self.store.claim_notification(notice_id)
        if notice is None:
            return
        if notice["kind"] == "auth_request" and not self.store.is_authorizable(notice["run_id"]):
            self.store.supersede_notification(notice)
            return
        if notice["kind"] == "started" and not self.store.is_run_running(notice["run_id"]):
            self.store.supersede_notification(notice)
            return
        try:
            result = self.send(OutgoingMessage(notice["channel"], notice["chat_id"], notice["body"]))
            if isinstance(result, dict) and result.get("code") not in (None, 0):
                raise RuntimeError(str(result.get("msg", "send failed")))
            self.store.mark_notification(notice)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("scheduled feedback failed")
            self.store.mark_notification(notice, str(exc))
