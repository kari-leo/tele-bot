"""Controlled Codex CLI process runner."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tele_bot.tools.workspace_policy import WorkspacePolicy

_DEFAULT_CODEX_WORKSPACE = Path("D:/files_data")
_DEFAULT_CODEX_MODEL = "gpt-6.1-sol"
MAX_CODEX_TIMEOUT_SECONDS = 20 * 60


def _local_env_values() -> dict[str, str]:
    """Read the ignored per-machine LLM environment file as a fallback."""
    path = Path(__file__).resolve().parents[1] / "config" / "llm" / "local.env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CodexResult:
    mode: str
    workspace: str
    returncode: int
    stdout: str
    stderr: str
    truncated: bool


def codex_failure_message(result: CodexResult) -> str:
    """Return the actionable error instead of forwarding noisy CLI retries."""
    details = result.stderr or result.stdout or "Codex 未返回错误信息。"
    unsupported = re.search(
        r"The '([^']+)' model is not supported when using Codex with a ChatGPT account",
        details,
    )
    if unsupported:
        return (f"当前 Codex CLI 登录会话无法使用模型 {unsupported.group(1)}。"
                "请检查 CLI 版本、登录账号和模型可用性，或调整 TELE_BOT_CODEX_MODEL 后重试。")
    if "request timed out" in details.lower() or "connection timed out" in details.lower():
        return "连接 Codex 服务超时。请检查网络后重试。"
    return details[-2000:]


@dataclass(frozen=True)
class CodexRunner:
    policy: WorkspacePolicy
    default_workspace: Path | None = None
    command: str = "codex"
    model: str = _DEFAULT_CODEX_MODEL
    timeout_seconds: int = MAX_CODEX_TIMEOUT_SECONDS
    skip_git_repo_check: bool = False
    max_output_chars: int = 20_000

    @classmethod
    def from_env(cls, policy: WorkspacePolicy | None = None) -> "CodexRunner":
        local_values = _local_env_values()
        command = os.environ.get("TELE_BOT_CODEX_COMMAND") or local_values.get(
            "TELE_BOT_CODEX_COMMAND", "codex"
        )
        timeout = os.environ.get("TELE_BOT_CODEX_TIMEOUT") or local_values.get(
            "TELE_BOT_CODEX_TIMEOUT", str(MAX_CODEX_TIMEOUT_SECONDS)
        )
        model = os.environ.get("TELE_BOT_CODEX_MODEL") or local_values.get(
            "TELE_BOT_CODEX_MODEL", _DEFAULT_CODEX_MODEL
        )
        skip_git_repo_check = os.environ.get("TELE_BOT_CODEX_SKIP_GIT_REPO_CHECK")
        if skip_git_repo_check is None:
            skip_git_repo_check = local_values.get("TELE_BOT_CODEX_SKIP_GIT_REPO_CHECK")
        if policy is None:
            policy = WorkspacePolicy.from_env()
            configured_root = os.environ.get("TELE_BOT_CODEX_WORKSPACE_ROOT") or local_values.get(
                "TELE_BOT_CODEX_WORKSPACE_ROOT", ""
            )
            if configured_root.strip():
                policy = WorkspacePolicy(
                    allowed_roots=policy.allowed_roots,
                    workspace_root=Path(configured_root).expanduser(),
                    quarantine_root=policy.quarantine_root,
                    max_file_bytes=policy.max_file_bytes,
                    max_content_chars=policy.max_content_chars,
                    max_changed_files=policy.max_changed_files,
                )
        configured_root = os.environ.get("TELE_BOT_CODEX_WORKSPACE_ROOT") or local_values.get(
            "TELE_BOT_CODEX_WORKSPACE_ROOT", ""
        )
        default_workspace = Path(configured_root).expanduser() if configured_root.strip() else _DEFAULT_CODEX_WORKSPACE
        return cls(
            policy=policy,
            default_workspace=default_workspace,
            command=command.strip() or "codex",
            model=model.strip() or _DEFAULT_CODEX_MODEL,
            timeout_seconds=max(1, min(int(timeout), MAX_CODEX_TIMEOUT_SECONDS)),
            skip_git_repo_check=_parse_bool(skip_git_repo_check, default=True),
        )

    def run(self, mode: str, prompt: str, workspace: str | None = None) -> CodexResult:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"inspect", "plan", "apply"}:
            raise ValueError("Codex mode must be inspect, plan, or apply")
        if not prompt.strip():
            raise ValueError("Codex prompt is required")

        target = (self.default_workspace or self.policy.workspace_root).expanduser().resolve()
        if workspace:
            target = self.policy.resolve_existing(workspace)
        if target is None or not target.is_dir():
            raise ValueError("Codex workspace must be an existing directory")
        if not self._workspace_allowed(target):
            raise ValueError("Codex workspace must be inside the configured workspace root")

        command = self._command_args(normalized_mode, prompt)
        environment = self._safe_environment()
        timeout_seconds = max(1, min(self.timeout_seconds, MAX_CODEX_TIMEOUT_SECONDS))
        try:
            completed = subprocess.run(
                command,
                cwd=str(target),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex timed out after {timeout_seconds}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"Codex executable not found: {self.command}") from exc

        stdout, stdout_truncated = self._truncate(completed.stdout or "")
        stderr, stderr_truncated = self._truncate(completed.stderr or "")
        return CodexResult(
            mode=normalized_mode,
            workspace=str(target),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
        )

    def _command_args(self, mode: str, prompt: str) -> list[str]:
        executable = shlex.split(self.command, posix=False)
        if not executable:
            raise ValueError("TELE_BOT_CODEX_COMMAND is empty")
        # Codex is always invoked in non-interactive exec mode. The application
        # decides whether a mode is authorized before this process is started.
        args = [*executable, "exec", "--model", self.model]
        if mode in {"inspect", "plan"}:
            args.extend(["--sandbox", "read-only"])
        else:
            args.extend(["--sandbox", "workspace-write"])
        if self.skip_git_repo_check:
            args.append("--skip-git-repo-check")
        args.append(self._prepare_prompt(prompt))
        return args

    @staticmethod
    def _prepare_prompt(prompt: str) -> str:
        if os.name != "nt":
            return prompt
        return (
            f"{prompt}\n\n"
            "Windows 编码要求：执行 PowerShell 前先设置 "
            "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)；"
            "读取 UTF-8 文本文件时，Get-Content 必须显式使用 -Encoding UTF8，避免中文乱码。"
        )

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        blocked = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(part in key.upper() for part in blocked)
            and key not in {"TELE_BOT_LLM_API_KEY", "ALIBAILIAN_API_KEY"}
        }
        if os.name == "nt":
            environment.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                }
            )
        return environment

    @staticmethod
    def _is_within(target: Path, root: Path) -> bool:
        try:
            target.relative_to(root)
        except ValueError:
            return False
        return True

    def _workspace_allowed(self, target: Path) -> bool:
        if self._is_within(target, self.policy.workspace_root):
            return True
        # A configured drive root such as D:\ is an explicit opt-in for
        # selecting sibling workspaces on Windows.
        for root in self.policy.allowed_roots:
            if root.anchor and root == Path(root.anchor).resolve() and self._is_within(target, root):
                return True
        return False

    def _truncate(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.max_output_chars:
            return value, False
        return value[: self.max_output_chars], True
