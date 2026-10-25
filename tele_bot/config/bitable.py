"""Base configuration, without SDK import or authentication on startup."""
from dataclasses import dataclass, field
from pathlib import Path
import os

from tele_bot.config.feishu import FeishuSettings
from tele_bot.config.paths import runtime_root


@dataclass(frozen=True)
class BitableSettings:
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    api_base_url: str = "https://open.feishu.cn"
    targets_file: Path = Path("tele_bot/config/feishu/bitable_targets.json")
    sqlite_path: Path = Path("data/bitable.sqlite")
    timeout: int = 30
    page_size: int = 50
    max_query_records: int = 500
    max_write_records: int = 100
    confirm_ttl: int = 600
    max_output_chars: int = 24000
    max_response_bytes: int = 2 * 1024 * 1024
    max_requests: int = 30
    operation_timeout: int = 120

    @classmethod
    def from_env(cls):
        values = FeishuSettings._load_env_file(Path(__file__).parent / "feishu/local.env")

        def get(key, default=""):
            return os.environ.get(key, values.get(key, default)).strip()

        app_id, secret = get("FEISHU_BITABLE_APP_ID"), get("FEISHU_BITABLE_APP_SECRET")
        if bool(app_id) != bool(secret):
            raise ValueError("Set both Base app credentials or neither")

        def path(key, default):
            result = Path(get(key, default)).expanduser()
            return result if result.is_absolute() else runtime_root() / result

        numbers = {}
        for name, key, default, upper in (
            ("timeout", "TIMEOUT_SECONDS", 30, 60), ("page_size", "QUERY_PAGE_SIZE", 50, 100),
            ("max_query_records", "MAX_QUERY_RECORDS", 500, 5000),
            ("max_write_records", "MAX_WRITE_RECORDS", 100, 100),
            ("confirm_ttl", "CONFIRM_TTL_SECONDS", 600, 3600),
        ):
            number = int(get("FEISHU_BITABLE_" + key, str(default)))
            if not 1 <= number <= upper:
                raise ValueError(f"Invalid Base configuration: {key}")
            numbers[name] = number
        return cls(app_id=app_id or get("FEISHU_APP_ID"),
                   app_secret=secret or get("FEISHU_APP_SECRET"),
                   api_base_url=get("FEISHU_API_BASE_URL", "https://open.feishu.cn"),
                   targets_file=path("FEISHU_BITABLE_TARGETS_FILE", "tele_bot/config/feishu/bitable_targets.json"),
                   sqlite_path=path("FEISHU_BITABLE_SQLITE_PATH", "data/bitable.sqlite"), **numbers)
