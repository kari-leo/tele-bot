"""Configuration for the local knowledge-base MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tele_bot.config.feishu import _parse_user_ids


def _load_feishu_local_env() -> dict[str, str]:
    path = Path(__file__).parent / "feishu" / "local.env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class KnowledgeMCPSettings:
    user_id: str
    admin_user_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "KnowledgeMCPSettings":
        file_values = _load_feishu_local_env()
        admins = tuple(dict.fromkeys(_parse_user_ids(os.environ.get(
            "FEISHU_ADMIN_USER_IDS", file_values.get("FEISHU_ADMIN_USER_IDS", "")))))
        if not admins or "*" in admins:
            raise ValueError("FEISHU_ADMIN_USER_IDS requires explicit administrator IDs")
        user_id = os.environ.get(
            "KB_MCP_USER_ID", file_values.get("KB_MCP_USER_ID", "")
        ).strip() or admins[0]
        if user_id not in admins:
            raise ValueError(
                "KB_MCP_USER_ID must be one of FEISHU_ADMIN_USER_IDS"
            )
        return cls(user_id=user_id, admin_user_ids=admins)
