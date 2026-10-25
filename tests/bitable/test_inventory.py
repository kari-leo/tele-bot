import json
from dataclasses import replace

from langchain_core.messages import AIMessage, ToolMessage

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.bitable.models import BitableError, CURRENT_BITABLE_MESSAGE
from tele_bot.bitable.policy import BitablePolicy
from tele_bot.bitable.service import BitableService
from tele_bot.router.bitable_service import render_bitable_receipt
from tele_bot.service import MessageService
from tele_bot.tools.bitable import build_bitable_tools
from tele_bot.workflows.react_graph import build_react_graph
from tests.bitable.support import ENTRIES, ServiceCase


class BaseInventoryTests(ServiceCase):
    def configure(self, entries):
        self.service.policy = BitablePolicy(entries)

    def test_inventory_uses_remote_names_permissions_and_is_open_to_all_callers(self):
        entries = [dict(ENTRIES[0], base_name="内容管理", table_name="选题"),
                   dict(ENTRIES[0], alias="只读表", table_id="tbl2", operations=["read"]),
                   dict(ENTRIES[0], alias="编辑表", table_id="tbl3", operations=["read", "update"]),
                   dict(ENTRIES[0], alias="其他人", users=["other"]),
                   dict(ENTRIES[0], alias="其他会话", chats=["other"]),
                   dict(ENTRIES[0], alias="停用表", operations=[])]
        self.configure(entries)
        result = self.service.inspect("", self.message)
        self.assertEqual(result["permission_source"], "feishu_api")
        targets = result["targets"]
        self.assertEqual(result["total"], 6)
        self.assertEqual([t["alias"] for t in targets], ["项目", "只读表", "编辑表", "其他人", "其他会话"])
        self.assertEqual((targets[0]["base_name"], targets[0]["table_name"]), ("内容管理", "选题"))
        self.assertEqual(targets[0]["permission_label"], "可阅读、可编辑")
        self.assertEqual(targets[1]["permission_level"], "read_write")
        self.assertEqual(targets[1]["base_name"], "内容管理")  # Names come from the API, not local aliases.
        self.assertEqual(targets[2]["permissions"], {"view": True, "edit": True})
        self.assertEqual(self.service.inspect("", self.message, page=2)["targets"][0]["alias"], "停用表")
        self.assertEqual(self.service.inspect("", self.message_for(user_id="anyone", chat_id="anychat")), result)
        self.assertNotIn("users", targets[0])
        self.assertTrue(self.client.calls)
        self.assertFalse(self.client.writes)
        for message in [None, self.message_for(channel="api")]:
            with self.subTest(message=message), self.assertRaises(BitableError):
                self.service.inspect("", message)

    def test_remote_permissions_never_infer_editability_from_local_open_access(self):
        for permissions, level, label in [({"view": True, "edit": False}, "read_only", "只读"),
                                          ({"view": False, "edit": False}, "no_access", "无阅读/编辑权限"),
                                          ({"view": None, "edit": None}, "unknown", "权限未核实")]:
            self.client.description["permissions"] = permissions
            result = self.service.inspect("", self.message)["targets"][0]
            self.assertEqual(result["permission_level"], level)
            self.assertEqual(result["permission_label"], label)
        self.client.description.update(base_name=None, url=None, tables={})
        result = self.service.inspect("", self.message)["targets"][0]
        self.assertEqual(result["base_name"], "名称未能读取")
        self.assertNotEqual(result["table_name"], "项目")

    def test_unique_display_names_allow_other_users_and_chats_to_query_and_update(self):
        self.configure([dict(ENTRIES[0], base_name="内容管理", table_name="选题", operations=["read"])])
        for name in ["内容管理", "选题", "项目"]:
            self.assertEqual(self.service.query(name, self.message, record_id="rec1")["records"][0]["record_id"], "rec1")
        result = self.service.write("update", "内容管理", self.message_for(user_id="anyone", chat_id="anychat"),
                                    [{"record_id": "rec1", "fields": {"数量": 5}}])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.client.rows["rec1"]["fields"]["数量"], 5)

    def test_shared_base_name_still_requires_alias(self):
        self.configure([dict(ENTRIES[0], base_name="内容管理", table_name="选题", operations=["read"]),
                        dict(ENTRIES[0], alias="渠道", base_name="内容管理", table_name="发布渠道", table_id="tbl2")])
        with self.assertRaises(BitableError) as error:
            self.service.write("create", "内容管理", self.message, [{"fields": {"名称": "C"}}])
        self.assertEqual(error.exception.kind, "validation")
        self.assertFalse(self.client.calls)
        self.assertEqual(self.service.policy.resolve("发布渠道", self.message, "create").alias, "渠道")
        with self.assertRaises(BitableError):
            self.service.policy.resolve("https://tenant.feishu.cn/base/base1", self.message, "read")

    def test_requested_chinese_add_word_and_unique_name_execute_creation(self):
        self.configure([dict(ENTRIES[0], base_name="内容管理", table_name="选题")])
        result = self.service.write("create", "内容管理", self.message_for(text="给内容管理增添一条测试选题"),
                                    [{"fields": {"名称": "新选题"}}])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.client.writes), 1)
        for text in ["不要增添测试选题", "别增添测试选题"]:
            with self.subTest(text=text), self.assertRaises(BitableError):
                self.service.write("create", "内容管理", self.message_for(text=text, message_id="denied"),
                                    [{"fields": {"名称": "不应创建"}}])
        self.assertEqual(len(self.client.writes), 1)

    def test_alias_precedence_and_legacy_user_restrictions_are_ignored(self):
        self.configure([dict(ENTRIES[0], alias="选题", base_name="内容管理", table_name="A"),
                        dict(ENTRIES[0], alias="另表", table_name="选题", table_id="tbl2"),
                        dict(ENTRIES[0], alias="隐私表", base_name="保密", users=["other"])])
        self.assertEqual(self.service.policy.resolve("选题", self.message, "read").table_id, "tbl1")
        for name in ["保密", "隐私表"]:
            self.assertEqual(self.service.policy.resolve(name, self.message, "read").alias, "隐私表")
        with self.assertRaises(BitableError) as error:
            self.service.policy.resolve("不存在的项目表", self.message, "read")
        self.assertEqual(error.exception.kind, "not_found")

    def test_display_name_validation_and_auth_version_changes(self):
        for value in [None, "", " ", 123, "x" * 101, "https://example.com"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                BitablePolicy([dict(ENTRIES[0], base_name=value)])
        original = self.service.version
        self.configure([dict(ENTRIES[0], users=[], chats=[], operations=[])])
        self.assertEqual(self.service.version, original)
        for op in ["read", "create", "update", "delete"]:
            self.assertEqual(self.service.policy.resolve("项目", self.message, op).alias, "项目")
        self.configure([dict(ENTRIES[0], base_name="新名称")])
        self.assertNotEqual(self.service.version, original)

    def test_list_command_is_open_paginated_and_does_not_need_agent(self):
        entries = [dict(ENTRIES[0], alias=f"项目{i}", base_name="内容管理", table_name=f"表{i}") for i in range(7)]
        entries.append(dict(ENTRIES[0], alias="隐私表", users=["other"]))
        self.configure(entries)

        class Agent:
            def handle_message(agent, message):
                raise AssertionError("List commands must not call the model")

        service = MessageService(Agent(), type("Adapter", (), {"name": "feishu"})(), bitable_service=self.service)
        first = service.handle(self.message_for(text="/bitable list"))
        second = service.handle(self.message_for(text="/bitable list 2"))
        self.assertIn("1. 内容管理 / 选题", first.text)
        self.assertIn("可阅读、可编辑", first.text)
        self.assertIn("第 1/2 页", first.text)
        self.assertNotIn("表5", first.text)
        self.assertIn("6. 内容管理 / 选题", second.text)
        self.assertIn("隐私表", second.text)
        self.assertEqual(first.text, service.handle(self.message_for(text="/bitable list", user_id="other2", chat_id="otherchat")).text)
        for text in ["/bitable list -1", "/bitable list a"]:
            self.assertIn("用法", service.handle(self.message_for(text=text)).text)
        for text in ["/bitable list 0", "/bitable list 3"]:
            self.assertIn("页码无效", service.handle(self.message_for(text=text)).text)
        first_page = self.service.inspect("", self.message)
        self.assertEqual(first_page["total"], 8)
        self.assertEqual(first_page["next_page"], 2)
        self.assertEqual(len(first_page["targets"]), 5)
        self.assertIsNone(self.service.inspect("", self.message, page=2)["next_page"])
        self.configure([])
        self.service.settings = replace(self.settings, app_id="empty-app")
        self.assertIn("尚未发现", service.handle(self.message_for(text="/bitable list")).text)
        self.assertTrue(self.client.calls)
        self.assertFalse(self.client.writes)

    def test_base_link_discovers_names_tables_and_persists_without_manual_registry(self):
        self.configure([])
        url = "https://tenant.feishu.cn/base/base1"
        overview = self.service.inspect(url, self.message)
        self.assertEqual(overview["coverage"], "specified_base_only")
        self.assertEqual(len(overview["tables"]), 3)
        self.assertEqual(overview["base_name"], "内容管理")
        restarted = BitableService(self.settings, self.client, self.service.policy, self.store)
        inventory = restarted.inspect("", self.message_for(user_id="any", chat_id="any"))
        self.assertEqual(inventory["coverage"], "known_resources_only")
        self.assertEqual(inventory["total"], 3)
        self.assertEqual(restarted.query("选题", self.message, record_id="rec1")["records"][0]["record_id"], "rec1")
        result = restarted.write("update", url + "?table=tbl1", self.message_for(user_id="other", chat_id="other"),
                                 [{"record_id": "rec1", "fields": {"数量": 9}}])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.client.rows["rec1"]["fields"]["数量"], 9)
        isolated = BitableService(replace(self.settings, app_id="different-app"), self.client, self.service.policy, self.store)
        self.assertEqual(isolated.inspect("", self.message)["targets"], [])

    def test_link_discovery_failure_and_untrusted_entry_do_not_cache_or_write(self):
        self.configure([])
        for url in ["https://evil.example/base/base1", "https://tenant.feishu.cn/wiki/base1"]:
            with self.subTest(url=url), self.assertRaises(BitableError):
                self.service.inspect(url, self.message)
        with self.assertRaises(BitableError):
            self.service.inspect("https://tenant.feishu.cn/base/base1", None)
        self.assertFalse(self.client.calls)
        self.client.description["errors"]["tables"] = {"error": "unauthorized", "message": "飞书拒绝读取", "code": 91403}
        with self.assertRaises(BitableError):
            self.service.inspect("https://tenant.feishu.cn/base/base1", self.message)
        self.assertEqual(self.store.discovered_targets(self.service._namespace()), [])
        self.assertFalse(self.client.writes)

    def test_register_link_with_unmatched_table_parameter_discovers_base_and_offers_real_tables(self):
        self.configure([])
        result = self.service.inspect("https://tenant.feishu.cn/base/base1?table=blkgMgG9ij8mnFqY", self.message)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["discovered"])
        self.assertFalse(result["requested_table_found"])
        self.assertEqual(result["requested_table_id"], "blkgMgG9ij8mnFqY")
        self.assertEqual({t["table_id"] for t in result["tables"]}, {"tbl1", "tbl2", "tbl3"})
        self.assertNotIn("管理员", result["message"])
        self.assertEqual(len(self.store.discovered_targets(self.service._namespace())), 3)
        self.assertEqual(self.client.calls, ["describe"])
        self.assertFalse(self.client.writes)

    def test_unmatched_link_table_never_redirects_record_operations_to_another_table(self):
        self.configure([])
        url = "https://tenant.feishu.cn/base/base1?table=blkgMgG9ij8mnFqY"
        for action, payload in [("read", None), ("create", [{"fields": {"名称": "C"}}]),
                                 ("update", [{"record_id": "rec1", "fields": {"数量": 8}}]),
                                 ("delete", ["rec1"])]:
            with self.subTest(action=action), self.assertRaises(BitableError) as error:
                if action == "read":
                    self.service.query(url, self.message)
                else:
                    self.service.write(action, url, self.message, payload)
            self.assertEqual(error.exception.kind, "validation")
            self.assertIn("table 参数", str(error.exception))
        self.assertFalse(self.client.writes)
        self.assertNotIn("fields", self.client.calls)

    def test_tool_inventory_uses_authenticated_context_and_errors_have_clear_receipts(self):
        inspect = build_bitable_tools(self.service)[0]
        self.assertEqual(json.loads(inspect.invoke({}))["error"], "unauthorized")
        token = CURRENT_BITABLE_MESSAGE.set(self.message)
        try:
            result = json.loads(inspect.invoke({}))
        finally:
            CURRENT_BITABLE_MESSAGE.reset(token)
        self.assertEqual(result["targets"][0]["alias"], "项目")
        for result, label in [({"status": "failed", "error": "unauthorized"}, "无权限"),
                              ({"status": "failed", "error": "transient"}, "执行失败"),
                              ({"status": "unknown"}, "结果未确定")]:
            self.assertIn(label, render_bitable_receipt(self.message, result).text)

    def test_real_graph_inventory_then_named_live_query_and_update(self):
        self.configure([dict(ENTRIES[0], base_name="内容管理", table_name="选题")])
        observed = []

        class Model:
            def invoke(model, messages, config=None):
                outputs = [m for m in messages if isinstance(m, ToolMessage)]
                if outputs:
                    observed.append(json.loads(outputs[-1].content))
                calls = [("bitable_inspect", {}), ("bitable_inspect", {"target": "内容管理"}),
                         ("bitable_query", {"target": "项目", "record_id": "rec1"}),
                         ("bitable_update_records", {"target": "项目", "records": [
                             {"record_id": "rec1", "fields": {"数量": 5}}]})]
                if len(outputs) < len(calls):
                    name, args = calls[len(outputs)]
                    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(len(outputs))}])
                return AIMessage(content="已执行")

        graph = build_react_graph(Model(), build_bitable_tools(self.service))
        executor = ReactAgentExecutor(graph=graph, model_name="fixture", bitable_service=self.service)
        service = MessageService(AgentCore(executor=executor), type("Adapter", (), {"name": "feishu"})(),
                                 bitable_service=self.service)
        output = service.handle(self.message_for(text="现在管哪些多维表格？将内容管理中 rec1 数量修改为5"))
        self.assertIn("已修改 1 条记录", output.text)
        self.assertEqual(observed[0]["targets"][0]["base_name"], "内容管理")
        self.assertEqual(self.client.rows["rec1"]["fields"]["数量"], 5)
        self.assertEqual(len(self.client.writes), 1)
