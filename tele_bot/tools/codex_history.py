"""Restricted local Codex App Server client. No transcript or RPC-body logging."""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from collections import deque

from tele_bot.tools.codex_runner import CodexRunner, MAX_CODEX_TIMEOUT_SECONDS


class CodexHistoryError(RuntimeError):
    """An error safe to display without leaking server stderr or credentials."""


class AppServerConnection:
    """One operation per owned stdio process; closing it releases subscriptions."""

    METHODS = frozenset({"initialize", "thread/list", "thread/read", "thread/turns/list",
                         "thread/resume", "turn/start", "turn/interrupt"})
    MAX_LINE_BYTES = 16 * 1024 * 1024

    def __init__(self, runner: CodexRunner):
        self.runner = runner
        self.process = None
        self.messages = queue.Queue(maxsize=256)
        self.events = deque()
        self.request_id = 0
        self.failed = threading.Event()

    def __enter__(self):
        executable = [arg[1:-1] if len(arg) > 1 and arg[0] == arg[-1] and arg[0] in "\"'" else arg
                      for arg in shlex.split(self.runner.command, posix=False)]
        if not executable:
            raise CodexHistoryError("Codex CLI 命令未配置。")
        command = [*executable, "app-server"]
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.runner._safe_environment(),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                start_new_session=os.name != "nt",
            )
            threading.Thread(target=self._read_stdout, args=(self.process,), daemon=True,
                             name="codex-history-rpc").start()
            self.rpc("initialize", {"clientInfo": {"name": "telebot_history", "version": "1.0.0"}})
            self._send({"method": "initialized", "params": {}})
            return self
        except (OSError, CodexHistoryError) as exc:
            self.close()
            if isinstance(exc, CodexHistoryError):
                raise
            raise CodexHistoryError("无法启动本机 Codex，请检查 CLI 安装及运行账户。") from None

    def __exit__(self, *args):
        self.close()

    def _read_stdout(self, process):
        try:
            while True:
                line = process.stdout.readline(self.MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > self.MAX_LINE_BYTES:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    break
                # Token deltas, tool output and reasoning never enter the queue.
                method = message.get("method")
                if method == "item/completed" and (
                    message.get("params", {}).get("item", {}).get("type") != "agentMessage"
                ):
                    continue
                if "id" not in message and method not in {"item/completed", "turn/completed"}:
                    continue
                self.messages.put_nowait(message)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            self.failed.set()

    def _send(self, message):
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise CodexHistoryError("Codex 连接已断开，请重新查询。") from None

    def _receive(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexHistoryError("Codex 请求超时，请稍后重试。")
            try:
                message = self.messages.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if self.failed.is_set():
                    raise CodexHistoryError("Codex 连接中断或响应格式异常，请检查 CLI 版本和运行账户。") from None
                continue
            if "id" in message and "method" in message:
                # Never grant server-initiated approvals, tool calls or permissions.
                self._send({"id": message["id"], "error": {
                    "code": -32601, "message": "Telebot does not grant interactive permissions"}})
                continue
            return message

    def rpc(self, method, params, *, timeout=30):
        if method not in self.METHODS:
            raise CodexHistoryError("不支持此 Codex 操作。")
        self.request_id += 1
        request_id = self.request_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(deadline)
            if message.get("id") == request_id:
                if "error" in message:
                    code = message["error"].get("code")
                    raise CodexHistoryError(f"Codex 接口 {method} 调用失败（错误码 {code}），请检查 CLI 版本、会话或登录状态。")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CodexHistoryError("Codex 返回了无法识别的结果。")
                return result
            if "method" in message:
                if len(self.events) >= 256:
                    raise CodexHistoryError("Codex 待处理事件过多，请重试。")
                self.events.append(message)

    def wait_turn(self, thread_id, turn_id, *, deadline, progress=None):
        final, commentary = [], []
        total = 0
        while True:
            message = self.events.popleft() if self.events else self._receive(deadline)
            params = message.get("params", {})
            if params.get("threadId") != thread_id:
                continue
            if message.get("method") == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item", {})
                text = item.get("text", "")
                if text and total < 20_000:
                    (final if item.get("phase") == "final_answer" else commentary).append(text[:20_000 - total])
                    total += len(text)
                if progress:
                    progress("Codex 正在继续所选会话，请稍候…")
            if message.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                turn = params["turn"]
                status = turn.get("status")
                if status != "completed":
                    raise CodexHistoryError("Codex 本轮执行失败或已中断；请查看该会话的最新记录，避免直接重复执行。")
                text = "\n\n".join(final or commentary)
                if not text:
                    text = "本轮已结束，未返回文本；可打开该会话查看最新记录。"
                if total > 20_000:
                    text += "\n（结果过长，完整内容请查看会话。）"
                return text

    def close(self):
        process = self.process
        if process is None:
            return
        self.process = None
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # This PID belongs exclusively to this connection, including npm shims.
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        finally:
            process.stdout.close()


class CodexHistoryClient:
    """Expose only history reads and explicitly requested, scoped continuations."""

    def __init__(self, runner: CodexRunner, connection_factory=AppServerConnection):
        self.runner = runner
        self.connection_factory = connection_factory
        self.execution_lock = threading.Lock()

    def list_threads(self, *, cursor=None, search="", archived=False):
        params = {"limit": 10, "cursor": cursor, "sortKey": "updated_at", "sortDirection": "desc",
                  "sourceKinds": ["cli", "vscode", "exec", "appServer"], "archived": archived}
        if search:
            params["searchTerm"] = search
        with self.connection_factory(self.runner) as server:
            return server.rpc("thread/list", params)

    def read_thread(self, thread_id):
        with self.connection_factory(self.runner) as server:
            return server.rpc("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]

    def read_turns(self, thread_id, cursor=None):
        with self.connection_factory(self.runner) as server:
            return server.rpc("thread/turns/list", {"threadId": thread_id, "cursor": cursor,
                              "limit": 3, "sortDirection": "desc", "itemsView": "full"})

    def _workspace(self, thread):
        raw = thread.get("cwd")
        if not raw:
            raise CodexHistoryError("该会话没有工作目录，无法安全恢复执行。")
        try:
            target = self.runner.policy.resolve_existing(raw)
            if not target.is_dir() or not self.runner._workspace_allowed(target):
                raise ValueError("workspace outside configured root")
        except (OSError, ValueError):
            raise CodexHistoryError("会话工作目录不存在或不在 Codex 允许的工作区内，无法恢复执行。") from None
        return str(target)

    def resume(self, thread_id, prompt, *, progress=None):
        if not prompt.strip() or len(prompt) > 30_000:
            raise CodexHistoryError("请输入 1–30000 字的继续执行任务。")
        if not self.execution_lock.acquire(blocking=False):
            raise CodexHistoryError("已有一项 Codex 恢复任务正在执行，请等待完成。")
        try:
            with self.connection_factory(self.runner) as server:
                thread = server.rpc("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                if thread.get("status", {}).get("type") == "active":
                    raise CodexHistoryError("该会话正在执行，请等待本轮结束后再继续。")
                cwd = self._workspace(thread)
                model = thread.get("model") or self.runner.model
                resume_params = {
                    "threadId": thread_id, "cwd": cwd, "excludeTurns": True,
                    "model": model, "sandbox": "workspace-write",
                    "approvalPolicy": "never", "approvalsReviewer": "user",
                }
                if thread.get("modelProvider"):
                    resume_params["modelProvider"] = thread["modelProvider"]
                server.rpc("thread/resume", resume_params)
                server.events.clear()
                budget = max(1, min(self.runner.timeout_seconds, MAX_CODEX_TIMEOUT_SECONDS))
                deadline = time.monotonic() + budget
                turn_id = None
                try:
                    started = server.rpc("turn/start", {
                        "threadId": thread_id, "cwd": cwd, "model": model,
                        "approvalPolicy": "never", "approvalsReviewer": "user",
                        "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [cwd],
                                          "networkAccess": False, "excludeSlashTmp": True,
                                          "excludeTmpdirEnvVar": True},
                        "input": [{"type": "text", "text": self.runner._prepare_prompt(prompt)}],
                    }, timeout=min(30, budget))
                    turn_id = started["turn"]["id"]
                    return server.wait_turn(thread_id, turn_id, deadline=deadline, progress=progress)
                except CodexHistoryError:
                    if turn_id:
                        try:
                            server.rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)
                        except CodexHistoryError:
                            pass
                    raise CodexHistoryError("恢复执行未能确认完成，已尝试停止本轮。请先查看会话最新记录，避免重复执行。") from None
        finally:
            self.execution_lock.release()
