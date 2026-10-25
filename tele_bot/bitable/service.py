"""Base application service: policy, bounded reads and durable write plans."""
from __future__ import annotations

from datetime import datetime
import json
import math
import re
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from tele_bot.bitable.models import (BitableError, canonical, digest,
                                     RecordFilter, RecordSort, NewRecord, RecordPatch, preview_body, preview_plan)
from tele_bot.bitable.policy import BitablePolicy, identifier

WRITABLE = {1, 2, 3, 4, 5, 7, 13, 15}
WRITE_INTENT = {
    "create": re.compile(r"新增|增加|添加|增添|创建|写入|录入|\b(create|add|insert)\b", re.I),
    "update": re.compile(r"修改|更改|更新|改成|改为|改到|改一下|改下|设为|设成|设置|定在|定为|调到|调整|标记|标为|标成|挪到|延期到|延后到|提前到|清空|\b(update|change|set|clear)\b", re.I),
    "delete": re.compile(r"删除|移除|\b(delete|remove)\b", re.I),
}


class BitableService:
    def __init__(self, settings, client, policy, store, *, configuration_error=None):
        self.settings, self.client, self.policy, self.store = settings, client, policy, store
        self.configuration_error = configuration_error

    @property
    def version(self):
        return digest([self.settings.app_id, self.settings.api_base_url,
                       self.policy.version if self.policy else "invalid"])

    def _target(self, value, message, action):
        if self.configuration_error or not self.policy or not self.store:
            raise BitableError("configuration", "多维表格配置不可用，请检查目标配置和操作数据库")
        registry = self._registry()
        try:
            return registry.resolve(value, message, action)
        except BitableError as exc:
            if exc.kind != "not_found" or not value.startswith("https://"):
                raise
        info = self._discover(value, message)
        _, table_id = self.policy.parse_url(value)
        if table_id is not None and table_id not in info["tables"]:
            raise BitableError("validation", "链接中的 table 参数未匹配飞书返回的数据表；"
                               "请调用 bitable_inspect 查看真实数据表后选择，不会自动改为其他表执行")
        return self._registry().resolve(value, message, action)

    def _namespace(self):
        return f"{self.settings.api_base_url}/{self.settings.app_id}"

    def _registry(self):
        entries = [t.listing() | {"app_token": t.app_token} for t in self.policy.targets]
        keys = {(t["app_token"], t["table_id"]) for t in entries}
        for row in self.store.discovered_targets(self._namespace()):
            if (row["app_token"], row["table_id"]) not in keys:
                entries.append(row | {"alias": "bitable-" + digest([row["app_token"], row["table_id"]])[:16]})
        if len(entries) > 100:
            raise BitableError("limit", "已知数据表超过 100 张，请缩小范围")
        return BitablePolicy(entries, origin=self.settings.api_base_url)

    def _discover(self, value, message):
        self._target_check(message)
        app_token, _ = self.policy.parse_url(value)
        with self.client.scope():
            info = self.client.describe_base(app_token)
        if "tables" in info["errors"]:
            error = info["errors"]["tables"]
            raise BitableError(error["error"], error["message"], code=error.get("code"))
        self.store.remember_targets(self._namespace(), app_token, info.get("base_name") or app_token, info["tables"])
        return info

    def scope_for(self, message):
        return self.version if message is not None else "unauthenticated"

    def inspect(self, target, message, *, page=1):
        if type(page) is not int or page < 1 or (target and page != 1):
            raise BitableError("validation", "页码无效，仅登记清单支持从 1 开始翻页")
        if not target:
            self._target_check(message)
            targets = self._registry().visible_targets(message)
            pages = max(1, (len(targets) + 4) // 5)
            if page > pages:
                raise BitableError("validation", f"页码无效，清单共 {pages} 页")
            start = (page - 1) * 5
            listed, descriptions = [], {}
            with self.client.scope():
                for item in targets[start:start + 5]:
                    if item.app_token not in descriptions:
                        descriptions[item.app_token] = self.client.describe_base(item.app_token)
                    info = descriptions[item.app_token]
                    if "tables" not in info["errors"]:
                        known_ids = {t.table_id for t in targets if t.app_token == item.app_token}
                        self.store.remember_targets(self._namespace(), item.app_token,
                                                    info.get("base_name") or item.app_token,
                                                    {tid: name for tid, name in info["tables"].items() if tid in known_ids})
                    view, edit = info["permissions"]["view"], info["permissions"]["edit"]
                    if view is True and edit is True:
                        level, label = "read_write", "可阅读、可编辑"
                    elif view is True and edit is False:
                        level, label = "read_only", "只读"
                    elif view is False and edit is False:
                        level, label = "no_access", "无阅读/编辑权限"
                    else:
                        level, label = "unknown", "权限未核实"
                    listed.append({"alias": item.alias, "table_id": item.table_id,
                                   "base_name": info.get("base_name") or "名称未能读取",
                                   "table_name": info["tables"].get(item.table_id, "数据表名称未能读取"),
                                   "url": info.get("url"), "permissions": info["permissions"],
                                   "permission_level": level, "permission_label": label,
                                   "permission_errors": info["errors"], "confirm_all": item.confirm_all})
            return {"status": "succeeded", "permission_source": "feishu_api",
                    "coverage": "known_resources_only",
                    "message": "名称与文档权限来自飞书 API；权限未核实不等于无权限，具体行列读写以实际操作为准",
                    "total": len(targets), "page": page, "pages": pages,
                    "next_page": page + 1 if page < pages else None,
                    "targets": listed}
        if target.startswith("https://"):
            info = self._discover(target, message)
            _, table_id = self.policy.parse_url(target)
            unmatched = table_id is not None and table_id not in info["tables"]
            if unmatched or (table_id is None and len(info["tables"]) != 1):
                if not info["tables"]:
                    message_text = "已识别 Base，但飞书未返回可用数据表；请核对文档或高级权限"
                elif unmatched:
                    message_text = ("已自动发现并记住此 Base；链接中的 table 参数未匹配飞书返回的数据表。"
                                    "请选择下列真实数据表的别名继续；尚未修改任何记录")
                else:
                    message_text = "已自动发现并记住此 Base；请选择具体数据表后再读结构或操作记录"
                return {"status": "succeeded", "coverage": "specified_base_only", "permission_source": "feishu_api",
                        "discovered": True, "requested_table_id": table_id, "requested_table_found": not unmatched if table_id else None,
                        "base_name": info.get("base_name"), "url": info.get("url"),
                        "permissions": info["permissions"], "permission_errors": info["errors"],
                        "message": message_text,
                        "tables": [t.listing() for t in self._registry().targets
                                   if t.app_token == info["app_token"] and t.table_id in info["tables"]]}
        target = self._target(target, message, "read")
        with self.store.lease(self._key(target)), self.client.scope():
            fields = self.client.fields(target)
        return {"status": "succeeded", "target": target.alias, "table_id": target.table_id,
                "fields": [{"field_id": f["field_id"], "field_name": f["field_name"], "type": f["type"],
                            "writable": f["type"] in WRITABLE,
                            "options": [o["name"] for o in (f.get("property") or {}).get("options", []) or []]}
                           for f in fields]}

    def _target_check(self, message):
        if message is None or message.channel != "feishu":
            raise BitableError("unauthorized", "需要已认证的飞书入口")
        if self.configuration_error or not self.policy or not self.store:
            raise BitableError("configuration", "多维表格配置不可用")

    def _key(self, target):
        return f"{self.settings.api_base_url}/{self.settings.app_id}/{target.key}"

    @staticmethod
    def _definitions(fields):
        by_name = {f["field_name"]: f for f in fields}
        if len(by_name) != len(fields):
            raise BitableError("validation", "字段名不唯一，不能可靠定位")
        return by_name

    def query(self, target, message, *, fields=None, filter=None, sort=None, record_id=None, limit=None, cursor=None,
              start_char=0, max_chars=4000):
        target = self._target(target, message, "read")
        size = self.settings.page_size if limit is None else limit
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 100:
            raise BitableError("validation", "limit 必须为 1–100")
        if (type(start_char) is not int or start_char < 0 or type(max_chars) is not int or not 1 <= max_chars <= 8000
                or (start_char and (not record_id or not fields or len(fields) != 1))):
            raise BitableError("validation", "长字段补读需指定 record_id、单个字段和有效字符范围")
        spec = {"fields": fields, "filter": filter, "sort": sort, "record_id": record_id}
        binding = digest([message.user_id, message.chat_id, self.version, target.key, spec])
        state = {"binding": binding, "remote": None, "seen": [], "count": 0, "pages": []}
        if cursor:
            state = self.store.read_cursor(cursor)
            if state["binding"] != binding:
                raise BitableError("unauthorized", "游标与当前用户、权限或查询范围不一致")
        remaining = self.settings.max_query_records - state["count"]
        if remaining <= 0:
            raise BitableError("limit", "累计查询记录超限，请缩小范围")
        with self.store.lease(self._key(target)), self.client.scope():
            definitions = self._definitions(self.client.fields(target))
            selected = list(definitions) if fields is None else fields
            if not selected or len(selected) != len(set(selected)) or any(f not in definitions for f in selected):
                raise BitableError("validation", "字段不存在、重复或字段选择为空")
            body = {"field_names": selected, "automatic_fields": True}
            if filter is not None:
                parsed = RecordFilter.model_validate(filter)
                if parsed.conjunction not in {"and", "or"}:
                    raise BitableError("validation", "不支持的筛选组合")
                for condition in parsed.conditions:
                    if condition.field_name not in definitions or condition.operator not in {
                        "is", "isNot", "contains", "doesNotContain", "isEmpty", "isNotEmpty",
                        "isGreater", "isGreaterEqual", "isLess", "isLessEqual",
                    }:
                        raise BitableError("validation", "筛选字段或运算符无效")
                    if definitions[condition.field_name]["type"] == 5 and condition.operator in {
                        "isNot", "contains", "doesNotContain", "isGreaterEqual", "isLessEqual",
                    }:
                        raise BitableError("validation", "该日期筛选运算符不支持")
                body["filter"] = parsed.model_dump()
            if sort:
                if len(sort) > 100:
                    raise BitableError("limit", "排序字段过多")
                parsed_sort = [RecordSort.model_validate(item) for item in sort]
                if any(item.field_name not in definitions for item in parsed_sort):
                    raise BitableError("validation", "排序字段不存在")
                body["sort"] = [item.model_dump() for item in parsed_sort]
            if record_id:
                if cursor or filter or sort:
                    raise BitableError("validation", "指定 record_id 时不能同时筛选/排序/续页")
                records, more, remote = [self.client.get(target, identifier(record_id))], False, None
            else:
                data = self.client.call("search", target.app_token, target.table_id, body=body,
                                        cursor=state["remote"], size=min(size, remaining))
                records, more, remote = data.get("items"), data.get("has_more"), data.get("page_token")
                if not isinstance(records, list) or type(more) is not bool or len(records) > min(size, remaining):
                    raise BitableError("invalid_response", "记录分页响应无效")
                if more and (not remote or remote in state["pages"]):
                    raise BitableError("pagination", "记录分页游标缺失或重复")
        seen = set(state["seen"])
        projected = []
        for record in records:
            rid = identifier(record.get("record_id"))
            if rid in seen:
                raise BitableError("pagination", "记录跨页重复，请重新查询")
            seen.add(rid)
            projected.append({"record_id": rid, "fields": {f: record.get("fields", {}).get(f) for f in selected}})
        state.update(seen=sorted(seen), count=state["count"] + len(records), remote=remote,
                     pages=state["pages"] + ([remote] if remote else []))
        capped = bool(more and state["count"] >= self.settings.max_query_records)
        next_cursor = self.store.cursor(state) if more and not capped else None
        result = {"status": "succeeded", "target": target.alias, "table_id": target.table_id,
                  "records": projected, "returned_count": len(records), "has_more": more,
                  "next_cursor": next_cursor, "read_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                  "scope": spec, "coverage": "page_only", "limit_reached": capped}
        # Never lose a long cell silently. Re-read its stable record_id with a narrow fields selection.
        if record_id and len(selected) == 1 and (start_char or len(canonical(result)) > self.settings.max_output_chars):
            content = canonical(projected[0]["fields"][selected[0]])
            if start_char > len(content):
                raise BitableError("validation", "字符偏移超出字段长度")
            end = min(len(content), start_char + max_chars)
            result["records"] = [{"record_id": record_id, "fields": {}}]
            result["field_content"] = {"field_name": selected[0], "encoding": "json", "text": content[start_char:end],
                                       "start_char": start_char, "next_start_char": end if end < len(content) else None,
                                       "total_chars": len(content), "value_hash": digest(content)}
            result.update(truncated=end < len(content), message="按字符顺序拼接相同 value_hash 的 text 后解析 JSON；字段可能在补读期间变化")
        elif len(canonical(result)) > self.settings.max_output_chars:
            result["records"] = [{"record_id": r["record_id"], "fields": {},
                                  "omitted_fields": list(r["fields"])} for r in projected]
            result.update(truncated=True, message="字段内容超出输出预算；用 record_id 和更窄字段选择补读，原文未返回")
        return result

    def _encode(self, fields, values, clears=()):
        definitions = self._definitions(fields)
        if not isinstance(values, dict) or set(values) & set(clears) or len(clears) != len(set(clears)):
            raise BitableError("validation", "字段补丁或清空列表无效")
        result = {}
        for name, value in list(values.items()) + [(name, None) for name in clears]:
            if name not in definitions or definitions[name]["type"] not in WRITABLE:
                raise BitableError("validation", "字段不存在或不支持写入")
            field, clear = definitions[name], name in clears
            kind = field["type"]
            if value is None and not clear:
                raise BitableError("validation", "请使用 clear_fields 明确清空意图")
            if clear:
                result[name] = False if kind == 7 else None
                continue
            valid = False
            if kind in {1, 13}:
                valid = isinstance(value, str) and len(value) <= 10000
            elif kind == 2:
                valid = type(value) in {float, int} and math.isfinite(value)
            elif kind == 7:
                valid = type(value) is bool
            elif kind in {3, 4}:
                options = {o["name"] for o in (field.get("property") or {}).get("options", []) or []}
                valid = (isinstance(value, str) and value in options if kind == 3 else
                         isinstance(value, list) and len(value) == len(set(value))
                         and all(isinstance(v, str) and v in options for v in value))
            elif kind == 5:
                if isinstance(value, str):
                    parsed = datetime.fromisoformat(value)
                    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    value = int(parsed.timestamp() * 1000)
                valid = type(value) is int and 0 <= value <= 253402214400000
            elif kind == 15:
                valid = (isinstance(value, dict) and set(value) <= {"text", "link"}
                         and isinstance(value.get("text", ""), str) and isinstance(value.get("link"), str)
                         and urlsplit(value["link"]).scheme in {"http", "https"})
            if not valid:
                raise BitableError("validation", "字段值格式或选项无效；不会自动新增选项")
            result[name] = value
        if not result:
            raise BitableError("validation", "不能提交空记录或空补丁")
        return result

    def write(self, action, target, message, records):
        target = self._target(target, message, action)
        if not message.message_id:
            raise BitableError("unauthorized", "写入需要真实消息 ID，不能从未认证入口执行")
        if (action not in WRITE_INTENT or not WRITE_INTENT[action].search(message.text)
                or re.search(r"不要|禁止|不许|别(?:再|去|给|帮)?(?:新增|增加|添加|增添|创建|录入|删除|修改|更新|写入)|\b(do not|don't|never)\b", message.text, re.I)
                or re.match(r"\s*(?:请问)?(?:如何|怎么|为什么|假如|如果)", message.text)):
            raise BitableError("write_intent", "未识别到本次写入要求，请说明要新增、修改或删除的内容")
        if not records or len(records) > self.settings.max_write_records:
            raise BitableError("limit", "写入记录数为空或超限，请缩小范围")
        normalized = []
        if action == "delete":
            normalized = [identifier(r) for r in records]
        elif action == "create":
            normalized = [NewRecord.model_validate(r).model_dump() for r in records]
        elif action == "update":
            normalized = [RecordPatch.model_validate(r).model_dump() for r in records]
            for r in normalized:
                identifier(r["record_id"])
        else:
            raise BitableError("validation", "未知写入操作")
        ids = normalized if action == "delete" else [r["record_id"] for r in normalized] if action == "update" else []
        if len(ids) != len(set(ids)):
            raise BitableError("validation", "记录身份重复")
        request_scope = digest([message.channel, message.message_id, message.user_id, message.chat_id,
                                self._key(target), action])
        dedup = digest([request_scope, sorted(normalized, key=canonical)])
        with self.store.lease(self._key(target)), self.client.scope():
            # Look up before reading changed rows: replay must return the original receipt.
            with self.store.connect() as db:
                prior = db.execute("SELECT id FROM operations WHERE dedup=?", (dedup,)).fetchone()
            if prior:
                previous = self.store.get(prior[0])
                if previous["policy"] != self.version:
                    raise BitableError("conflict", "同一消息的原操作已登记，但授权版本已变化；不会重复执行")
                return self.receipt(previous)
            with self.store.connect() as db:
                sibling = db.execute("SELECT id FROM operations WHERE request_scope=? "
                                     "AND status NOT IN ('cancelled','expired','failed','conflict')",
                                     (request_scope,)).fetchone()
            if sibling:
                raise BitableError("conflict", "同一消息该目标已有操作，请勿变换内容重试或拆分批量请求；查询原操作状态")
            fields = self.client.fields(target)
            before = self.client.records(target, ids) if ids else {}
            encoded = normalized if action == "delete" else [
                {**({"record_id": r["record_id"]} if action == "update" else {}),
                 "fields": self._encode(fields, r["fields"], r.get("clear_fields", []))}
                for r in normalized]
            plan = {"alias": target.alias, "table_name": target.table_name, "target": target.key, "records": encoded, "before": before,
                    "schema": self.client.field_fingerprint(fields)}
            if len(canonical(plan).encode("utf-8")) > self.settings.max_response_bytes:
                raise BitableError("limit", "变更快照过大，请减少记录/字段；未生成写入")
            needs_confirmation = target.confirm_all or action == "delete" or len(records) > 1
            if needs_confirmation and len(preview_body(plan, action)) > 3800:
                raise BitableError("limit", "变更范围超过确认卡片预算，请减少记录/字段；未生成写入")
            row = self.store.prepare(dedup=dedup, request_scope=request_scope, message=message,
                                     policy=self.version, target=target.key,
                                     action=action, plan=plan, ttl=self.settings.confirm_ttl)
            if needs_confirmation:
                self.store.finish(row["id"], "awaiting_confirmation", {"preview": plan})
                return self.receipt(self.store.get(row["id"]))
            return self._execute(row, target)

    def receipt(self, row):
        result = json.loads(row["result"]) if row.get("result") else {}
        if row["status"] in {"prepared", "awaiting_confirmation"}:
            if row["expires"] <= time.time():
                self.store.finish(row["id"], "expired", {"message": "确认已过期，请重新准备"})
                return self.receipt(self.store.get(row["id"]))
            result = {"preview": preview_plan(json.loads(row["plan"])), "confirmation_token": row["token"]}
        if row["status"] not in {"prepared", "awaiting_confirmation"}:
            result["change_summary"] = preview_body(json.loads(row["plan"]), row["action"])
        return {**result, "status": row["status"], "operation_id": row["id"], "action": row["action"]}

    def confirm(self, token, message, *, cancel=False):
        if self.store is None:
            raise BitableError("configuration", "表格操作数据库不可用")
        row = self.store.get(token)
        if row["user_id"] != message.user_id or row["chat_id"] != message.chat_id:
            raise BitableError("unauthorized", "确认必须来自原用户和原会话")
        plan = json.loads(row["plan"])
        target = self._target(plan["alias"], message, row["action"])
        if row["policy"] != self.version or row["target"] != target.key:
            raise BitableError("unauthorized", "授权或目标已变化，请重新准备操作")
        with self.store.lease(self._key(target)), self.client.scope():
            row = self.store.get(row["id"])
            if row["status"] not in {"prepared", "awaiting_confirmation"}:
                return self.receipt(row)
            if row["expires"] <= time.time():
                self.store.finish(row["id"], "expired", {"message": "确认已过期"})
            elif cancel:
                self.store.finish(row["id"], "cancelled", {"message": "操作已取消"})
            else:
                return self._execute(row, target)
            return self.receipt(self.store.get(row["id"]))

    @staticmethod
    def _values_equal(actual, expected):
        if expected is None:
            return actual is None or actual == "" or actual == []
        if isinstance(actual, list) and isinstance(expected, str):
            return "".join(x.get("text", "") for x in actual if isinstance(x, dict)) == expected
        return actual == expected

    def _execute(self, row, target):
        plan = json.loads(row["plan"])
        if not self.store.claim(row["id"]):
            return self.receipt(self.store.get(row["id"]))
        dispatched = False
        submitted = None
        try:
            fields = self.client.fields(target)
            if self.client.field_fingerprint(fields) != plan["schema"]:
                raise BitableError("conflict", "字段结构已变化，请重新准备")
            if plan["before"]:
                current = self.client.records(target, list(plan["before"]))
                patches = {r["record_id"]: r["fields"] for r in plan["records"]} if row["action"] == "update" else {}
                if any(any(not self._values_equal(current[r].get("fields", {}).get(name),
                                                   old.get("fields", {}).get(name))
                           for name in patches[r]) if row["action"] == "update" else
                       canonical(current[r].get("fields", {})) != canonical(old.get("fields", {}))
                       for r, old in plan["before"].items()):
                    raise BitableError("conflict", "记录内容已变化，请重新准备")
            dispatched = True
            kwargs = {"body": {"records": plan["records"]}}
            if row["action"] == "create":
                kwargs["client_token"] = row["client_token"]
            submitted = self.client.call(row["action"], target.app_token, target.table_id, **kwargs)
            # Persist server acceptance before any verification read, never replay on read failure.
            self.store.finish(row["id"], "unknown", {"submitted": True, "response": submitted,
                                                       "verification": "pending"})
            returned = submitted.get("records")
            if not isinstance(returned, list) or len(returned) != len(plan["records"]):
                raise BitableError("invalid_response", "写响应数量不一致，需要核对")
            if row["action"] == "delete":
                expected = set(plan["records"])
                if {r.get("record_id") for r in returned} != expected:
                    raise BitableError("invalid_response", "删除结果记录身份不一致")
                success = sum(r.get("deleted") is True for r in returned)
                status = "succeeded" if success == len(returned) else "partial"
                result = {"records": returned, "succeeded_count": success, "verification": "delete_receipts"}
            else:
                ids = [identifier(r.get("record_id")) for r in returned]
                if len(set(ids)) != len(ids):
                    raise BitableError("invalid_response", "写响应包含重复记录")
                if row["action"] == "update" and set(ids) != {r["record_id"] for r in plan["records"]}:
                    raise BitableError("invalid_response", "修改结果记录身份不一致")
                verified = self.client.records(target, ids)
                patches = {r["record_id"]: r["fields"] for r in plan["records"]} if row["action"] == "update" else dict(zip(ids, [r["fields"] for r in plan["records"]]))
                mismatch = [rid for rid in ids if any(not self._values_equal(verified[rid].get("fields", {}).get(k), v)
                                                     for k, v in patches[rid].items())]
                status = "conflict" if mismatch else "succeeded"
                result = {"submitted": True, "record_ids": ids, "succeeded_count": len(ids) - len(mismatch),
                          "verification": "mismatch" if mismatch else "verified", "conflicting_records": mismatch}
            self.store.finish(row["id"], status, result)
        except Exception as exc:
            error = exc.result() if isinstance(exc, BitableError) else {"error": "internal", "message": "表格操作未能核实，请查询操作状态"}
            if submitted is not None:
                status = "unknown"
                error.update(submitted=True, response=submitted, verification="unverified")
            elif dispatched:
                # Even a platform error can follow a committed write. Do not automatically retry.
                status = ("failed" if isinstance(exc, BitableError)
                          and exc.kind in {"unauthorized", "configuration", "not_found", "validation"}
                          else "unknown")
            else:
                status = "conflict" if isinstance(exc, BitableError) and exc.kind == "conflict" else "failed"
            self.store.finish(row["id"], status, error)
        return self.receipt(self.store.get(row["id"]))
