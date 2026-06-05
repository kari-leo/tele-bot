from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_user_ids(value: str | None) -> set[str]:
    if not value:
        return set()

    return {item.strip() for item in value.split(",") if item.strip()}


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
class TelegramSettings:
    bot_token: str | None
    secret_token: str | None
    allowed_user_ids: set[str]
    api_base_url: str
    proxy_url: str | None
    polling_timeout: int
    polling_interval_seconds: int

    @classmethod
    def from_env(cls) -> "TelegramSettings":
        local_values = _load_local_env()

        def resolve(key: str, default: str | None = None) -> str | None:
            if key in os.environ:
                return os.environ[key]
            if key in local_values:
                return local_values[key]
            return default

        return cls(
            bot_token=resolve("TELEGRAM_BOT_TOKEN"),
            secret_token=resolve("TELEGRAM_SECRET_TOKEN"),
            allowed_user_ids=_parse_user_ids(resolve("TELEGRAM_ALLOWED_USER_IDS")),
            api_base_url=resolve("TELEGRAM_API_BASE_URL", "https://api.telegram.org")
            or "https://api.telegram.org",
            proxy_url=resolve("TELEGRAM_PROXY_URL"),
            polling_timeout=_parse_int(resolve("TELEGRAM_POLLING_TIMEOUT"), 30),
            polling_interval_seconds=_parse_int(
                resolve("TELEGRAM_POLLING_INTERVAL_SECONDS"),
                2,
            ),
        )
