"""Cross-platform policy for Codex and destructive workspace operations."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from tele_bot.config.paths import is_windows_runtime, runtime_root

_ALLOWED_ROOTS_ENV = "TELE_BOT_ALLOWED_ROOTS"
_WORKSPACE_ROOT_ENV = "TELE_BOT_WORKSPACE_ROOT"
_QUARANTINE_ROOT_ENV = "TELE_BOT_QUARANTINE_ROOT"

_DEFAULT_WINDOWS_ALLOWED_ROOT = Path("D:/")
_DEFAULT_UBUNTU_ALLOWED_ROOT = Path("/srv/telebot/workspace")
_SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
}
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")


def _default_quarantine_root() -> Path:
    root = runtime_root()
    return root.parent / f"{root.name}-quarantine"


@dataclass(frozen=True)
class WorkspacePolicy:
    """Resolve and validate paths used by file mutation and Codex tools."""

    allowed_roots: tuple[Path, ...] = field(default_factory=tuple)
    workspace_root: Path | None = None
    quarantine_root: Path | None = None
    max_file_bytes: int = 20 * 1024 * 1024
    max_content_chars: int = 500_000
    max_changed_files: int = 20

    @classmethod
    def from_env(cls) -> "WorkspacePolicy":
        allowed_value = os.environ.get(_ALLOWED_ROOTS_ENV, "").strip()
        if allowed_value:
            allowed_roots = tuple(
                Path(item.strip()).expanduser()
                for item in allowed_value.split(os.pathsep)
                if item.strip()
            )
        else:
            allowed_roots = (
                (_DEFAULT_WINDOWS_ALLOWED_ROOT,)
                if is_windows_runtime()
                else (runtime_root(), _DEFAULT_UBUNTU_ALLOWED_ROOT)
            )

        configured_workspace = os.environ.get(_WORKSPACE_ROOT_ENV, "").strip()
        workspace_root = (
            Path(configured_workspace).expanduser()
            if configured_workspace
            else runtime_root()
        )

        configured_quarantine = os.environ.get(_QUARANTINE_ROOT_ENV, "").strip()
        quarantine_root = (
            Path(configured_quarantine).expanduser()
            if configured_quarantine
            else _default_quarantine_root()
        )
        if not allowed_value and not is_windows_runtime():
            allowed_roots = (*allowed_roots, quarantine_root)
        return cls(
            allowed_roots=allowed_roots,
            workspace_root=workspace_root,
            quarantine_root=quarantine_root,
        )

    def __post_init__(self) -> None:
        roots = tuple(root.expanduser().resolve() for root in self.allowed_roots)
        if not roots:
            raise ValueError("at least one allowed root is required")
        object.__setattr__(self, "allowed_roots", roots)

        workspace = (self.workspace_root or runtime_root()).expanduser().resolve()
        quarantine = (self.quarantine_root or self._default_quarantine()).expanduser().resolve()
        self._assert_allowed(workspace, "workspace root", roots)
        if self._is_relative_to(quarantine, workspace) or self._is_relative_to(workspace, quarantine):
            raise ValueError("workspace and quarantine roots must not overlap")
        if not self._is_allowed(quarantine, roots):
            raise ValueError("quarantine root must be inside an allowed root")
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "quarantine_root", quarantine)

    def resolve_existing(self, raw_path: str) -> Path:
        target = self._resolve(raw_path)
        if not target.exists():
            raise ValueError(f"path does not exist: {target}")
        self._validate_sensitive(target)
        return target

    def resolve_target(self, raw_path: str) -> Path:
        target = self._resolve(raw_path)
        self._validate_sensitive(target)
        return target

    def validate_content(self, content: str) -> None:
        if not content:
            raise ValueError("content is required")
        if len(content.encode("utf-8")) > self.max_file_bytes:
            raise ValueError(f"content exceeds {self.max_file_bytes} bytes")
        if len(content) > self.max_content_chars:
            raise ValueError(f"content exceeds {self.max_content_chars} characters")

    def _resolve(self, raw_path: str) -> Path:
        normalized = raw_path.strip()
        if not normalized:
            raise ValueError("path is required")
        candidate = Path(normalized).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        target = candidate.resolve(strict=False)
        self._assert_allowed(target, "path", self.allowed_roots)
        return target

    def _validate_sensitive(self, target: Path) -> None:
        if target.name.lower() in {name.lower() for name in _SENSITIVE_NAMES}:
            raise ValueError(f"sensitive path is not allowed: {target.name}")
        if target.suffix.lower() in _SENSITIVE_SUFFIXES:
            raise ValueError(f"sensitive file type is not allowed: {target.suffix}")

    @staticmethod
    def _assert_allowed(target: Path, label: str, roots: tuple[Path, ...]) -> None:
        if not any(WorkspacePolicy._is_relative_to(target, root) for root in roots):
            allowed = ", ".join(str(root) for root in roots)
            raise ValueError(f"{label} is outside allowed roots ({allowed}): {target}")

    @staticmethod
    def _is_allowed(target: Path, roots: tuple[Path, ...]) -> bool:
        return any(WorkspacePolicy._is_relative_to(target, root) for root in roots)

    @staticmethod
    def _is_relative_to(target: Path, root: Path) -> bool:
        try:
            target.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _default_quarantine() -> Path:
        return _default_quarantine_root()
