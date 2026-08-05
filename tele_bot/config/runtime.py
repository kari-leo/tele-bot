"""
Runtime configuration — ENV parsing for production wiring.

ENVs:
- TELEGRAM_STREAMING: "1"/"true" (default on) | "0"/"false" — streaming progress
- SQLITE_CHECKPOINT_PATH: path string, default "data/conversations.sqlite" (D1)

Error handling:
- TELEGRAM_STREAMING unrecognized value → treat as off + WARN log.
- SQLITE_CHECKPOINT_PATH empty string → fallback to default.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)

SQLITE_PATH_DEFAULT = "data/conversations.sqlite"


@dataclass(frozen=True)
class RuntimeSettings:
    telegram_streaming: bool
    sqlite_checkpoint_path: str

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
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
            telegram_streaming=telegram_streaming,
            sqlite_checkpoint_path=sqlite_path,
        )
