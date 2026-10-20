"""Schema-driven Base display values. Unknown objects never become JSON facts."""

from datetime import datetime
from zoneinfo import ZoneInfo
import math


class FieldValueError(ValueError):
    pass


SUPPORTED_FIELD_TYPES = {
    1,
    2,
    3,
    4,
    5,
    7,
    11,
    13,
    15,
    17,
    18,
    19,
    20,
    21,
    1001,
    1002,
    1003,
    1004,
    1005,
}


def scalar(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise FieldValueError("非有限数字不可解释")
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    raise FieldValueError("无法解释的标量结果")


def field_text(field, value, *, timezone="UTC"):
    kind = field["type"]
    prop = field.get("property") or {}
    if kind not in SUPPORTED_FIELD_TYPES:
        raise FieldValueError(f"暂不支持字段类型 {kind}")
    if value is None:
        if kind in {19, 20}:
            raise FieldValueError("公式/查找结果为空或未返回，未重新计算")
        return None
    if kind in {1, 13, 1005}:
        if isinstance(value, list):
            return "".join(
                scalar(item["text"])
                if isinstance(item, dict) and "text" in item
                else scalar(item)
                for item in value
            )
        return scalar(value)
    if kind == 2:
        result = scalar(value)
        details = [str(prop[k]) for k in ("formatter", "currency_code") if prop.get(k)]
        return result + (
            " [源格式/单位：" + "；".join(details) + "]" if details else ""
        )
    if kind in {3, 4}:
        options = {
            o.get("id"): o.get("name")
            for o in prop.get("options", [])
            if isinstance(o, dict)
        }

        def option(item):
            if isinstance(item, dict):
                label = item.get("name") or options.get(item.get("id"))
                if not label:
                    raise FieldValueError("选项身份无法映射为名称")
                return scalar(label)
            text = scalar(item)
            return options.get(text) or text

        return (
            "；".join(option(item) for item in value)
            if isinstance(value, list)
            else option(value)
        )
    if kind in {5, 1001, 1002}:
        try:
            return (
                datetime.fromtimestamp(
                    float(value) / 1000, ZoneInfo(timezone)
                ).isoformat()
                + f" [{timezone}]"
            )
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise FieldValueError("日期毫秒时间戳不可解释") from exc
    if kind == 7:
        if type(value) is not bool:
            raise FieldValueError("复选框结果不是布尔值")
        return "是" if value else "否"
    if kind in {11, 1003, 1004}:
        people = value if isinstance(value, list) else [value]
        result = []
        for person in people:
            if not isinstance(person, dict) or not (
                person.get("name") or person.get("id")
            ):
                raise FieldValueError("人员缺少展示名和身份")
            result.append(
                str(
                    person.get("name")
                    or f"[人员身份：{person['id']}；展示名未授权/未返回]"
                )
            )
        return "；".join(result)
    if kind == 15:
        if not isinstance(value, dict) or not isinstance(value.get("link"), str):
            raise FieldValueError("链接缺少 URL")
        return f"{value.get('text') or '链接'} ({value['link']})"
    if kind == 17:
        if not isinstance(value, list):
            raise FieldValueError("附件结果不是列表")
        attachments = []
        for item in value:
            if not isinstance(item, dict) or not (
                item.get("file_token") or item.get("url")
            ):
                raise FieldValueError("附件缺少源引用")
            attachments.append(
                f"{item.get('name') or '[文件名未返回]'} [{item.get('type') or '类型未返回'}] "
                f"{item.get('url') or item['file_token']}（仅元数据，未下载正文）"
            )
        return "；".join(attachments)
    if kind in {18, 21}:
        if not isinstance(value, list):
            raise FieldValueError("关联记录结果不是列表")
        records = []
        for item in value:
            if isinstance(item, str):
                records.append(item)
            elif isinstance(item, dict) and (
                item.get("record_id") or item.get("record_ids")
            ):
                ids = item.get("record_ids") or [item["record_id"]]
                if not isinstance(ids, list) or any(
                    not isinstance(i, str) for i in ids
                ):
                    raise FieldValueError("关联记录身份不可解释")
                label = item.get("text")
                if isinstance(label, list):
                    label = "；".join(scalar(x) for x in label)
                records.append((scalar(label) + " " if label else "") + "；".join(ids))
            else:
                raise FieldValueError("关联记录缺少 record_id")
        return "；".join(records) + " [仅关联引用，未展开其他表]"
    if kind in {19, 20}:
        if (
            not isinstance(value, dict)
            or "value" not in value
            or value.get("type") in {19, 20, None}
        ):
            raise FieldValueError("公式/查找结果缺少可解释类型和值")
        definition = {"type": value["type"], "property": prop}
        raw = value["value"]
        if isinstance(raw, list) and value["type"] in {2, 5, 7, 15, 1001, 1002}:
            result = "；".join(
                field_text(definition, item, timezone=timezone) or "[空值]"
                for item in raw
            )
        else:
            result = field_text(definition, raw, timezone=timezone)
        return f"[{'公式' if kind == 20 else '查找'}结果：{result if result is not None else '[空值]'}；未重算]"
    raise FieldValueError(f"暂不支持字段类型 {kind}")
