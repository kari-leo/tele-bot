from dataclasses import replace
import importlib
import json
import unittest
from unittest.mock import patch

from tele_bot.bitable.client import BitableClient
from tele_bot.bitable.models import BitableError
from tele_bot.config.bitable import BitableSettings


class SDKContractTests(unittest.TestCase):
    def setUp(self):
        self.settings = BitableSettings(app_id="test-app", app_secret="do-not-log", timeout=1)
        self.client = BitableClient(self.settings, sleep=lambda seconds: None)

    @staticmethod
    def response(data=None, code=0, status=200, headers=None):
        from lark_oapi.core.model import RawResponse
        result = RawResponse()
        result.content = json.dumps({"code": code, "data": data or {}}).encode()
        result.status_code = status
        result.headers = headers or {}
        return result

    def test_real_sdk_wire_clear_false_zero_and_idempotency_query(self):
        from lark_oapi.core.json import JSON
        responses = [self.response({"records": [{"record_id": "rec1"}]}),
                     self.response({"records": [{"record_id": "rec1"}]})]
        captured = []

        def transport(config, request, option):
            captured.append(request)
            return responses.pop(0)

        with patch("lark_oapi.api.bitable.v1.resource.app_table_record.verify"), \
             patch("lark_oapi.core.http.Transport.execute", side_effect=transport):
            self.client.call("create", "base1", "tbl1", client_token="persisted-uuid",
                             body={"records": [{"fields": {"text": "hello", "number": 0, "check": False}}]})
            self.client.call("update", "base1", "tbl1",
                             body={"records": [{"record_id": "rec1", "fields": {"text": None}}]})
        self.assertEqual(dict(captured[0].queries)["client_token"], "persisted-uuid")
        self.assertEqual(json.loads(JSON.marshal(captured[0].body))["records"][0]["fields"]["number"], 0)
        self.assertIsNone(json.loads(JSON.marshal(captured[1].body))["records"][0]["fields"]["text"])
        self.assertIn("batch_update", captured[1].uri)

    def test_sdk_rate_limit_read_retry_write_no_retry_and_redaction(self):
        rate = self.response(code=1254290, headers={"Retry-After": "0", "X-Tt-Logid": "safe-log-id"})
        ok = self.response({"items": [], "has_more": False})
        with patch("lark_oapi.api.bitable.v1.resource.app_table_record.verify"), \
             patch("lark_oapi.core.http.Transport.execute", side_effect=[rate, ok]) as transport:
            self.assertEqual(self.client.call("search", "base1", "tbl1", body={}, size=1)["items"], [])
            self.assertEqual(transport.call_count, 2)
        with patch("lark_oapi.api.bitable.v1.resource.app_table_record.verify"), \
             patch("lark_oapi.core.http.Transport.execute", side_effect=RuntimeError("do-not-log")) as transport:
            with self.assertRaises(BitableError) as error:
                self.client.call("create", "base1", "tbl1", body={"records": []})
            self.assertNotIn("do-not-log", str(error.exception))
            self.assertEqual(transport.call_count, 1)

    def test_sdk_byte_budget_and_missing_dependency_are_local_errors(self):
        oversized = self.response({"items": [{"record_id": "rec1", "fields": {"text": "x" * 1000}}]})
        self.client.settings = replace(self.settings, max_response_bytes=100)
        with patch("lark_oapi.api.bitable.v1.resource.app_table_record.verify"), \
             patch("lark_oapi.core.http.Transport.execute", return_value=oversized):
            with self.assertRaises(BitableError) as error:
                self.client.call("search", "base1", "tbl1", body={})
            self.assertEqual(error.exception.kind, "limit")
        with patch.object(importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertRaises(BitableError) as error:
                self.client.call("search", "base1", "tbl1", body={})
            self.assertEqual(error.exception.kind, "configuration")

    def test_request_and_total_time_budget_stop_before_network(self):
        self.client.settings = replace(self.settings, max_requests=0)
        with self.client.scope(), patch.object(self.client, "_request") as request:
            with self.assertRaises(BitableError):
                self.client.call("search", "base1", "tbl1", body={})
            request.assert_not_called()

    def test_real_sdk_reads_base_identity_tables_and_document_permissions_without_writes(self):
        responses = [self.response({"app": {"app_token": "base1", "name": "远端名称", "url": "https://tenant.feishu.cn/base/base1"}}),
                     self.response({"items": [{"table_id": "tbl1", "name": "远端表名"}], "has_more": False}),
                     self.response({"auth_result": True}), self.response({"auth_result": False})]
        captured = []

        def transport(config, request, option):
            captured.append(request)
            return responses.pop(0)

        with patch("lark_oapi.api.bitable.v1.resource.app.verify"), \
             patch("lark_oapi.api.bitable.v1.resource.app_table.verify"), \
             patch("lark_oapi.api.drive.v1.resource.permission_member.verify"), \
             patch("lark_oapi.core.http.Transport.execute", side_effect=transport):
            with self.client.scope():
                result = self.client.describe_base("base1")
        self.assertEqual(result["base_name"], "远端名称")
        self.assertEqual(result["tables"], {"tbl1": "远端表名"})
        self.assertEqual(result["permissions"], {"view": True, "edit": False})
        self.assertFalse(result["errors"])
        self.assertEqual(dict(captured[2].queries), {"type": "bitable", "action": "view"})
        self.assertEqual(dict(captured[3].queries)["action"], "edit")

    def test_missing_permission_scopes_are_unknown_instead_of_denied_or_fabricated(self):
        def call(action, app_token, **kwargs):
            if action == "tables":
                return {"items": [{"table_id": "tbl1", "name": "已读取表名"}], "has_more": False}
            raise BitableError("unauthorized", "缺少查询 scope", code=99991672)

        with patch.object(self.client, "call", side_effect=call):
            result = self.client.describe_base("base1")
        self.assertIsNone(result["base_name"])
        self.assertEqual(result["permissions"], {"view": None, "edit": None})
        self.assertEqual(result["errors"]["edit"]["code"], 99991672)
        self.assertEqual(result["tables"]["tbl1"], "已读取表名")
