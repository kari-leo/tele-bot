from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default

    return int(value)


def _parse_float(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    return float(value)


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
    embedding_api_key: str | None = None
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    embedding_timeout_seconds: int = 30
    embedding_batch_size: int = 10
    embedding_daily_token_budget: int = 1_000_000
    embedding_daily_cost_budget_cny: float = 1.0

    def __post_init__(self) -> None:
        positive_values = {
            "ALIBAILIAN_TIMEOUT_SECONDS": self.timeout_seconds,
            "ALIBAILIAN_EMBEDDING_DIMENSIONS": self.embedding_dimensions,
            "ALIBAILIAN_EMBEDDING_TIMEOUT_SECONDS": self.embedding_timeout_seconds,
            "ALIBAILIAN_EMBEDDING_BATCH_SIZE": self.embedding_batch_size,
            "ALIBAILIAN_EMBEDDING_DAILY_TOKEN_BUDGET": self.embedding_daily_token_budget,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.embedding_daily_cost_budget_cny <= 0:
            raise ValueError(
                "ALIBAILIAN_EMBEDDING_DAILY_COST_BUDGET_CNY must be greater than zero"
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("ALIBAILIAN_BASE_URL must be an HTTP(S) URL")
        if not self.embedding_base_url.startswith(("http://", "https://")):
            raise ValueError("ALIBAILIAN_EMBEDDING_BASE_URL must be an HTTP(S) URL")

    def require_embedding_credentials(self) -> str:
        """Return the embedding key or fail when KB embedding is actually enabled."""
        if not self.embedding_api_key:
            raise RuntimeError(
                "ALIBAILIAN_EMBEDDING_API_KEY is required when knowledge-base "
                "embedding is enabled"
            )
        return self.embedding_api_key

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
            embedding_api_key=resolve("ALIBAILIAN_EMBEDDING_API_KEY"),
            embedding_base_url=resolve(
                "ALIBAILIAN_EMBEDDING_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
            or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            embedding_model=resolve(
                "ALIBAILIAN_EMBEDDING_MODEL", "text-embedding-v4"
            )
            or "text-embedding-v4",
            embedding_dimensions=_parse_int(
                resolve("ALIBAILIAN_EMBEDDING_DIMENSIONS"), 1024
            ),
            embedding_timeout_seconds=_parse_int(
                resolve("ALIBAILIAN_EMBEDDING_TIMEOUT_SECONDS"), 30
            ),
            embedding_batch_size=_parse_int(
                resolve("ALIBAILIAN_EMBEDDING_BATCH_SIZE"), 10
            ),
            embedding_daily_token_budget=_parse_int(
                resolve("ALIBAILIAN_EMBEDDING_DAILY_TOKEN_BUDGET"), 1_000_000
            ),
            embedding_daily_cost_budget_cny=_parse_float(
                resolve("ALIBAILIAN_EMBEDDING_DAILY_COST_BUDGET_CNY"), 1.0
            ),
        )
