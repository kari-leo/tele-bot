"""Bounded, model-visible business schemas and safe service errors."""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from tele_bot.models import IncomingMessage

CURRENT_BITABLE_MESSAGE: ContextVar[IncomingMessage | None] = ContextVar("bitable_message", default=None)
CURRENT_BITABLE_RECEIPTS: ContextVar[list | None] = ContextVar("bitable_receipts", default=None)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


ACTION_LABELS = {"create": "新增", "update": "修改", "delete": "删除"}


def _field_definitions(plan):
    try:
        return {f["field_name"]: f for f in json.loads(plan.get("schema", "[]"))}
    except (ValueError, TypeError, KeyError):
        return {}


def _cell_text(value, definition=None):
    if value is None or value == "" or value == []:
        return "空"
    if definition and definition.get("type") == 5 and isinstance(value, (int, float)):
        try:
            local = datetime.fromtimestamp(value / 1000, ZoneInfo("Asia/Shanghai"))
            return local.strftime("%Y-%m-%d %H:%M" if any((local.hour, local.minute, local.second)) else "%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass
    if isinstance(value, bool):
        return "已勾选" if value else "未勾选"
    if isinstance(value, list):
        return "、".join(_cell_text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("link") or "（复杂内容）")
    return str(value)


def _md(value):
    text = str(value).replace("\r", " ").replace("\n", " ")
    for char in ("\\", "*", "_", "`", "[", "]", "<", ">", "|"):
        text = text.replace(char, "\\" + char)
    return text


def preview_plan(plan):
    """A model-visible preview; the full immutable snapshot stays in the ledger."""
    definitions = _field_definitions(plan)
    wanted = {name for record in plan["records"] if isinstance(record, dict) for name in record.get("fields", {})}
    names = {"任务名称", "名称", "标题", "name", "title"}
    names.update(f["field_name"] for f in definitions.values() if f.get("is_primary"))
    selected = wanted | names
    schema = [{k: f[k] for k in ("field_name", "type", "is_primary") if k in f}
              for name, f in definitions.items() if name in selected]
    return {**plan, "before": {rid: {"fields": {k: v for k, v in row.get("fields", {}).items() if k in selected}}
                                for rid, row in plan.get("before", {}).items()}, "schema": canonical(schema)}


def preview_body(plan, action):
    """Show exactly the affected fields, never whole records or raw timestamps."""
    definitions = _field_definitions(plan)
    primary = next((f["field_name"] for f in definitions.values() if f.get("is_primary")), None)
    names = [primary, "任务名称", "名称", "标题", "name", "title"]
    label = plan.get("table_name") or plan["alias"]
    lines = [f"数据表：{_md(label)}", f"{ACTION_LABELS.get(action, action)} {len(plan['records'])} 条记录"]
    for index, record in enumerate(plan["records"], 1):
        rid = record if isinstance(record, str) else record.get("record_id", "")
        before = plan.get("before", {}).get(rid, {}).get("fields", {})
        after = record.get("fields", {}) if isinstance(record, dict) else {}
        source = after if action == "create" else before
        title = next((_cell_text(source[n]) for n in names if n and source.get(n)), rid or f"新记录 {index}")
        lines.append(f"\n{index}. **{_md(title)}**")
        for name, value in after.items():
            new = _md(_cell_text(value, definitions.get(name)))
            if action == "update":
                old = _md(_cell_text(before.get(name), definitions.get(name)))
                lines.append(f"   - {_md(name)}：{old} → {new}")
            else:
                lines.append(f"   - {_md(name)}：{new}")
    return "\n".join(lines)


class BitableError(RuntimeError):
    def __init__(self, kind, message, *, code=None, log_id=None, retry_after=0):
        self.kind, self.code, self.log_id, self.retry_after = kind, code, log_id, retry_after
        super().__init__(message)

    def result(self):
        return {"status": "failed", "error": self.kind, "message": str(self),
                "code": self.code, "log_id": self.log_id}


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(Schema):
    field_name: StrictStr = Field(min_length=1, max_length=1000)
    operator: StrictStr
    value: list[StrictStr] = Field(default_factory=list, max_length=10)


class RecordFilter(Schema):
    conjunction: StrictStr = "and"
    conditions: list[Condition] = Field(min_length=1, max_length=50)


class RecordSort(Schema):
    field_name: StrictStr
    desc: bool = False


class NewRecord(Schema):
    fields: dict[str, object]


class RecordPatch(Schema):
    record_id: StrictStr
    fields: dict[str, object] = Field(default_factory=dict)
    clear_fields: list[StrictStr] = Field(default_factory=list, max_length=256)
