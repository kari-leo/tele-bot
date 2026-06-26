"""
Phase 6 runtime configuration — ENV parsing for production wiring.

ENVs (per AC v1.0 lockdown):
- EXECUTOR: "react" (default) | "legacy" — main path branch (D6)
- TELEGRAM_STREAMING: "1"/"true" (default on) | "0"/"false" — streaming progress (D4)
- SQLITE_CHECKPOINT_PATH: path string, default "data/conversations.sqlite" (D1)

Error handling:
- EXECUTOR unknown value → RuntimeError with `[FATAL] executor: ...` prefix.
  Caller (app entry) catches and exits 1 per D2 fail-fast pattern.
- TELEGRAM_STREAMING unrecognized value → treat as off + WARN log per D4.
- SQLITE_CHECKPOINT_PATH empty string → fallback to default.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)

VALID_EXECUTORS = ("react", "legacy")
SQLITE_PATH_DEFAULT = "data/conversations.sqlite"


@dataclass(frozen=True)
class RuntimeSettings:
    executor: str
    telegram_streaming: bool
    sqlite_checkpoint_path: str

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        executor_raw = os.environ.get("EXECUTOR", "react").strip().lower()
        if executor_raw not in VALID_EXECUTORS:
            raise RuntimeError(
                f"[FATAL] executor: unknown value '{executor_raw}', expected react|legacy"
            )

        streaming_raw = os.environ.get("TELEGRAM_STREAMING", "1").strip().lower()
        if streaming_raw in ("1", "true"):
            telegram_streaming = True
        elif streaming_raw in ("0", "false"):
            telegram_streaming = False
        else:
            _LOG.warning(
                "TELEGRAM_STREAMING: unrecognized value %r, treating as 0 (off)",
                streaming_raw,
            )
            telegram_streaming = False

        sqlite_path = os.environ.get("SQLITE_CHECKPOINT_PATH", SQLITE_PATH_DEFAULT).strip()
        if not sqlite_path:
            sqlite_path = SQLITE_PATH_DEFAULT

        return cls(
            executor=executor_raw,
            telegram_streaming=telegram_streaming,
            sqlite_checkpoint_path=sqlite_path,
        )
