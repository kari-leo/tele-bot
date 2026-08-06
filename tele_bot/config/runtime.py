"""
Runtime configuration — ENV parsing for production wiring.

ENVs:
- SQLITE_CHECKPOINT_PATH: path string, default "data/conversations.sqlite" (D1)

Error handling:
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
    sqlite_checkpoint_path: str

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        sqlite_path = os.environ.get("SQLITE_CHECKPOINT_PATH", SQLITE_PATH_DEFAULT).strip()
        if not sqlite_path:
            sqlite_path = SQLITE_PATH_DEFAULT

        return cls(
            sqlite_checkpoint_path=sqlite_path,
        )
