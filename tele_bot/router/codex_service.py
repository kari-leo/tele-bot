"""Message-level orchestration for explicit Codex commands."""

from __future__ import annotations

from dataclasses import dataclass
import re
from secrets import token_urlsafe
from threading import Lock

from tele_bot.models import IncomingMessage
from tele_bot.router.codex_commands import CodexCommand, parse_codex_command
from tele_bot.tools.codex_runner import CodexResult, CodexRunner


@dataclass(frozen=True)
class PendingCodexRun:
    token: str
    prompt: str
    workspace: str | None


class CodexCommandService:
    """Execute Codex only after an explicit command and matching confirmation."""

    def __init__(self, runner: CodexRunner) -> None:
        self.runner = runner
        self._pending: dict[tuple[str, str], PendingCodexRun] = {}
        self._lock = Lock()

    def handle(self, message: IncomingMessage) -> str | None:
        command = parse_codex_command(message.text)
        if command is None:
            return None
        if command.mode == "apply":
            return self._apply(message.chat_id, command.prompt, command.workspace)
        return self._inspect_or_plan(message.chat_id, command)

    def _inspect_or_plan(self, chat_id: str, command: CodexCommand) -> str:
        try:
            result = self.runner.run(command.mode, command.prompt, command.workspace)
        except (RuntimeError, ValueError) as exc:
            return f"Codex 执行失败：{exc}"
        text = self._format_result(result)
        if command.mode == "plan" and result.returncode == 0:
            token = token_urlsafe(12)
            with self._lock:
                self._pending[(chat_id, token)] = PendingCodexRun(
                    token=token,
                    prompt=command.prompt,
                    workspace=command.workspace,
                )
            text += f"\n\n确认执行请发送：/codex apply {token}"
        return text

    def _apply(self, chat_id: str, token: str, direct_workspace: str | None = None) -> str:
        with self._lock:
            pending = self._pending.pop((chat_id, token), None)
        if pending is not None:
            prompt = pending.prompt
            workspace = pending.workspace
        elif self._looks_like_confirmation_token(token):
            return "Codex 确认令牌无效、已使用或不属于当前会话。请先发送 /codex plan <任务>。"
        else:
            prompt = token
            workspace = direct_workspace
        try:
            result = self.runner.run("apply", prompt, workspace)
        except (RuntimeError, ValueError) as exc:
            return f"Codex 执行失败：{exc}"
        return self._format_result(result)

    @staticmethod
    def _looks_like_confirmation_token(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{16,}", value))

    @staticmethod
    def _format_result(result: CodexResult) -> str:
        parts = [
            (
                f"Codex {result.mode} 完成，目录：{result.workspace}"
                if result.returncode == 0
                else f"Codex {result.mode} 执行失败，目录：{result.workspace}"
            ),
            f"退出码：{result.returncode}",
        ]
        if result.stdout.strip():
            parts.append(f"输出：\n{result.stdout.strip()}")
        if result.returncode != 0 and result.stderr.strip():
            parts.append(f"错误输出：\n{result.stderr.strip()}")
        return "\n".join(parts)