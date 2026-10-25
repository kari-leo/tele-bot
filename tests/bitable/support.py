from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tele_bot.bitable.models import BitableError
from tele_bot.bitable.policy import BitablePolicy
from tele_bot.bitable.service import BitableService
from tele_bot.config.bitable import BitableSettings
from tele_bot.models import IncomingMessage
from tele_bot.persistence.bitable_store import BitableStore

FIELDS = [
    {"field_id": "fld1", "field_name": "名称", "type": 1},
    {"field_id": "fld2", "field_name": "数量", "type": 2},
    {"field_id": "fld3", "field_name": "状态", "type": 3, "property": {"options": [{"name": "进行中"}, {"name": "已完成"}]}},
    {"field_id": "fld4", "field_name": "日期", "type": 5},
    {"field_id": "fld5", "field_name": "复选", "type": 7},
    {"field_id": "fld6", "field_name": "网址", "type": 15},
    {"field_id": "fld7", "field_name": "公式", "type": 20},
]
ENTRIES = [{"alias": "项目", "app_token": "base1", "table_id": "tbl1"}]


class FakeClient:
    from tele_bot.bitable.client import BitableClient
    field_fingerprint = staticmethod(BitableClient.field_fingerprint)

    def __init__(self):
        self.definitions = deepcopy(FIELDS)
        self.rows = {"rec1": {"record_id": "rec1", "fields": {"名称": "A", "数量": 0, "状态": "进行中"}},
                     "rec2": {"record_id": "rec2", "fields": {"名称": "B", "数量": 2}}}
        self.calls, self.writes = [], []
        self.error = None
        self.verify_error = False
        self.repeat_cursor = False
        self.description = {"base_name": "内容管理", "url": "https://tenant.feishu.cn/base/base1",
                            "tables": {"tbl1": "选题", "tbl2": "渠道", "tbl3": "其他"},
                            "permissions": {"view": True, "edit": True}, "errors": {}}

    def describe_base(self, app_token):
        self.calls.append("describe")
        return deepcopy(self.description) | {"app_token": app_token}

    def scope(self):
        return nullcontext()

    def fields(self, target):
        self.calls.append("fields")
        return deepcopy(self.definitions)

    def get(self, target, rid):
        return deepcopy(self.rows[rid])

    def records(self, target, ids):
        self.calls.append("records")
        if self.verify_error and self.writes:
            raise BitableError("transient", "核实读取超时")
        if not set(ids) <= set(self.rows):
            raise BitableError("not_found", "记录不存在")
        return {rid: deepcopy(self.rows[rid]) for rid in ids}

    def call(self, action, app_token, table_id=None, **kwargs):
        self.calls.append(action)
        if action == "search":
            rows = deepcopy(list(self.rows.values()))
            start = int(kwargs.get("cursor") or 0)
            size = kwargs["size"]
            end = start + size
            return {"items": rows[start:end], "has_more": end < len(rows),
                    "page_token": str(start if self.repeat_cursor else end)}
        self.writes.append((action, deepcopy(kwargs)))
        if self.error:
            raise BitableError(self.error, "远端写响应未知")
        records = kwargs["body"]["records"]
        if action == "delete":
            for rid in records:
                self.rows.pop(rid, None)
            return {"records": [{"record_id": rid, "deleted": True} for rid in records]}
        result = []
        for item in records:
            rid = f"rec{len(self.rows) + 1}" if action == "create" else item["record_id"]
            self.rows.setdefault(rid, {"record_id": rid, "fields": {}})["fields"].update(item["fields"])
            result.append(deepcopy(self.rows[rid]))
        return {"records": result}


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = BitableSettings(app_id="app", app_secret="secret",
                                        sqlite_path=Path(self.tmp.name) / "base.sqlite")
        self.client = FakeClient()
        self.policy = BitablePolicy(deepcopy(ENTRIES))
        self.store = BitableStore(self.settings.sqlite_path)
        self.service = BitableService(self.settings, self.client, self.policy, self.store)
        self.message = IncomingMessage("feishu", "user", "chat", "请新增、修改或删除指定测试记录", "msg1")

    def message_for(self, **kwargs):
        return replace(self.message, **kwargs)
