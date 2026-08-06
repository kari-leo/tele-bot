"""
GitPushTool - guarded git add/commit/push with chat-scoped confirmation.

Safety model:
- first call creates a pending push request and returns a confirmation token
- push runs only after a second explicit confirmation call with that token
- pending requests are chat-scoped and expire automatically
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import subprocess


@dataclass
class _PendingPush:
    token: str
    file_path: Path
    commit_message: str
    remote: str
    branch: str
    created_at: datetime


class GitPushError(RuntimeError):
    pass


class GitPushTool:
    def __init__(
        self,
        *,
        repo_root: Path,
        default_remote: str = "origin",
        confirm_ttl_seconds: int = 900,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.default_remote = default_remote
        self.confirm_ttl = timedelta(seconds=confirm_ttl_seconds)
        self._pending_by_chat: dict[str, _PendingPush] = {}

    def request_push(
        self,
        *,
        chat_id: str,
        file_path: str,
        commit_message: str | None = None,
        remote: str | None = None,
        branch: str | None = None,
    ) -> str:
        self._ensure_inside_repo()
        target = self._resolve_repo_path(file_path)
        if not target.exists() or not target.is_file():
            raise GitPushError(f"file not found for push: {target}")

        final_remote = (remote or self.default_remote).strip() or self.default_remote
        final_branch = (branch or self._current_branch()).strip() or self._current_branch()
        final_message = (commit_message or f"docs: add note {target.stem}").strip()

        token = secrets.token_hex(3).upper()
        self._pending_by_chat[str(chat_id)] = _PendingPush(
            token=token,
            file_path=target,
            commit_message=final_message,
            remote=final_remote,
            branch=final_branch,
            created_at=datetime.now(timezone.utc),
        )

        rel = target.relative_to(self.repo_root)
        return (
            f"报告已写入：{target}\n"
            "已创建待确认的 git push 请求。\n"
            f"文件：{rel}\n"
            f"远程：{final_remote}\n"
            f"分支：{final_branch}\n"
            f"提交信息：{final_message}\n\n"
            "如需执行 push，请再次调用 write_report 并传入：\n"
            f"- git_push_enabled=true\n- push_confirm_token={token}"
        )

    def confirm_push(self, *, chat_id: str, confirm_token: str) -> str:
        self._ensure_inside_repo()
        pending = self._pending_by_chat.get(str(chat_id))
        if pending is None:
            raise GitPushError("no pending push request for this chat")

        now = datetime.now(timezone.utc)
        if now - pending.created_at > self.confirm_ttl:
            self._pending_by_chat.pop(str(chat_id), None)
            raise GitPushError("pending push request expired; please request again")

        if confirm_token.strip().upper() != pending.token:
            raise GitPushError("invalid push confirmation token")

        rel = str(pending.file_path.relative_to(self.repo_root)).replace("\\", "/")
        self._run_git(["add", "--", rel])

        commit_code, _, commit_err = self._run_git(
            ["commit", "-m", pending.commit_message],
            allow_nonzero=True,
        )
        if commit_code != 0:
            err_lower = (commit_err or "").lower()
            if "nothing to commit" not in err_lower and "no changes added" not in err_lower:
                raise GitPushError(f"git commit failed: {commit_err.strip()}")

        _, push_out, _ = self._run_git(
            ["push", pending.remote, pending.branch],
            allow_nonzero=False,
        )
        self._pending_by_chat.pop(str(chat_id), None)

        return (
            "git push 已完成。\n"
            f"文件：{rel}\n"
            f"远程：{pending.remote}\n"
            f"分支：{pending.branch}\n"
            f"提交信息：{pending.commit_message}\n"
            f"输出：{push_out.strip() or '(empty)'}"
        )

    def _ensure_inside_repo(self) -> None:
        code, out, err = self._run_git(["rev-parse", "--is-inside-work-tree"], allow_nonzero=True)
        if code != 0 or out.strip().lower() != "true":
            raise GitPushError(f"not a git repository: {self.repo_root} ({err.strip()})")

    def _current_branch(self) -> str:
        _, out, _ = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        branch = out.strip()
        if not branch:
            raise GitPushError("failed to resolve current git branch")
        return branch

    def _resolve_repo_path(self, raw_path: str) -> Path:
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = (self.repo_root / p)
        resolved = p.resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise GitPushError(f"path is outside repository: {raw_path}") from exc
        return resolved

    def _run_git(self, args: list[str], allow_nonzero: bool = False) -> tuple[int, str, str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0 and not allow_nonzero:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise GitPushError(f"git {' '.join(args)} failed: {msg}")
        return proc.returncode, proc.stdout or "", proc.stderr or ""
