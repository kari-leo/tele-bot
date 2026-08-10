"""Confirmed file overwrite and quarantine-delete operations."""

from __future__ import annotations

import hashlib
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tele_bot.tools.workspace_policy import WorkspacePolicy


@dataclass(frozen=True)
class PendingMutation:
    token: str
    operation: str
    target: Path
    original_sha256: str
    content: str | None
    created_at: datetime


class FileMutationService:
    """Prepare and execute narrowly scoped, user-confirmed file mutations."""

    def __init__(self, policy: WorkspacePolicy | None = None) -> None:
        self.policy = policy or WorkspacePolicy.from_env()
        self._pending: dict[str, PendingMutation] = {}

    def prepare_overwrite(self, path: str, content: str) -> dict[str, str | int]:
        target = self.policy.resolve_existing(path)
        if not target.is_file():
            raise ValueError(f"overwrite target is not a file: {target}")
        self.policy.validate_content(content)
        original_sha256 = self._sha256(target)
        token = secrets.token_urlsafe(12)
        self._pending[token] = PendingMutation(
            token=token,
            operation="overwrite",
            target=target,
            original_sha256=original_sha256,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        return {
            "operation": "overwrite",
            "path": str(target),
            "original_sha256": original_sha256,
            "new_sha256": self._sha256_text(content),
            "content_bytes": len(content.encode("utf-8")),
            "confirm_token": token,
        }

    def confirm_overwrite(self, token: str) -> dict[str, str]:
        pending = self._consume(token, "overwrite")
        assert pending.content is not None
        if self._sha256(pending.target) != pending.original_sha256:
            raise ValueError("target changed after prepare; create a new plan")

        backup = pending.target.with_name(
            f"{pending.target.name}.backup-{pending.token}"
        )
        if backup.exists():
            raise ValueError(f"backup already exists: {backup}")
        shutil.copy2(pending.target, backup)
        temporary = pending.target.with_name(f".{pending.target.name}.{pending.token}.tmp")
        try:
            temporary.write_text(pending.content, encoding="utf-8", newline="")
            if self._sha256(temporary) != self._sha256_text(pending.content):
                raise ValueError("temporary file verification failed")
            temporary.replace(pending.target)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "operation": "overwrite",
            "path": str(pending.target),
            "backup": str(backup),
            "sha256": self._sha256(pending.target),
        }

    def prepare_delete(self, path: str) -> dict[str, str | int]:
        target = self.policy.resolve_existing(path)
        if not target.is_file():
            raise ValueError("only individual files can be deleted")
        original_sha256 = self._sha256(target)
        token = secrets.token_urlsafe(12)
        self._pending[token] = PendingMutation(
            token=token,
            operation="delete",
            target=target,
            original_sha256=original_sha256,
            content=None,
            created_at=datetime.now(timezone.utc),
        )
        return {
            "operation": "delete",
            "path": str(target),
            "original_sha256": original_sha256,
            "size_bytes": target.stat().st_size,
            "confirm_token": token,
        }

    def confirm_delete(self, token: str) -> dict[str, str]:
        pending = self._consume(token, "delete")
        if self._sha256(pending.target) != pending.original_sha256:
            raise ValueError("target changed after prepare; create a new plan")
        quarantine_root = self.policy.quarantine_root
        assert quarantine_root is not None
        relative = pending.target.relative_to(self.policy.workspace_root)
        quarantine_path = quarantine_root / pending.token / relative
        quarantine_path.parent.mkdir(parents=True, exist_ok=False)
        shutil.move(str(pending.target), str(quarantine_path))
        return {
            "operation": "delete",
            "path": str(pending.target),
            "quarantine_path": str(quarantine_path),
            "sha256": pending.original_sha256,
        }

    def _consume(self, token: str, operation: str) -> PendingMutation:
        normalized = token.strip()
        pending = self._pending.pop(normalized, None)
        if pending is None or pending.operation != operation:
            raise ValueError("invalid or already used confirmation token")
        return pending

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _sha256_text(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
