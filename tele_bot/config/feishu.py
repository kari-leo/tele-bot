"""Feishu configuration loaded from environment or tele_bot/config/feishu/local.env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_user_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass(frozen=True)
class FeishuSettings:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str
    allowed_user_ids: list[str]
    api_base_url: str
    webhook_host: str
    webhook_port: int
    webhook_path: str
    kb_creator_user_ids: tuple[str, ...] = ()

    @classmethod
    def _load_env_file(cls, env_file: Path) -> dict[str, str]:
        env_vars: dict[str, str] = {}
        if not env_file.exists():
            return env_vars

        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env_vars[key.strip()] = value.strip()
        return env_vars

    @classmethod
    def from_env(cls) -> "FeishuSettings":
        config_file = Path(__file__).parent / "feishu" / "local.env"
        file_vars = cls._load_env_file(config_file)

        def get_config(key: str, default: str = "") -> str:
            return os.environ.get(key, file_vars.get(key, default)).strip()

        app_id = get_config("FEISHU_APP_ID")
        app_secret = get_config("FEISHU_APP_SECRET")

        if not app_id or not app_secret:
            raise ValueError(
                "FEISHU_APP_ID and FEISHU_APP_SECRET are required for Feishu. "
                f"Set them in environment variables or {config_file}"
            )

        webhook_path = get_config("FEISHU_WEBHOOK_PATH", "/feishu/webhook")
        if not webhook_path.startswith("/"):
            webhook_path = f"/{webhook_path}"

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=get_config("FEISHU_VERIFICATION_TOKEN"),
            encrypt_key=get_config("FEISHU_ENCRYPT_KEY"),
            allowed_user_ids=_parse_user_ids(get_config("FEISHU_ALLOWED_USER_IDS")),
            api_base_url=get_config("FEISHU_API_BASE_URL", "https://open.feishu.cn"),
            webhook_host=get_config("FEISHU_WEBHOOK_HOST", "127.0.0.1"),
            webhook_port=_parse_int(get_config("FEISHU_WEBHOOK_PORT"), 3000),
            webhook_path=webhook_path,
            kb_creator_user_ids=tuple(
                _parse_user_ids(get_config("KB_CREATOR_USER_IDS"))
            ),
        )
