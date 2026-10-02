"""Configuration for the local knowledge-base MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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

    @classmethod
    def from_env(cls) -> "KnowledgeMCPSettings":
        file_values = _load_feishu_local_env()
        user_id = os.environ.get(
            "KB_MCP_USER_ID", file_values.get("KB_MCP_USER_ID", "")
        ).strip()
        if not user_id:
            raise ValueError(
                "KB_MCP_USER_ID is required; configure the fixed MCP identity in "
                "tele_bot/config/feishu/local.env"
            )
        return cls(user_id=user_id)
