"""Scheduling flows through the MCP tool bound to a trusted chat message."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from mcp import Client
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.mcp.schedule import create_schedule_server
from tele_bot.models import IncomingMessage
from tele_bot.scheduled.service import ScheduleService
from tele_bot.scheduled.store import ScheduleStore, iso, now_utc
from tele_bot.scheduled.context import scheduling_context
from tele_bot.workflows.context_manager import text_tokens
from tele_bot.tools.workspace_policy import WorkspacePolicy
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
        self.assertEqual(set(schedule_tool.tool_call_schema.model_json_schema()["properties"]), {"request"})
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

    def test_delete_followup_receives_history_and_stops_repeated_calls(self):
        task_id = self.store.create_many("alice", "feishu", "chat", [{
            "content": "更新机器人产品经理岗位", "at": iso(self.future),
            "rule": {"kind": "once", "interval": 1, "weekdays": [], "count": None, "until": None},
            "mode": "remind", "scope": {"tools": [], "directories": []},
            "missed_policy": "skip", "grace_seconds": 300, "auth_seconds": 900,
        }])[0]
        parsed = []

        class Parser:
            def parse(inner, text):
                parsed.append(text)
                if task_id in text and "删除这个任务" in text:
                    return {"operation": "delete", "task_id": task_id}
                return {"operation": "clarify", "question": "请提供任务 ID。"}

        self.service.parser = Parser()
        schedule_tool = next(t for t in build_core_tools(schedule_service=self.service)
                             if t.name == "schedule_manage")

        class Model:
            calls = 0

            def invoke(inner, messages, config=None):
                inner.calls += 1
                # Reproduce the actual model retrying the same empty call.
                return AIMessage(content="", tool_calls=[{
                    "name": "schedule_manage", "args": {}, "id": f"call-{inner.calls}"}])

        model = Model()
        graph = build_react_graph(model, [schedule_tool])
        config = {"configurable": {"thread_id": "chat"}}
        graph.update_state(config, {"messages": [
            HumanMessage(content="现在有哪些定时任务？"),
            AIMessage(content="", tool_calls=[{"name": "schedule_manage", "args": {}, "id": "list"}]),
            ToolMessage(name="schedule_manage", tool_call_id="list", content=json.dumps({
                "handled": True, "text": f"当前活跃任务：\n{task_id} [active] 更新机器人产品经理岗位"})),
            AIMessage(content=f"当前有一个活跃任务：{task_id}"),
            HumanMessage(content="删除这个任务"),
            AIMessage(content="请提供任务 ID。"),
        ]})
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            result = ReactAgentExecutor(graph=graph, model_name="fake").handle(
                IncomingMessage("feishu", "alice", "chat", "前面提到的这个任务"))
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed)
        self.assertIn("待删除任务", result.reply_text)
        self.assertIn(task_id, result.reply_text)
        self.assertIn(task_id, parsed[0])
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.store.get(task_id, "alice")["status"], "active")
        confirmation = result.reply_text.rsplit("确认 ", 1)[1]
        confirmed = self.service.handle(IncomingMessage("feishu", "alice", "chat", f"确认 {confirmation}"))
        self.assertIn("已取消后续调度", confirmed.text)

    def test_structured_proposal_uses_same_turn_tool_result(self):
        @tool
        def read_requirements() -> str:
            """Read the requested job direction."""
            return "岗位方向：机器人/具身智能产品经理；输出新增和修改表格。"

        schedule_tool = next(t for t in build_core_tools(schedule_service=self.service)
                             if t.name == "schedule_manage")
        seen = []

        class Model:
            def invoke(inner, messages, config=None):
                seen.append(messages)
                if not any(isinstance(m, ToolMessage) and m.name == "read_requirements" for m in messages):
                    return AIMessage(content="", tool_calls=[{
                        "name": "read_requirements", "args": {}, "id": "read"}])
                return AIMessage(content="", tool_calls=[{
                    "name": "schedule_manage", "id": "schedule", "args": {"request": {
                        "operation": "create", "tasks": [{
                            "content": "提醒更新机器人/具身智能产品经理岗位，输出新增和修改表格",
                            "at": iso(self.future), "mode": "remind",
                            "rule": {"kind": "once"}, "scope": {"tools": [], "directories": []}}]}}}])

        graph = build_react_graph(Model(), [read_requirements, schedule_tool])
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            result = ReactAgentExecutor(graph=graph, model_name="fake").handle(
                IncomingMessage("feishu", "alice", "chat", "先读岗位要求，明天按要求提醒我"))
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed)
        self.assertIn("待创建", result.reply_text)
        self.assertIn("机器人/具身智能产品经理", result.reply_text)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.store.list_tasks("alice"), [])

    def test_model_cannot_supply_confirmation_or_another_owner(self):
        async def call():
            message = IncomingMessage("feishu", "alice", "chat", "删除这个任务")
            async with Client(create_schedule_server(self.service, message)) as client:
                result = await client.call_tool("schedule_manage", {"request": {
                    "operation": "authorize", "user_id": "bob", "task_id": "fake"}})
                return result

        result = asyncio.run(call())
        self.assertFalse(result.is_error)
        self.assertIn("不支持", json.loads(result.content[0].text)["text"])

    def test_structured_request_cannot_use_another_users_task(self):
        proposed = self.service.handle(IncomingMessage(
            "feishu", "alice", "chat", "明天提醒我检查报告"))
        token = proposed.text.split("确认 ")[1].splitlines()[0]
        created = self.service.handle(IncomingMessage("feishu", "alice", "chat", f"确认 {token}"))
        task_id = created.text.rsplit("：", 1)[1]
        result = self.service.handle(IncomingMessage("feishu", "bob", "chat", "删除这个任务"),
                                     request={"operation": "delete", "task_id": task_id, "user_id": "alice"})
        self.assertIn("找不到属于你的", result.text)
        self.assertEqual(self.store.get(task_id, "alice")["status"], "active")

    def test_no_argument_tool_receives_same_turn_results(self):
        parsed = []

        @tool
        def read_requirements() -> str:
            """Read the requested task requirements."""
            return "范围：机器人/具身智能产品经理；输出新增和修改表格。"

        class Parser:
            def parse(inner, text):
                parsed.append(text)
                return {"operation": "clarify", "question": "请补充触发时间。"}

        self.service.parser = Parser()
        schedule_tool = next(t for t in build_core_tools(schedule_service=self.service)
                             if t.name == "schedule_manage")

        class Model:
            calls = 0

            def invoke(inner, messages, config=None):
                inner.calls += 1
                name = "read_requirements" if inner.calls == 1 else "schedule_manage"
                return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": str(inner.calls)}])

        graph = build_react_graph(Model(), [read_requirements, schedule_tool])
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            ReactAgentExecutor(graph=graph, model_name="fake").handle(
                IncomingMessage("feishu", "alice", "chat", "先读要求，再创建定时任务"))
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed)
        self.assertEqual(len(parsed), 1)
        self.assertIn("机器人/具身智能产品经理", parsed[0])
        self.assertIn('"role": "tool"', parsed[0])

    def test_context_keeps_summary_under_large_same_turn_results(self):
        context = [HumanMessage(content="Earlier context (reference only):\n"
                                "用户要求删除任务 1b59197868ba，尚未完成。")]
        context.extend(ToolMessage(name="read", tool_call_id=str(i), content="大量结果" * 3000)
                       for i in range(20))
        context.append(HumanMessage(content="前面提到的这个任务"))
        reference = scheduling_context(context)
        self.assertLessEqual(text_tokens(reference), 8000)
        self.assertIn("1b59197868ba", reference)
        self.assertIn("前面提到的这个任务", reference)

    def test_ambiguous_reference_returns_clarification_without_proposal(self):
        seen = []

        class Parser:
            def parse(inner, text):
                seen.append(text)
                return {"operation": "clarify", "question": "有两个任务，请说明要删除哪一个。"}

        self.service.parser = Parser()
        result = self.service.handle(IncomingMessage("feishu", "alice", "chat", "删除这个任务"),
                                     context_messages=[AIMessage(content="任务 a：报告；任务 b：岗位更新。")])
        self.assertIn("有两个任务", result.text)
        self.assertIn("任务 a", seen[0])
        self.assertEqual(self.store.pending_for_chat("alice", "feishu", "chat")[0], [])

    def test_log_create_with_top_level_content_falls_back_to_original_request(self):
        workspace = Path(self.temp.name) / "job_workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("job workspace", encoding="utf-8")
        self.service.policy = WorkspacePolicy((Path(self.temp.name),), workspace,
                                              Path(self.temp.name) / "quarantine")
        original = ("今天凌晨三点半在job_workspace目录下用codex调用里面的autumn-recruiment skill，"
                    "更新机器人/具身智能产品经理 相关的岗位，将新增和修改的部分总结成一个表格发给我。")
        trigger = datetime(2026, 10, 7, 3, 30, tzinfo=timezone(timedelta(hours=8)))
        parsed = []

        class Parser:
            def parse(inner, text):
                parsed.append(text)
                return {"operation": "create", "tasks": [{
                    "content": "更新岗位", "at": iso(trigger), "mode": "remind",
                    "rule": {"kind": "once"}, "scope": {"tools": [], "directories": []}}]}

        self.service.parser = Parser()
        schedule_tool = next(t for t in build_core_tools(schedule_service=self.service)
                             if t.name == "schedule_manage")

        class Model:
            def invoke(inner, messages, config=None):
                # Exact shape recorded at 2026-10-07 02:09:41.
                return AIMessage(content="", tool_calls=[{"name": "schedule_manage", "id": "create", "args": {
                    "request": {"operation": "create", "content": original}}}])

        graph = build_react_graph(Model(), [schedule_tool])
        graph.update_state({"configurable": {"thread_id": "chat"}}, {"messages": [
            HumanMessage(content="今天凌晨两点半更新岗位"),
            AIMessage(content="之前的两点半任务已取消。"),
        ]})
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            with patch("tele_bot.scheduled.service.CodexRunner.from_env",
                       return_value=SimpleNamespace(default_workspace=Path(self.temp.name))), \
                    patch("tele_bot.scheduled.service.now_utc", return_value=trigger - timedelta(hours=1)):
                result = ReactAgentExecutor(graph=graph, model_name="fake").handle(
                    IncomingMessage("feishu", "alice", "chat", original))
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed)
        self.assertIn("待创建", result.reply_text)
        self.assertIn("确认预授权", result.reply_text)
        self.assertIn("2026-10-07 03:30", result.reply_text)
        self.assertIn(original, result.reply_text)
        self.assertEqual(len(parsed), 1)
        self.assertTrue(parsed[0].endswith(original))
        self.assertIn("两点半", parsed[0])
        self.assertEqual(self.store.list_tasks("alice"), [])
        self.assertEqual(len(self.store.pending_for_chat("alice", "feishu", "chat")[0]), 1)

    def test_incomplete_task_parameters_reparse_trusted_text_once(self):
        parsed = []

        class Parser:
            def parse(inner, text):
                parsed.append(text)
                return {"operation": "clarify", "question": "请说明要提醒的事项。"}

        self.service.parser = Parser()
        original = "明天提醒我处理前面说的事情"
        requests = [
            {},
            {"content": "伪造的新任务"},
            {"operation": ""},
            {"operation": "create", "content": "伪造的新任务"},
            {"operation": "create", "tasks": []},
            {"operation": "create", "tasks": [{"content": "事项"}]},
            {"operation": "create", "tasks": [{"at": iso(self.future)}]},
            {"operation": "update", "task_id": "abc", "tasks": []},
            {"operation": "delete"},
        ]
        for request in requests:
            with self.subTest(request=request):
                result = self.service.handle(IncomingMessage("feishu", "alice", "chat", original),
                                             request=request)
                self.assertEqual(result.text, "请说明要提醒的事项。")
                self.assertEqual(parsed[-1], original)
        self.assertEqual(len(parsed), len(requests))
        self.assertEqual(self.store.pending_for_chat("alice", "feishu", "chat")[0], [])

    def test_tool_schema_describes_nested_task_content_and_time(self):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        schedule_tool = next(t for t in build_core_tools(schedule_service=self.service)
                             if t.name == "schedule_manage")
        schema = convert_to_openai_tool(schedule_tool)["function"]["parameters"]
        request = next(item for item in schema["properties"]["request"]["anyOf"]
                       if item.get("type") == "object")
        self.assertIn("tasks", request["properties"])
        item = request["properties"]["tasks"]["items"]
        self.assertIn("content", item["properties"])
        self.assertIn("at", item["properties"])
        self.assertNotIn("context_messages", schema["properties"])


if __name__ == "__main__":
    unittest.main()
