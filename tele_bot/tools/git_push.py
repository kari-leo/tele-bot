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
from threading import Lock, RLock

_REPO_LOCKS: dict[Path, RLock] = {}
_LOCKS_GUARD = Lock()


@dataclass
class _PendingPush:
    token: str
    file_paths: tuple[Path, ...]
    commit_message: str
    remote: str
    branch: str
    created_at: datetime
    committed: bool = False


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
        self._pending_by_chat: dict[str, dict[str, _PendingPush]] = {}
        lock_root = next((root for root in (self.repo_root, *self.repo_root.parents)
                          if (root / ".git").exists()), self.repo_root)
        with _LOCKS_GUARD:
            self._lock = _REPO_LOCKS.setdefault(lock_root, RLock())

    def _new_push(self, file_paths, commit_message, remote, branch) -> _PendingPush:
        self._ensure_inside_repo()
        targets = tuple(dict.fromkeys(self._resolve_repo_path(path) for path in file_paths))
        if not targets or len(targets) > 50:
            raise GitPushError("push requires 1–50 existing files")
        for target in targets:
            if not target.is_file():
                raise GitPushError(f"file not found for push: {target}")
        current_branch = self._current_branch()
        final_branch = (branch or current_branch).strip() or current_branch
        if current_branch == "HEAD" or final_branch != current_branch:
            raise GitPushError("push branch must be the current checked-out branch")
        return _PendingPush(
            token=secrets.token_hex(3).upper(), file_paths=targets,
            commit_message=(commit_message or f"docs: publish {targets[0].stem}").strip(),
            remote=(remote or self.default_remote).strip() or self.default_remote,
            branch=final_branch, created_at=datetime.now(timezone.utc))

    def request_push(
        self,
        *,
        chat_id: str,
        file_path: str,
        commit_message: str | None = None,
        remote: str | None = None,
        branch: str | None = None,
        confirmation_tool: str = "write_report",
    ) -> str:
        with self._lock:
            pending = self._new_push([file_path], commit_message, remote, branch)
            requests = self._pending_by_chat.setdefault(str(chat_id), {})
            now = datetime.now(timezone.utc)
            for token in list(requests):
                if now - requests[token].created_at > self.confirm_ttl:
                    del requests[token]
            requests[pending.token] = pending
        rel = pending.file_paths[0].relative_to(self.repo_root)
        return (
            f"文件已写入：{pending.file_paths[0]}\n"
            "已创建待确认的 git push 请求。\n"
            f"文件：{rel}\n"
            f"远程：{pending.remote}\n"
            f"分支：{pending.branch}\n"
            f"提交信息：{pending.commit_message}\n\n"
            f"如需执行 push，请再次调用 {confirmation_tool} 并传入：\n"
            f"- git_push_enabled=true\n- push_confirm_token={pending.token}"
        )

    def confirm_push(self, *, chat_id: str, confirm_token: str) -> str:
        with self._lock:
            self._ensure_inside_repo()
            requests = self._pending_by_chat.get(str(chat_id), {})
            token = confirm_token.strip().upper()
            pending = requests.get(token)
            if pending is None:
                raise GitPushError("invalid push confirmation token; use blog_push for existing blog files, do not republish")
            if datetime.now(timezone.utc) - pending.created_at > self.confirm_ttl:
                del requests[token]
                raise GitPushError("pending push request expired; request a push of the existing file, do not republish")
            result = self._execute_push(pending)
            del requests[token]
            return result

    def push_files(self, *, chat_id: str, file_paths: list[str],
                   commit_message: str | None = None, remote: str | None = None,
                   branch: str | None = None) -> str:
        """Execute a push after authorization has been checked by the caller."""
        with self._lock:
            pending = self._new_push(file_paths, commit_message, remote, branch)
            return self._execute_push(pending)

    def _execute_push(self, pending: _PendingPush) -> str:
        relatives = [path.relative_to(self.repo_root).as_posix() for path in pending.file_paths]
        try:
            if not pending.committed:
                self._run_git(["add", "--", *relatives])
                changed, _, diff_err = self._run_git(
                    ["diff", "--cached", "--quiet", "--", *relatives], allow_nonzero=True)
                if changed not in (0, 1):
                    raise GitPushError(f"git diff failed: {diff_err.strip()}")
                if changed:
                    code, out, err = self._run_git(
                        ["commit", "-m", pending.commit_message, "--only", "--", *relatives],
                        allow_nonzero=True)
                    if code:
                        raise GitPushError(f"git commit failed: {(err or out).strip()}")
                pending.committed = True
            _, push_out, push_err = self._run_git(["push", pending.remote, pending.branch])
        except GitPushError as exc:
            raise GitPushError(f"{exc}\n本地文件已保留；重试推送已有文件或使用同一确认令牌，不要重新发布或更换 slug。") from exc
        return (
            "git push 已完成。\n"
            f"文件：{', '.join(relatives)}\n"
            f"远程：{pending.remote}\n"
            f"分支：{pending.branch}\n"
            f"提交信息：{pending.commit_message}\n"
            f"输出：{(push_out or push_err).strip() or '(empty)'}"
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
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(self.repo_root), capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False, timeout=90)
        except subprocess.TimeoutExpired as exc:
            raise GitPushError(f"git {args[0]} timed out after 90 seconds") from exc
        if proc.returncode != 0 and not allow_nonzero:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise GitPushError(f"git {' '.join(args)} failed: {msg}")
        return proc.returncode, proc.stdout or "", proc.stderr or ""
