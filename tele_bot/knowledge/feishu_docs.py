"""Independent read-only Feishu Docs transport with bounded retry/pagination."""

from __future__ import annotations

import re
import threading
import time
from typing import Callable
from contextlib import contextmanager
from contextvars import ContextVar

import httpx


class CloudAPIError(RuntimeError):
    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(f"{kind}: {message}")


def resource_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise ValueError("invalid Feishu resource identifier")
    return value


class FeishuDocsClient:
    ALLOWED_ORIGINS = {"https://open.feishu.cn", "https://open.larksuite.com"}
    REVOKED_CODES = {
        1254302,
        1770032,
        91403,
        90213,
        90303,
        91204,
        95008,
        95009,
        99991672,
        99991676,
        99991679,
    }
    NOT_FOUND_CODES = {
        1254040,
        1254041,
        1770002,
        1770003,
        90214,
        90215,
        90304,
        90305,
        91005,
        91203,
        91205,
        91402,
        95007,
        1002,
        93004,
    }
    LIMIT_CODES = {90219, 90221, 90222, 90226, 90227, 1770034}
    RATE_CODES = {99991400, 90217}
    TRANSIENT_CODES = {90203, 90235, 90242, 1500, 1503, 1551, 5000, 95001}

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        api_base_url="https://open.feishu.cn",
        request: Callable | None = None,
        sleep: Callable = time.sleep,
        max_bytes=30 * 1024 * 1024,
        max_pages=100,
        max_items=50000,
        retries=2,
        max_requests=512,
        max_total_bytes=100 * 1024 * 1024,
    ):
        if api_base_url.rstrip("/") not in self.ALLOWED_ORIGINS:
            raise ValueError("Docs API must use a fixed official Feishu/Lark origin")
        self.app_id, self._secret = app_id, app_secret
        self.api_base_url = api_base_url.rstrip("/")
        self._request, self._sleep = request or self._stream_request, sleep
        self.max_bytes, self.max_pages, self.max_items, self.retries = (
            max_bytes,
            max_pages,
            max_items,
            retries,
        )
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()
        self.max_requests, self.max_total_bytes = max_requests, max_total_bytes
        self._budget = ContextVar("docs_read_budget", default=None)

    @contextmanager
    def read_scope(self):
        """Nested embedded reads share one per-operation budget, isolated by context."""
        if self._budget.get() is not None:
            yield
            return
        token = self._budget.set([self.max_requests, self.max_total_bytes])
        try:
            yield
        finally:
            self._budget.reset(token)

    def _stream_request(self, method, url, **kwargs):
        with httpx.stream(method, url, follow_redirects=False, **kwargs) as response:
            content, size = [], 0
            for part in response.iter_bytes():
                size += len(part)
                if size > self.max_bytes:
                    raise CloudAPIError("limit", "单次 API 响应超限，请缩小导入范围")
                content.append(part)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(content),
            )

    def _call(self, method, path, *, params=None, body=None, auth=True):
        if not re.fullmatch(r"/open-apis/[A-Za-z0-9_!/:.\-]+", path):
            raise ValueError("invalid Docs API path")
        for attempt in range(self.retries + 1):
            budget = self._budget.get()
            if budget is not None:
                if budget[0] <= 0:
                    raise CloudAPIError("limit", "云来源读取请求预算耗尽，请缩小范围")
                budget[0] -= 1
            headers = {"Authorization": f"Bearer {self.access_token()}"} if auth else {}
            try:
                response = self._request(
                    method,
                    self.api_base_url + path,
                    headers=headers,
                    params=params,
                    json=body,
                    timeout=30,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.retries:
                    self._sleep(2**attempt)
                    continue
                raise CloudAPIError(
                    "transient", "API 超时或连接失败；未取得完整快照"
                ) from exc
            if len(response.content) > self.max_bytes:
                raise CloudAPIError("limit", "API 响应超限，请缩小导入范围")
            if budget is not None:
                budget[1] -= len(response.content)
                if budget[1] < 0:
                    raise CloudAPIError(
                        "limit", "云来源累计响应字节预算耗尽，请缩小范围"
                    )
            status = response.status_code
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = payload.get("code") if isinstance(payload, dict) else None
            retryable = (
                status == 429
                or status >= 500
                or code in self.RATE_CODES | self.TRANSIENT_CODES
            )
            if status == 401 and auth and attempt < self.retries:
                with self._lock:
                    self._token, self._expires = "", 0.0
                continue
            if retryable and attempt < self.retries:
                try:
                    delay = min(
                        10,
                        max(0, float(response.headers.get("Retry-After", 2**attempt))),
                    )
                except ValueError:
                    delay = 2**attempt
                self._sleep(delay)
                continue
            if status in {401, 403, 404}:
                raise CloudAPIError(
                    {401: "auth", 403: "revoked", 404: "not_found"}[status],
                    f"API HTTP {status}",
                )
            if retryable:
                raise CloudAPIError(
                    "rate_limited"
                    if status == 429 or code in self.RATE_CODES
                    else "transient",
                    "API 重试耗尽",
                )
            if not 200 <= status < 300 or code != 0:
                # Do not expose remote messages, which can contain request/credential data.
                kind = (
                    "revoked"
                    if code in self.REVOKED_CODES
                    else "not_found"
                    if code in self.NOT_FOUND_CODES
                    else "limit"
                    if code in self.LIMIT_CODES
                    else "api_error"
                )
                raise CloudAPIError(
                    kind,
                    f"API HTTP {status}, code={code}"
                    + ("；请缩小导入范围" if kind == "limit" else ""),
                )
            return payload
        raise AssertionError("unreachable")

    def access_token(self):
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            payload = self._call(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                body={"app_id": self.app_id, "app_secret": self._secret},
                auth=False,
            )
            token = payload.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise CloudAPIError("auth", "认证响应缺少 access token")
            self._token = token
            self._expires = time.monotonic() + max(
                0, int(payload.get("expire", 7200)) - 300
            )
            return token

    def get(self, path, **params):
        data = self._call("GET", path, params=params).get("data")
        if not isinstance(data, dict):
            raise CloudAPIError("invalid_response", "响应缺少 data")
        return data

    def pages(self, path, *, key="items", params=None, body=None):
        items, seen, cursor = [], set(), None
        for _ in range(self.max_pages):
            query = dict(params or {})
            if cursor:
                query["page_token"] = cursor
            payload = self._call(
                "POST" if body is not None else "GET", path, params=query, body=body
            )
            data = payload.get("data", {})
            page = data.get(key)
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                raise CloudAPIError("invalid_response", "分页响应缺少完整数据列表")
            items.extend(page)
            if len(items) > self.max_items:
                raise CloudAPIError("limit", "分页记录超限，请缩小字段/过滤条件/范围")
            if data.get("has_more") is False:
                return items
            if data.get("has_more") is not True:
                raise CloudAPIError("invalid_response", "分页完整性标记缺失")
            cursor = data.get("page_token")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise CloudAPIError("pagination", "分页游标缺失或重复；未发布新快照")
            seen.add(cursor)
        raise CloudAPIError("limit", "分页页数超限，请缩小导入范围")
