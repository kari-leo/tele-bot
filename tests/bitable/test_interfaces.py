import json
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.bitable.bootstrap import build_bitable_service
from tele_bot.bitable.models import CURRENT_BITABLE_MESSAGE
from tele_bot.bitable.policy import BitablePolicy
from tele_bot.config.bitable import BitableSettings
from tele_bot.models import OutgoingMessage, CardAction
from tele_bot.service import MessageService
from tele_bot.tools.bitable import build_bitable_tools
from tele_bot.tools.lc_adapters import build_core_tools
from tele_bot.workflows.react_graph import build_react_graph
from tests.bitable.support import ServiceCase


class BaseInterfaceTests(ServiceCase):
    def test_default_registration_survives_missing_config_without_eager_sdk(self):
        settings = BitableSettings(targets_file=Path(self.tmp.name) / "missing.json")
        with patch("tele_bot.bitable.bootstrap.BitableSettings.from_env", return_value=settings), \
             patch("tele_bot.bitable.client.importlib.import_module") as importer:
            service = build_bitable_service()
            self.assertIsNotNone(service)
            self.assertFalse(service.configuration_error)
            tools = build_core_tools()
            names = {tool.name for tool in tools}
            self.assertEqual(len([n for n in names if n.startswith("bitable_")]), 5)
            token = CURRENT_BITABLE_MESSAGE.set(self.message)
            try:
                inspect = next(tool for tool in tools if tool.name == "bitable_inspect")
                self.assertEqual(json.loads(inspect.invoke({"target": "项目"}))["error"], "not_found")
            finally:
                CURRENT_BITABLE_MESSAGE.reset(token)
            importer.assert_not_called()
        names = {tool.name for tool in build_core_tools(bitable_service=self.service)}
        self.assertEqual(len([n for n in names if n.startswith("bitable_")]), 5)

    def test_tools_schema_and_forged_identity_fail_closed(self):
        tools = {tool.name: tool for tool in build_bitable_tools(self.service)}
        for tool in tools.values():
            schema = tool.args_schema.model_json_schema()["properties"]
            self.assertFalse({"user_id", "chat_id", "confirmed", "token"} & set(schema))
        result = json.loads(tools["bitable_query"].invoke({"target": "项目"}))
        self.assertEqual(result["error"], "unauthorized")
        self.assertFalse(self.client.calls)

    def test_message_boundary_receipts_card_and_real_confirmation(self):
        tools = {tool.name: tool for tool in build_bitable_tools(self.service)}

        class Agent:
            def handle_message(agent, message):
                tools["bitable_delete_records"].invoke({"target": "项目", "record_ids": ["rec1"]})
                return OutgoingMessage(message.channel, message.chat_id, "模型误称已经删除")

        adapter = type("Adapter", (), {"name": "feishu"})()
        service = MessageService(Agent(), adapter, bitable_service=self.service)
        output = service.handle(self.message)
        self.assertIsNotNone(output.card)
        self.assertNotIn("误称", output.text)
        self.assertFalse(self.client.writes)
        token = output.card.actions[0].value
        confirmed = service.handle(self.message_for(card_action=CardAction("bitable_confirm", "确认", token)))
        self.assertIn("已删除 1 条记录", confirmed.text)
        self.assertNotIn("rec1", self.client.rows)
        self.assertIsNone(CURRENT_BITABLE_MESSAGE.get())

    def test_untrusted_message_cannot_execute_tools_or_confirmation(self):
        result = self.service.write("delete", "项目", self.message, ["rec1"])
        calls = []

        class Agent:
            def handle_message(agent, message):
                calls.append(CURRENT_BITABLE_MESSAGE.get())
                return OutgoingMessage(message.channel, message.chat_id, "正常聊天")

        service = MessageService(Agent(), type("Adapter", (), {"name": "feishu"})(), bitable_service=self.service)
        service.handle(self.message_for(text="/bitable confirm " + result["confirmation_token"]), allow_bitable=False)
        self.assertEqual(calls, [None])
        self.assertFalse(self.client.writes)

    def test_real_react_tool_graph_creates_and_failure_does_not_kill_chat(self):
        tools = build_bitable_tools(self.service)

        class Model:
            def __init__(model):
                model.calls = 0

            def invoke(model, messages, config=None):
                model.calls += 1
                if model.calls == 1:
                    return AIMessage(content="", tool_calls=[{"name": "bitable_create_records", "id": "create",
                        "args": {"target": "项目", "records": [{"fields": {"名称": "C"}}]}}])
                return AIMessage(content="已新增")

        graph = build_react_graph(Model(), tools)
        executor = ReactAgentExecutor(graph=graph, model_name="fixture", bitable_service=self.service)
        service = MessageService(AgentCore(executor=executor), type("Adapter", (), {"name": "feishu"})(),
                                 bitable_service=self.service)
        output = service.handle(self.message)
        self.assertIn("已新增 1 条记录", output.text)
        self.assertEqual(len(self.client.writes), 1)

    def test_registration_request_with_unmatched_url_table_returns_discovery_choices_through_graph(self):
        self.service.policy = BitablePolicy([])

        class Model:
            def invoke(model, messages, config=None):
                results = [m for m in messages if isinstance(m, ToolMessage)]
                if not results:
                    return AIMessage(content="", tool_calls=[{"name": "bitable_inspect", "id": "discover",
                        "args": {"target": "https://tenant.feishu.cn/base/base1?table=blkgMgG9ij8mnFqY"}}])
                result = json.loads(results[-1].content)
                if result.get("discovered") and result.get("status") == "succeeded":
                    return AIMessage(content="已记住 " + result["base_name"] + "，请选择：" +
                                     "、".join(t["table_name"] for t in result["tables"]))
                return AIMessage(content="无法登记，请联系管理员在后台配置")

        graph = build_react_graph(Model(), build_bitable_tools(self.service))
        executor = ReactAgentExecutor(graph=graph, model_name="fixture", bitable_service=self.service)
        service = MessageService(AgentCore(executor=executor), type("Adapter", (), {"name": "feishu"})(),
                                 bitable_service=self.service)
        result = service.handle(self.message_for(text="https://tenant.feishu.cn/base/base1?table=blkgMgG9ij8mnFqY登记一下这个多维表格"))
        self.assertIn("已记住 内容管理", result.text)
        self.assertIn("选题", result.text)
        self.assertNotIn("管理员", result.text)
        self.assertFalse(self.client.writes)
        self.assertEqual(self.client.calls, ["describe"])

    def test_config_paths_partial_credentials_and_invalid_targets_fail_locally(self):
        with patch("tele_bot.config.bitable.FeishuSettings._load_env_file", return_value={}), \
             patch.dict("os.environ", {"FEISHU_APP_ID": "a",
                                     "FEISHU_APP_SECRET": "s", "TELE_BOT_RUNTIME_ROOT": self.tmp.name}, clear=True):
            settings = BitableSettings.from_env()
            self.assertEqual(settings.sqlite_path, Path(self.tmp.name) / "data/bitable.sqlite")
            self.assertEqual(settings.app_id, "a")
            self.assertFalse(hasattr(settings, "enabled"))
            with patch.dict("os.environ", {"FEISHU_BITABLE_ENABLED": "false"}):
                self.assertEqual(BitableSettings.from_env().app_id, "a")
            self.assertNotIn("app_secret", repr(settings))
            with patch.dict("os.environ", {"FEISHU_BITABLE_APP_ID": "partial"}):
                service = build_bitable_service()
                self.assertTrue(service.configuration_error)

    def test_checkpoint_identity_changes_with_bitable_auth_scope(self):
        configs = []

        class Graph:
            def invoke(graph, state, config):
                configs.append(config["configurable"]["thread_id"])
                return {"messages": state["messages"] + [AIMessage(content="ok")]}

        executor = ReactAgentExecutor(graph=Graph(), model_name="test", bitable_service=self.service)
        for message in [self.message, self.message_for(user_id="other")]:
            token = CURRENT_BITABLE_MESSAGE.set(message)
            try:
                executor.handle(message)
            finally:
                CURRENT_BITABLE_MESSAGE.reset(token)
        executor.handle(self.message)
        self.assertEqual(len(set(configs)), 3)
