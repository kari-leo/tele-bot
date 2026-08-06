from __future__ import annotations

import os
import sys
from pathlib import Path

_RUNTIME_ROOT_ENV = "TELE_BOT_RUNTIME_ROOT"


def configure_runtime_root(script_file: str | Path) -> Path:
    """Record the directory that should anchor relative runtime paths."""
    root = Path(script_file).expanduser().resolve()
    if root.is_file():
        root = root.parent
    os.environ.setdefault(_RUNTIME_ROOT_ENV, str(root))
    return root


def runtime_root() -> Path:
    """Return the bot runtime root.

    The launcher sets this explicitly. If code is imported directly, prefer the
    current Python script directory, then fall back to the repository root.
    """
    configured = os.environ.get(_RUNTIME_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and argv0 not in ("-c", "-m"):
        script = Path(argv0).expanduser()
        try:
            resolved = script.resolve()
        except OSError:
            resolved = script.absolute()
        if resolved.exists() and resolved.is_file():
            return resolved.parent

    return Path(__file__).resolve().parents[2]


def default_allowed_roots() -> tuple[Path, ...]:
    """Return filesystem/shell roots for the current platform."""
    root = runtime_root()
    if os.name == "nt":
        drive = root.drive or "D:"
        return (Path(f"{drive}\\"),)
    return (root, Path("/tmp"))


def is_windows_runtime() -> bool:
    return os.name == "nt"
