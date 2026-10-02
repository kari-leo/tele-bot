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
from pathlib import Path

_LOG = logging.getLogger(__name__)

SQLITE_PATH_DEFAULT = "data/conversations.sqlite"
KB_SQLITE_PATH_DEFAULT = "data/knowledge.sqlite"


@dataclass(frozen=True)
class RuntimeSettings:
    sqlite_checkpoint_path: str
    kb_sqlite_path: str = KB_SQLITE_PATH_DEFAULT
    kb_max_chunks_per_space: int = 10_000
    kb_max_import_bytes: int = 30 * 1024 * 1024
    kb_index_worker_count: int = 1

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        local_values: dict[str, str] = {}
        local_env_path = Path(__file__).parent / "llm" / "local.env"
        if local_env_path.exists():
            for raw_line in local_env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                local_values[key.strip()] = value.strip()

        def resolve(name: str, default: str = "") -> str:
            return os.environ.get(name, local_values.get(name, default)).strip()

        sqlite_path = resolve("SQLITE_CHECKPOINT_PATH", SQLITE_PATH_DEFAULT)
        if not sqlite_path:
            sqlite_path = SQLITE_PATH_DEFAULT

        kb_sqlite_path = resolve("KB_SQLITE_PATH", KB_SQLITE_PATH_DEFAULT)
        if not kb_sqlite_path:
            kb_sqlite_path = KB_SQLITE_PATH_DEFAULT

        def positive_int(name: str, default: int) -> int:
            raw_value = resolve(name)
            value = int(raw_value) if raw_value else default
            if value <= 0:
                raise RuntimeError(f"{name} must be greater than zero")
            return value

        return cls(
            sqlite_checkpoint_path=sqlite_path,
            kb_sqlite_path=kb_sqlite_path,
            kb_max_chunks_per_space=positive_int(
                "KB_MAX_CHUNKS_PER_SPACE", 10_000
            ),
            kb_max_import_bytes=positive_int(
                "KB_MAX_IMPORT_BYTES", 30 * 1024 * 1024
            ),
            kb_index_worker_count=positive_int("KB_INDEX_WORKER_COUNT", 1),
        )
