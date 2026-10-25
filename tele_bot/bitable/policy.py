"""Registered target resolution with all record operations open by default."""
from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit, parse_qs

from tele_bot.bitable.models import BitableError, digest

RECORD_OPERATIONS = ("read", "create", "update", "delete")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise BitableError("validation", "资源 ID 格式无效")
    return value


@dataclass(frozen=True)
class Target:
    alias: str
    app_token: str
    table_id: str
    confirm_all: bool = False
    base_name: str = ""
    table_name: str = ""

    @property
    def key(self):
        return f"{self.app_token}/{self.table_id}"

    @property
    def operations(self):
        return RECORD_OPERATIONS

    def listing(self):
        return {"alias": self.alias, "base_name": self.base_name, "table_name": self.table_name,
                "table_id": self.table_id, "confirm_all": self.confirm_all}


class BitablePolicy:
    def __init__(self, entries, *, origin="https://open.feishu.cn"):
        if origin not in {"https://open.feishu.cn", "https://open.larksuite.com"}:
            raise ValueError("Base API must use an official origin")
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError("Base targets must be a bounded array")
        self.origin, self.targets = origin, []
        aliases = set()
        for row in entries:
            alias = row["alias"]
            if not isinstance(alias, str) or not alias.strip() or len(alias) > 100 or "://" in alias:
                raise ValueError("Invalid Base alias")
            target = Target(alias=alias, app_token=identifier(row["app_token"]),
                            table_id=identifier(row["table_id"]),
                            confirm_all=row.get("confirm_all", False),
                            base_name=self._name(row.get("base_name", alias)),
                            table_name=self._name(row.get("table_name", alias)))
            if target.alias in aliases or not isinstance(target.confirm_all, bool):
                raise ValueError("Invalid/duplicate Base target policy")
            aliases.add(target.alias)
            self.targets.append(target)
        # Ignore legacy users/chats/operations fields, including in the configuration version.
        self.version = digest(["open-records-v1", origin,
                               [t.listing() | {"app_token": t.app_token} for t in self.targets]])

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 100 or "://" in value:
            raise ValueError("Invalid Base display name")
        return value

    def visible_targets(self, message):
        if message is None or message.channel != "feishu":
            raise BitableError("unauthorized", "多维表格需要已认证的飞书消息入口")
        return list(self.targets)

    @classmethod
    def load(cls, path, **kwargs):
        if path.stat().st_size > 512000:
            raise ValueError("Base target file too large")
        return cls(json.loads(path.read_text(encoding="utf-8")), **kwargs)

    def resolve(self, value, message, operation):
        visible = self.visible_targets(message)
        if operation not in RECORD_OPERATIONS:
            raise BitableError("validation", "仅支持记录查询、新增、修改、删除")
        # Explicit unique aliases take precedence over non-unique display names.
        candidates = [t for t in visible if value == t.alias]
        if not candidates:
            candidates = [t for t in visible if value in {t.base_name, t.table_name}]
        if value.startswith("https://"):
            app_token, table = self.parse_url(value)
            candidates = [t for t in visible if t.app_token == app_token and (table is None or table == t.table_id)]
        if not candidates:
            raise BitableError("not_found", "未找到对应的已知目标；请将 Base 直链传给 bitable_inspect 自动发现，"
                               "或从已发现清单选择唯一别名，无需后台登记")
        if len(candidates) != 1:
            raise BitableError("validation", "名称匹配多个已发现数据表，请从清单选择唯一目标别名")
        return candidates[0]

    def parse_url(self, value):
        url = urlsplit(value)
        suffix = ".feishu.cn" if self.origin.endswith("feishu.cn") else ".larksuite.com"
        match = re.fullmatch(r"/base/([A-Za-z0-9_-]+)", url.path.rstrip("/"))
        if url.scheme != "https" or not url.hostname or not url.hostname.endswith(suffix) or url.username or url.port or not match:
            raise BitableError("validation", "请提供飞书 Base 直链；Wiki 请先提供对应 Base 直链")
        table = parse_qs(url.query).get("table", [None])[0]
        return identifier(match[1]), identifier(table) if table is not None else None
