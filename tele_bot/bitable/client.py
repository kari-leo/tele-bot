"""Lazy official SDK adapter. Reads retry; ambiguous writes never replay here."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import importlib
import json
import threading
import time

from tele_bot.bitable.models import BitableError, canonical
from tele_bot.bitable.policy import identifier


class BitableClient:
    def __init__(self, settings, *, sdk_factory=None, sleep=time.sleep):
        self.settings, self._factory, self._sleep = settings, sdk_factory, sleep
        self._sdk = None
        self._init_lock = threading.Lock()
        self._budget = ContextVar("bitable_request_budget", default=None)

    @contextmanager
    def scope(self):
        if self._budget.get() is not None:
            yield
            return
        token = self._budget.set([self.settings.max_requests, self.settings.max_response_bytes * 2,
                                  time.monotonic() + self.settings.operation_timeout])
        try:
            yield
        finally:
            self._budget.reset(token)

    def _get_sdk(self):
        with self._init_lock:
            if self._sdk is None:
                if self.settings.api_base_url not in {"https://open.feishu.cn", "https://open.larksuite.com"}:
                    raise BitableError("configuration", "API 域必须为官方飞书/Lark 域")
                if not self.settings.app_id or not self.settings.app_secret:
                    raise BitableError("configuration", "缺少多维表格应用凭据")
                try:
                    if self._factory:
                        self._sdk = self._factory()
                    else:
                        sdk = importlib.import_module("lark_oapi")
                        self._sdk = (sdk.Client.builder().app_id(self.settings.app_id)
                                     .app_secret(self.settings.app_secret).domain(self.settings.api_base_url)
                                     .timeout(self.settings.timeout).log_level(sdk.LogLevel.ERROR).build())
                except Exception:
                    raise BitableError("configuration", "官方 SDK 初始化失败，请检查依赖及应用配置") from None
            return self._sdk

    def _request(self, action, app_token, table_id=None, *, body=None, cursor=None, size=None, record_id=None,
                 client_token=None, permission_action=None):
        sdk = self._get_sdk()
        if action == "permission":
            api = importlib.import_module("lark_oapi.api.drive.v1")
            request = (api.AuthPermissionMemberRequest.builder().token(identifier(app_token))
                       .type("bitable").action(permission_action).build())
            return sdk.drive.v1.permission_member.auth, request
        api = importlib.import_module("lark_oapi.api.bitable.v1")
        names = {"metadata": ("GetAppRequest", "app", "get"),
                 "tables": ("ListAppTableRequest", "app_table", "list"),
                 "fields": ("ListAppTableFieldRequest", "app_table_field", "list"),
                 "search": ("SearchAppTableRecordRequest", "app_table_record", "search"),
                 "get": ("GetAppTableRecordRequest", "app_table_record", "get"),
                 "batch_get": ("BatchGetAppTableRecordRequest", "app_table_record", "batch_get"),
                 "create": ("BatchCreateAppTableRecordRequest", "app_table_record", "batch_create"),
                 "update": ("BatchUpdateAppTableRecordRequest", "app_table_record", "batch_update"),
                 "delete": ("BatchDeleteAppTableRecordRequest", "app_table_record", "batch_delete")}
        name, resource, method = names[action]
        builder = getattr(api, name).builder().app_token(identifier(app_token))
        if table_id:
            builder.table_id(identifier(table_id))
        if record_id:
            builder.record_id(identifier(record_id))
        if cursor:
            builder.page_token(cursor)
        if size:
            builder.page_size(size)
        if hasattr(builder, "user_id_type"):
            builder.user_id_type("open_id")
        if client_token:
            builder.client_token(client_token)
        request = builder.build()
        if body is not None:
            # Native JSON avoids SDK model Encoder.filter_null removing explicit clears.
            request.body = body
        return getattr(getattr(sdk.bitable.v1, resource), method), request

    def call(self, action, app_token, table_id=None, **kwargs):
        read = action in {"metadata", "permission", "tables", "fields", "search", "get", "batch_get"}
        for attempt in range(3 if read else 1):
            budget = self._budget.get()
            if budget is not None:
                if budget[0] <= 0 or time.monotonic() >= budget[2]:
                    raise BitableError("limit", "多维表格请求/时间预算耗尽，请缩小范围")
                budget[0] -= 1
            try:
                method, request = self._request(action, app_token, table_id, **kwargs)
                response = method(request)
                raw = response.raw
                content = raw.content
                if isinstance(content, str):
                    content = content.encode()
                if len(content) > self.settings.max_response_bytes:
                    raise BitableError("limit", "API 响应过大，请缩小范围")
                if budget is not None:
                    budget[1] -= len(content)
                    if budget[1] < 0:
                        raise BitableError("limit", "API 累计响应预算耗尽")
                status = raw.status_code
                payload = json.loads(content)
                code = payload.get("code")
                headers = {str(k).lower(): str(v) for k, v in (raw.headers or {}).items()}
                if status != 200 or code != 0:
                    if status in {401, 403} or code in {99991672, 99991676, 1254302}:
                        kind = "unauthorized"
                    elif status == 404 or code in {1254040, 1254041, 1254043}:
                        kind = "not_found"
                    elif status == 429 or code in {1254290, 99991400}:
                        kind = "rate_limited"
                    elif code in {1254291, 1254607}:
                        kind = "conflict"
                    elif status >= 500 or code in {1255001, 1255002, 1255040}:
                        kind = "transient"
                    else:
                        kind = "api_error"
                    raise BitableError(kind, f"飞书 API 未成功（HTTP {status}, code={code}）",
                                       code=code, log_id=headers.get("x-tt-logid"),
                                       retry_after=self._retry_after(headers.get("retry-after")))
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise BitableError("invalid_response", "API 缺少有效 data")
                return data
            except BitableError as exc:
                error = exc
            except (ImportError, AttributeError):
                raise BitableError("configuration", "SDK 不可用或版本不兼容，请安装锁定依赖") from None
            except Exception as exc:
                if type(exc).__module__.startswith("lark_oapi"):
                    kind = "unauthorized" if "Token" in type(exc).__name__ or "Authorization" in type(exc).__name__ else "invalid_response"
                    error = BitableError(kind, "SDK 认证或响应处理失败，请检查应用权限与接口契约",
                                         code=getattr(exc, "code", None))
                else:
                    error = BitableError("transient", "SDK 请求超时或连接失败，结果尚未确定")
            if read and error.kind in {"transient", "rate_limited", "conflict"} and attempt < 2:
                self._sleep(max(error.retry_after, 2 ** attempt))
                continue
            raise error from None

    @staticmethod
    def _retry_after(value):
        try:
            return min(5, max(0, float(value))) if value is not None else 1
        except (ValueError, TypeError):
            return 1

    def describe_base(self, app_token):
        """Read remote identity and document permissions without trial writes."""
        result = {"app_token": app_token, "base_name": None, "url": None, "tables": {},
                  "permissions": {"view": None, "edit": None}, "errors": {}}
        try:
            app = self.call("metadata", app_token).get("app", {})
            if not isinstance(app, dict) or app.get("app_token") != app_token or not app.get("name"):
                raise BitableError("invalid_response", "Base 元数据缺少有效身份或名称")
            result.update(base_name=app["name"], url=app.get("url"))
        except BitableError as exc:
            result["errors"]["metadata"] = exc.result()
        try:
            cursor, seen = None, set()
            for _ in range(10):
                data = self.call("tables", app_token, cursor=cursor, size=100)
                items = data.get("items")
                if not isinstance(items, list) or any(not isinstance(t, dict) or not t.get("table_id") or not t.get("name") for t in items):
                    raise BitableError("invalid_response", "数据表列表缺少有效身份或名称")
                for table in items:
                    if table["table_id"] in result["tables"]:
                        raise BitableError("pagination", "数据表跨页重复")
                    result["tables"][table["table_id"]] = table["name"]
                if data.get("has_more") is False:
                    break
                cursor = data.get("page_token")
                if data.get("has_more") is not True or not cursor or cursor in seen:
                    raise BitableError("pagination", "数据表分页游标无效")
                seen.add(cursor)
            else:
                raise BitableError("limit", "数据表分页超限")
        except BitableError as exc:
            result["errors"]["tables"] = exc.result()
        for action in ("view", "edit"):
            try:
                value = self.call("permission", app_token, permission_action=action).get("auth_result")
                if type(value) is not bool:
                    raise BitableError("invalid_response", "权限查询未返回明确结果")
                result["permissions"][action] = value
            except BitableError as exc:
                result["errors"][action] = exc.result()
        return result

    def fields(self, target):
        fields, cursor, seen = [], None, set()
        for _ in range(10):
            data = self.call("fields", target.app_token, target.table_id, cursor=cursor, size=100)
            items = data.get("items")
            if not isinstance(items, list):
                raise BitableError("invalid_response", "字段列表无效")
            fields.extend(items)
            if len(fields) > 256:
                raise BitableError("limit", "字段数量超限")
            if data.get("has_more") is False:
                if len({f["field_id"] for f in fields}) != len(fields):
                    raise BitableError("pagination", "字段跨页重复")
                return fields
            cursor = data.get("page_token")
            if data.get("has_more") is not True or not cursor or cursor in seen:
                raise BitableError("pagination", "字段分页游标无效")
            seen.add(cursor)
        raise BitableError("limit", "字段分页超限")

    def get(self, target, record_id):
        result = self.call("get", target.app_token, target.table_id, record_id=record_id).get("record")
        if not isinstance(result, dict) or result.get("record_id") != record_id:
            raise BitableError("invalid_response", "记录身份不一致")
        return result

    def records(self, target, record_ids):
        data = self.call("batch_get", target.app_token, target.table_id,
                         body={"record_ids": record_ids, "automatic_fields": True})
        records = data.get("records")
        if not isinstance(records, list) or {r.get("record_id") for r in records} != set(record_ids):
            raise BitableError("not_found", "部分目标记录不存在或不可读，请重新查询")
        return {r["record_id"]: r for r in records}

    @staticmethod
    def field_fingerprint(fields):
        return canonical(sorted(fields, key=lambda item: item["field_id"]))
