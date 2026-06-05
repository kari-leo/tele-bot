from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default

    return int(value)


def _load_local_env() -> dict[str, str]:
    local_env_path = Path(__file__).with_name("local.env")
    if not local_env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in local_env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    return values


@dataclass(frozen=True)
class AliBailianSettings:
    api_key: str | None
    base_url: str
    model: str
    reasoning_model: str
    timeout_seconds: int

    @classmethod
    def from_env(cls) -> "AliBailianSettings":
        local_values = _load_local_env()

        def resolve(key: str, default: str | None = None) -> str | None:
            if key in os.environ:
                return os.environ[key]
            if key in local_values:
                return local_values[key]
            return default

        return cls(
            api_key=resolve("ALIBAILIAN_API_KEY"),
            base_url=resolve(
                "ALIBAILIAN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
            or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=resolve("ALIBAILIAN_MODEL", "qwen-plus") or "qwen-plus",
            reasoning_model=resolve("ALIBAILIAN_REASONING_MODEL", "qwen3-max") or "qwen3-max",
            timeout_seconds=_parse_int(resolve("ALIBAILIAN_TIMEOUT_SECONDS"), 60),
        )
