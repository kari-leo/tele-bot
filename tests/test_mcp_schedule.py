"""Scheduling flows through the MCP tool bound to a trusted chat message."""

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from mcp import Client
from langchain_core.messages import AIMessage, ToolMessage

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.mcp.schedule import create_schedule_server
from tele_bot.models import IncomingMessage
from tele_bot.scheduled.service import ScheduleService
from tele_bot.scheduled.store import ScheduleStore, iso, now_utc
from tele_bot.tools.lc_adapters import (
    CURRENT_SCHEDULE_ALLOWED, CURRENT_SCHEDULE_MESSAGE, build_core_tools,
)
from tele_bot.service import MessageService
from tele_bot.skills.loader import SkillLoader
from tele_bot.workflows.react_graph import build_react_graph


class ScheduleMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScheduleStore(Path(self.temp.name) / "schedule.sqlite")
        self.future = now_utc() + timedelta(hours=1)

        class Parser:
            def parse(inner, text):
                return {"operation": "create", "tasks": [{
                    "content": "提醒检查报告", "at": iso(self.future),
                    "rule": {"kind": "once"}, "mode": "remind",
                    "scope": {"tools": [], "directories": []},
                }]}

        self.service = ScheduleService(self.store, Parser())

    def test_official_mcp_client_proposes_then_confirms(self):
        async def call(message):
            async with Client(create_schedule_server(self.service, message)) as client:
                listed = await client.list_tools()
                self.assertEqual([tool.name for tool in listed.tools], ["schedule_manage"])
                result = await client.call_tool("schedule_manage", {})
                self.assertFalse(result.is_error)
                return json.loads(result.content[0].text)

        original = IncomingMessage("feishu", "alice", "chat", "明天上午九点提醒我检查报告")
        proposal = asyncio.run(call(original))
        self.assertIn("待创建", proposal["text"])
        self.assertEqual(self.store.list_tasks("alice"), [])
        token = proposal["text"].split("确认 ")[1].splitlines()[0]
        confirmation = asyncio.run(call(IncomingMessage("feishu", "alice", "chat", f"确认 {token}")))
        self.assertIn("已创建任务", confirmation["text"])
        self.assertEqual(len(self.store.list_tasks("alice")), 1)

    def test_agent_tool_invokes_mcp_with_trusted_message_and_blocks_api(self):
        schedule_tool = next(tool for tool in build_core_tools(schedule_service=self.service)
                             if tool.name == "schedule_manage")
        self.assertEqual(schedule_tool.tool_call_schema.model_json_schema()["properties"], {})
        message_token = CURRENT_SCHEDULE_MESSAGE.set(IncomingMessage(
            "feishu", "alice", "chat", "明天上午九点提醒我检查报告"
        ))
        allowed_token = CURRENT_SCHEDULE_ALLOWED.set(False)
        try:
            denied = json.loads(schedule_tool.invoke({}))
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed_token)
        self.assertIn("未校验调用者身份", denied["text"])
        self.assertEqual(self.store.pending_for_chat("alice", "feishu", "chat")[0], [])

        allowed_token = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            proposed = json.loads(schedule_tool.invoke({}))
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed_token)
            CURRENT_SCHEDULE_MESSAGE.reset(message_token)
        self.assertIn("待创建", proposed["text"])
        self.assertEqual(self.store.list_tasks("alice"), [])

    def test_react_agent_calls_mcp_and_keeps_confirmation_text(self):
        schedule_tool = next(tool for tool in build_core_tools(schedule_service=self.service)
                             if tool.name == "schedule_manage")

        class FakeLLM:
            config = None

            def invoke(self, messages, config=None):
                self.config = config
                if any(isinstance(message, ToolMessage) and message.name == "schedule_manage"
                       for message in messages):
                    return AIMessage(content="抱歉，我无法设置定时任务")
                return AIMessage(content="", tool_calls=[
                    {"name": "schedule_manage", "args": {}, "id": "call-1"}
                ])

        class Adapter:
            name = "feishu"

        llm = FakeLLM()
        graph = build_react_graph(llm, [schedule_tool])
        agent = AgentCore(executor=ReactAgentExecutor(graph=graph, model_name="fake"))
        response = MessageService(agent_core=agent, feishu_adapter=Adapter()).handle(
            IncomingMessage("feishu", "alice", "chat", "明天上午九点提醒我检查报告")
        )
        self.assertIn("待创建", response.text)
        self.assertIn("确认 ", response.text)
        self.assertNotIn("无法设置", response.text)
        self.assertTrue(llm.config["configurable"]["schedule_allowed"])
        self.assertIn("schedule_manage", SkillLoader().build_system_prompt())


if __name__ == "__main__":
    unittest.main()
