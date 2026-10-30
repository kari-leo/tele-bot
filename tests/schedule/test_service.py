"""Persistent scheduler lifecycle, authorization and trusted-message MCP integration."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from mcp import Client

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.mcp.schedule import create_schedule_server
from tele_bot.models import IncomingMessage, OutgoingMessage
from tele_bot.scheduled.context import scheduling_context
from tele_bot.scheduled.service import ScheduleService
from tele_bot.scheduled.store import ScheduleStore, iso, next_time, now_utc
from tele_bot.scheduled.worker import ScheduleWorker
from tele_bot.service import MessageService
from tele_bot.skills.loader import SkillLoader
from tele_bot.tools.lc_adapters import (
    CURRENT_SCHEDULE_ALLOWED,
    CURRENT_SCHEDULE_MESSAGE,
    build_core_tools,
)
from tele_bot.tools.workspace_policy import WorkspacePolicy
from tele_bot.workflows.context_manager import text_tokens
from tele_bot.workflows.react_graph import build_react_graph


def spec(at, mode="remind", rule=None, scope=None):
    return {
        "content": "查询资料并生成报告" if mode != "remind" else "提醒检查报告",
        "at": iso(at),
        "rule": rule or {"kind": "once"},
        "mode": mode,
        "scope": scope or {"tools": [], "directories": []},
        "missed_policy": "skip",
        "grace_seconds": 300,
        "auth_seconds": 900,
    }


class ScheduledTests(unittest.TestCase):
    def test_codex_thread_id_is_confirmed_and_persisted_in_scope(self):
        workspace = Path(self.temp.name) / "workspace"
        workspace.mkdir()
        self.service.policy = WorkspacePolicy((Path(self.temp.name),), workspace, Path(self.temp.name) / "quarantine")
        task = spec(now_utc() + timedelta(hours=1), "auto", scope={
            "tools": ["codex_apply"], "directories": [str(workspace)],
            "codex_thread_id": "01a187a9-24cf-7e72-ad49-93b6c99817dc"})
        task["content"] = "调用 Codex 继续指定会话核对代码进度"
        original = IncomingMessage("feishu", "alice", "private", "/schedule create " + json.dumps(task), chat_type="p2p")
        response = self.service.handle(original)
        self.assertIn(task["scope"]["codex_thread_id"], response.text)
        self.assertIn("确认预授权", response.text)
        token = response.text.split("确认预授权 ")[1].splitlines()[0]
        confirmed = self.service.handle(IncomingMessage("feishu", "alice", "private", f"确认预授权 {token}", chat_type="p2p"))
        self.assertIn("已创建", confirmed.text)
        saved = self.store.list_tasks("alice")[0]
        self.assertEqual(json.loads(saved["scope_json"])["codex_thread_id"], task["scope"]["codex_thread_id"])

    def test_resume_request_without_id_cannot_silently_start_a_new_session(self):
        workspace = Path(self.temp.name) / "workspace"
        workspace.mkdir()
        self.service.policy = WorkspacePolicy((Path(self.temp.name),), workspace, Path(self.temp.name) / "quarantine")
        task = spec(now_utc() + timedelta(hours=1), "auto", scope={"tools": ["codex_apply"], "directories": [str(workspace)]})
        task["content"] = "调用 Codex 继续评估项目开发进度这个对话"
        response = self.service.handle(self.msg("alice", "/schedule create " + json.dumps(task)))
        self.assertIn("会话 ID", response.text)
        self.assertEqual(self.store.list_tasks("alice"), [])

    def test_codex_resume_rejects_group_and_invalid_scope(self):
        workspace = Path(self.temp.name) / "workspace"
        workspace.mkdir()
        self.service.policy = WorkspacePolicy((Path(self.temp.name),), workspace, Path(self.temp.name) / "quarantine")
        task = spec(now_utc() + timedelta(hours=1), "auto", scope={"tools": ["codex_apply"],
            "directories": [str(workspace)], "codex_thread_id": "01a187a9-24cf-7e72-ad49-93b6c99817dc"})
        task["content"] = "调用 Codex 继续指定会话"
        for change, expected in (({}, "私聊"), ({"codex_thread_id": "invalid"}, "完整会话 ID"),
                                 ({"tools": ["web_search"]}, "单独授权")):
            with self.subTest(change=change):
                changed = {**task, "scope": {**task["scope"], **change}}
                reply = self.service.handle(IncomingMessage("feishu", "alice", "group", "/schedule create " + json.dumps(changed), chat_type="group"))
                self.assertIn(expected, reply.text)
        self.assertEqual(self.store.list_tasks("alice"), [])

    def test_original_session_id_overrides_model_guess_before_confirmation(self):
        original_id = "01a187a9-24cf-7e72-ad49-93b6c99817dc"
        task = {"content": "猜测", "scope": {"codex_thread_id": "01a18908-9e49-7453-b676-210dbd520c19"}}
        proposal = {"operation": "create", "tasks": [task]}
        self.service._preserve_codex_request(proposal, f"明天调用 Codex 继续 session id {original_id} 核对进度")
        self.assertEqual(task["scope"]["codex_thread_id"], original_id)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScheduleStore(Path(self.temp.name) / "tasks.sqlite")
        self.service = ScheduleService(self.store)

    def test_worker_timeout_defaults_to_twenty_minutes_and_is_capped(self):
        self.assertEqual(ScheduleWorker(self.store).execution_timeout, 1200)
        self.assertEqual(
            ScheduleWorker(self.store, execution_timeout=3600).execution_timeout, 1200
        )

    @staticmethod
    def msg(user, text):
        return IncomingMessage("feishu", user, "chat", text)

    def test_create_requires_confirmation_and_owner_check(self):
        future = now_utc() + timedelta(hours=1)
        response = self.service.handle(
            self.msg("alice", "/schedule create " + json.dumps(spec(future)))
        )
        self.assertEqual(self.store.list_tasks("alice"), [])
        token = response.text.split("确认 ")[1].splitlines()[0]
        self.assertIn(
            "不属于当前用户", self.service.handle(self.msg("bob", f"确认 {token}")).text
        )
        created = self.service.handle(self.msg("alice", f"确认 {token}"))
        task_id = created.text.rsplit("：", 1)[1]
        self.assertIn(
            task_id, self.service.handle(self.msg("alice", "/schedule list")).text
        )
        self.assertIn(
            "找不到",
            self.service.handle(self.msg("bob", f"/schedule get {task_id}")).text,
        )

    def test_confirmation_after_trigger_time_returns_clarification(self):
        trigger = now_utc() + timedelta(minutes=1)
        response = self.service.handle(
            self.msg("alice", "/schedule create " + json.dumps(spec(trigger)))
        )
        token = response.text.split("确认 ")[1].splitlines()[0]
        with patch(
            "tele_bot.scheduled.service.now_utc",
            return_value=trigger + timedelta(seconds=1),
        ):
            result = self.service.handle(self.msg("alice", f"确认 {token}"))
        self.assertIn("触发时间必须在未来", result.text)

    def test_preauthorization_requires_distinct_confirmation(self):
        future = now_utc() + timedelta(hours=1)
        task = spec(
            future, "auto", scope={"tools": ["web_search"], "directories": []}
        )
        response = self.service.handle(
            self.msg("alice", "/schedule create " + json.dumps(task))
        )
        self.assertIn("确认预授权", response.text)
        token = response.text.split("确认预授权 ")[1].splitlines()[0]
        reply = self.service.handle(self.msg("alice", f"确认 {token}"))
        self.assertIn("请明确回复", reply.text)
        fresh = reply.text.rsplit(" ", 1)[1]
        self.assertIn(
            "已创建", self.service.handle(self.msg("alice", f"确认预授权 {fresh}")).text
        )

    def test_chinese_codex_request_preserves_original_intent(self):
        from tele_bot.tools.workspace_policy import WorkspacePolicy

        workspace = Path(self.temp.name) / "job_workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("project", encoding="utf-8")
        child = workspace / "tracking_outputs"
        child.mkdir()
        (child / "report.md").write_text("child", encoding="utf-8")
        self.service.policy = WorkspacePolicy(
            (Path(self.temp.name),), workspace, Path(self.temp.name) / "quarantine"
        )
        future = now_utc() + timedelta(hours=1)
        parsed = spec(future, "remind", scope={"tools": [], "directories": []})
        parsed["content"] = "更新机器人/具身智能产品经理岗位并总结表格"
        seen = []
        self.service.parser = type(
            "Parser",
            (),
            {
                "parse": lambda _, text: (
                    seen.append(text) or {"operation": "create", "tasks": [parsed]}
                )
            },
        )()
        original = "今天下午四点半点在job_workspace目录下用codex调用里面的autumn-recruiment skill，更新机器人/具身智能产品经理相关的岗位，将新增和修改的部分总结成一个表格发给我。"
        from types import SimpleNamespace

        with patch(
            "tele_bot.scheduled.service.CodexRunner.from_env",
            return_value=SimpleNamespace(default_workspace=Path(self.temp.name)),
        ):
            response = self.service.handle(self.msg("alice", original))
        self.assertIn("确认预授权", response.text)
        self.assertIn("autumn-recruiment", response.text)
        self.assertIn("四点半在", seen[0])
        self.assertIn("job_workspace", response.text)
        self.assertNotIn("tracking_outputs", response.text)
        token = response.text.split("确认预授权 ")[1].splitlines()[0]
        self.assertIn(
            "已创建", self.service.handle(self.msg("alice", f"确认预授权 {token}")).text
        )

    def test_past_time_correction_continues_original_codex_schedule(self):
        from types import SimpleNamespace

        from tele_bot.tools.workspace_policy import WorkspacePolicy

        workspace = Path(self.temp.name) / "job_workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("project", encoding="utf-8")
        self.service.policy = WorkspacePolicy(
            (Path(self.temp.name),), workspace, Path(self.temp.name) / "quarantine"
        )
        local_now = datetime(2026, 10, 5, 17, 42, tzinfo=timezone(timedelta(hours=8)))
        parsed_texts = []

        class Parser:
            def parse(self, text):
                parsed_texts.append(text)
                corrected = "五点四十五" in text and "五点四十在" in text
                at = local_now.replace(minute=45 if corrected else 40)
                task = spec(at)
                task["content"] = "更新机器人/具身智能产品经理岗位"
                return {"operation": "create", "tasks": [task]}

        self.service.parser = Parser()
        original = (
            "今天下午五点四十在job_workspace目录下用codex调用里面的"
            "autumn-recruiment skill，更新机器人/具身智能产品经理相关的岗位，"
            "将新增和修改的部分总结成一个表格发给我。"
        )

        async def call(text):
            async with Client(
                create_schedule_server(self.service, self.msg("alice", text))
            ) as client:
                result = await client.call_tool("schedule_manage", {})
                return json.loads(result.content[0].text)["text"]

        with (
            patch("tele_bot.scheduled.service.now_utc", return_value=local_now),
            patch(
                "tele_bot.scheduled.service.CodexRunner.from_env",
                return_value=SimpleNamespace(default_workspace=Path(self.temp.name)),
            ),
        ):
            first = asyncio.run(call(original))
            second = asyncio.run(call("啊我说错了，五点四十五"))

        self.assertIn("触发时间必须在未来", first)
        self.assertIn("待创建", second)
        self.assertIn("确认预授权", second)
        self.assertIn("autumn-recruiment skill", second)
        self.assertEqual(len(parsed_texts), 2)

    def test_agent_tool_call_shows_feishu_processing_status(self):
        class Adapter:
            name = "feishu"

        class Agent:
            def handle_message(self, message):
                from tele_bot.workflows.react_graph import PROGRESS_CONTEXTVAR

                PROGRESS_CONTEXTVAR.get()("调用工具: schedule_manage")
                return OutgoingMessage(
                    message.channel, message.chat_id, "待创建定时任务"
                )

        with patch("tele_bot.service.FeishuProgressReporter") as reporter_type:
            response = MessageService(
                agent_core=Agent(),
                feishu_adapter=Adapter(),
                streaming_enabled=True,
            ).handle(self.msg("alice", "今天下午四点半点更新岗位"))
        reporter_type.return_value.start.assert_called_once()
        reporter_type.return_value.update.assert_called_once_with(
            "调用工具: schedule_manage"
        )
        reporter_type.return_value.finish.assert_called_once_with(response.text)
        self.assertTrue(response.already_sent)

    def test_worker_announces_execution_start(self):
        future = now_utc() + timedelta(hours=1)
        task = spec(
            future, "auto", scope={"tools": ["web_search"], "directories": []}
        )
        task_id = self.store.create_many("alice", "feishu", "chat", [task])[0]
        self.store.trigger(task_id, iso(future), future)
        notices = []
        worker = ScheduleWorker(
            self.store,
            lambda outgoing: notices.append(outgoing.text) or {"code": 0},
            runner=lambda payload, timeout: "完成",
        )
        worker.execute_one()
        self.assertTrue(any("开始执行" in item for item in notices))

    def test_result_attachments_survive_delivery_failure_and_restart(self):
        future = now_utc() + timedelta(hours=1)
        report = Path(self.temp.name) / "changes.xlsx"
        report.write_bytes(b"report")
        task = spec(
            future,
            "auto",
            scope={"tools": ["codex_apply"], "directories": [self.temp.name]},
        )
        task_id = self.store.create_many("alice", "feishu", "chat", [task])[0]
        self.store.trigger(task_id, iso(future), future)
        sent = []

        def failing_send(outgoing):
            if outgoing.file_paths:
                sent.append(outgoing.file_paths)
                raise RuntimeError("upload unavailable")
            return {"code": 0}

        worker = ScheduleWorker(
            self.store,
            failing_send,
            runner=lambda payload, timeout: {
                "status": "failed",
                "result": "部分步骤失败，表格已生成",
                "paths": [str(report)],
            },
        )
        worker.execute_one()
        self.assertEqual(sent, [(str(report),)])
        restarted = ScheduleWorker(
            ScheduleStore(self.store.path),
            lambda outgoing: sent.append(outgoing.file_paths) or {"code": 0},
        )
        retry_at = now_utc() + timedelta(seconds=20)
        with patch("tele_bot.scheduled.store.now_utc", return_value=retry_at):
            restarted.tick(retry_at)
        self.assertEqual(sent, [(str(report),), (str(report),)])
        with self.store._db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM notifications WHERE kind='result_file'"
                ).fetchone()[0],
                "sent",
            )

    def test_once_confirmation_expiry_and_history(self):
        future = now_utc() + timedelta(hours=1)
        task = spec(
            future, "confirm", scope={"tools": ["web_search"], "directories": []}
        )
        task_id = self.store.create_many("alice", "feishu", "chat", [task])[0]
        worker = ScheduleWorker(self.store, lambda outgoing: {"code": 0})
        worker.tick(future + timedelta(seconds=1))
        run = self.store.history(task_id, "alice")[0]
        self.assertEqual(run["status"], "awaiting_auth")
        self.assertFalse(self.store.authorize(run["id"], "bob", True))
        self.assertTrue(self.store.authorize(run["id"], "alice", True))
        worker.runner = lambda payload, timeout: "完成"
        self.assertTrue(worker.execute_one())
        self.assertEqual(self.store.get(task_id, "alice")["status"], "completed")
        self.assertEqual(self.store.history(task_id, "alice")[0]["status"], "success")
        self.assertEqual(self.store.list_tasks("alice"), [])

    def test_missed_occurrence_skips_and_weekly_continues(self):
        future = now_utc() + timedelta(hours=1)
        rule = {
            "kind": "weekly",
            "weekdays": [future.astimezone(timezone(timedelta(hours=8))).weekday()],
            "interval": 1,
        }
        task_id = self.store.create_many(
            "alice", "feishu", "chat", [spec(future, rule=rule)]
        )[0]
        ScheduleWorker(self.store, lambda outgoing: {"code": 0}).tick(
            future + timedelta(hours=2)
        )
        run = self.store.history(task_id, "alice")[0]
        self.assertEqual(run["status"], "skipped")
        task = self.store.get(task_id, "alice")
        self.assertEqual(task["status"], "active")
        self.assertIsNotNone(task["next_at"])

    def test_recovery_does_not_repeat_running_execution(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many(
            "alice",
            "feishu",
            "chat",
            [
                spec(
                    future,
                    "auto",
                    scope={"tools": ["web_search"], "directories": []},
                )
            ],
        )[0]
        self.store.trigger(task_id, iso(future), future)
        run = self.store.claim_run()
        self.assertIsNotNone(run)
        self.assertEqual(len(self.store.recover()), 1)
        self.assertIsNone(self.store.claim_run())
        self.assertEqual(self.store.history(task_id, "alice")[0]["status"], "failed")

    def test_offline_daily_cycles_coalesce_into_one_catchup(self):
        past = now_utc() - timedelta(days=3, minutes=1)
        task = spec(
            past,
            "auto",
            rule={"kind": "daily", "interval": 1},
            scope={"tools": ["web_search"], "directories": []},
        )
        task["missed_policy"] = "run_once"
        task_id = self.store.create_many("alice", "feishu", "chat", [task])[0]
        current = now_utc()
        ScheduleWorker(self.store, lambda outgoing: {"code": 0}).tick(current)
        runs = self.store.history(task_id, "alice")
        self.assertGreaterEqual(len(runs), 3)
        self.assertEqual(sum(run["status"] == "queued" for run in runs), 1)
        self.assertEqual(sum(run["status"] == "skipped" for run in runs), len(runs) - 1)
        self.assertGreater(
            datetime.fromisoformat(self.store.get(task_id, "alice")["next_at"]), current
        )

    def test_cancel_during_execution_is_visible_to_runner(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many(
            "alice",
            "feishu",
            "chat",
            [
                spec(
                    future,
                    "auto",
                    scope={"tools": ["web_search"], "directories": []},
                )
            ],
        )[0]
        self.store.trigger(task_id, iso(future), future)
        run = self.store.claim_run()
        self.assertTrue(self.store.cancel(task_id, "alice"))
        self.assertTrue(self.store.is_run_cancelled(run["id"]))
        self.store.finish_run(run["id"], "cancelled", "cancelled by user")
        self.assertEqual(self.store.history(task_id, "alice")[0]["status"], "cancelled")

    def test_expired_authorization_skips_only_this_cycle(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many(
            "alice",
            "feishu",
            "chat",
            [
                spec(
                    future,
                    "confirm",
                    rule={"kind": "daily", "interval": 1},
                    scope={"tools": ["web_search"], "directories": []},
                )
            ],
        )[0]
        worker = ScheduleWorker(self.store, lambda outgoing: {"code": 0})
        worker.tick(future)
        run = self.store.history(task_id, "alice")[0]
        worker.tick(future + timedelta(seconds=901))
        self.assertFalse(self.store.authorize(run["id"], "alice", True))
        self.assertEqual(self.store.history(task_id, "alice")[0]["status"], "skipped")
        self.assertEqual(self.store.get(task_id, "alice")["status"], "active")

    def test_weekly_multiple_days(self):
        monday = datetime(2026, 10, 5, 11, tzinfo=timezone.utc)  # 19:00 CST
        rule = {"kind": "weekly", "weekdays": [0, 5], "interval": 2}
        saturday = next_time(monday, rule)
        self.assertEqual(saturday.date().isoformat(), "2026-10-10")
        self.assertEqual(next_time(saturday, rule).date().isoformat(), "2026-10-19")

    def test_only_one_worker_owns_database_lease(self):
        first = ScheduleWorker(self.store, poll_seconds=0.02)
        second = ScheduleWorker(self.store, poll_seconds=0.02)
        try:
            first.start()
            second.start()
            self.assertEqual(len(first.threads), 2)
            self.assertEqual(second.threads, [])
        finally:
            second.stop()
            first.stop()

    def test_failed_feedback_is_retried(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many("alice", "feishu", "chat", [spec(future)])[0]
        sent = []

        def send(message):
            sent.append(message.text)
            return {"code": -1 if len(sent) == 1 else 0}

        worker = ScheduleWorker(self.store, send)
        worker.tick(future)
        with self.store._db() as db:
            notice = db.execute(
                "SELECT status,attempts FROM notifications WHERE task_id=?", (task_id,)
            ).fetchone()
            self.assertEqual((notice["status"], notice["attempts"]), ("pending", 1))
        with patch(
            "tele_bot.scheduled.store.now_utc",
            return_value=now_utc() + timedelta(seconds=10),
        ):
            worker.tick(future + timedelta(seconds=10))
        with self.store._db() as db:
            notice = db.execute(
                "SELECT status,attempts FROM notifications WHERE task_id=?", (task_id,)
            ).fetchone()
            self.assertEqual((notice["status"], notice["attempts"]), ("sent", 2))
        self.assertEqual(len(sent), 2)

    def test_expired_authorization_request_is_not_retried(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many(
            "alice",
            "feishu",
            "chat",
            [
                spec(
                    future,
                    "confirm",
                    scope={"tools": ["web_search"], "directories": []},
                )
            ],
        )[0]
        sent = []

        def send(message):
            sent.append(message.text)
            return {"code": -1 if len(sent) == 1 else 0}

        worker = ScheduleWorker(self.store, send)
        base = now_utc()
        with patch("tele_bot.scheduled.store.now_utc", return_value=base):
            worker.tick(future)
            worker.tick(future + timedelta(seconds=901))
        with patch(
            "tele_bot.scheduled.store.now_utc",
            return_value=base + timedelta(seconds=10),
        ):
            worker.tick(future + timedelta(seconds=911))
        with self.store._db() as db:
            statuses = {
                row["kind"]: row["status"]
                for row in db.execute(
                    "SELECT kind,status FROM notifications WHERE task_id=?", (task_id,)
                ).fetchall()
            }
        self.assertEqual(statuses["auth_request"], "superseded")
        self.assertEqual(len(sent), 2)


class ScheduleMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScheduleStore(Path(self.temp.name) / "schedule.sqlite")
        self.future = now_utc() + timedelta(hours=1)

        class Parser:
            def parse(inner, text):
                return {
                    "operation": "create",
                    "tasks": [
                        {
                            "content": "提醒检查报告",
                            "at": iso(self.future),
                            "rule": {"kind": "once"},
                            "mode": "remind",
                            "scope": {"tools": [], "directories": []},
                        }
                    ],
                }

        self.service = ScheduleService(self.store, Parser())

    def test_official_mcp_client_proposes_then_confirms(self):
        async def call(message):
            async with Client(create_schedule_server(self.service, message)) as client:
                listed = await client.list_tools()
                self.assertEqual(
                    [tool.name for tool in listed.tools], ["schedule_manage"]
                )
                result = await client.call_tool("schedule_manage", {})
                self.assertFalse(result.is_error)
                return json.loads(result.content[0].text)

        original = IncomingMessage(
            "feishu", "alice", "chat", "明天上午九点提醒我检查报告"
        )
        proposal = asyncio.run(call(original))
        self.assertIn("待创建", proposal["text"])
        self.assertEqual(self.store.list_tasks("alice"), [])
        token = proposal["text"].split("确认 ")[1].splitlines()[0]
        confirmation = asyncio.run(
            call(IncomingMessage("feishu", "alice", "chat", f"确认 {token}"))
        )
        self.assertIn("已创建任务", confirmation["text"])
        self.assertEqual(len(self.store.list_tasks("alice")), 1)

    def test_agent_tool_invokes_mcp_with_trusted_message_and_blocks_api(self):
        schedule_tool = next(
            tool
            for tool in build_core_tools(schedule_service=self.service)
            if tool.name == "schedule_manage"
        )
        self.assertEqual(
            set(schedule_tool.tool_call_schema.model_json_schema()["properties"]),
            {"request"},
        )
        message_token = CURRENT_SCHEDULE_MESSAGE.set(
            IncomingMessage("feishu", "alice", "chat", "明天上午九点提醒我检查报告")
        )
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
        schedule_tool = next(
            tool
            for tool in build_core_tools(schedule_service=self.service)
            if tool.name == "schedule_manage"
        )

        class FakeLLM:
            config = None

            def invoke(self, messages, config=None):
                self.config = config
                if any(
                    isinstance(message, ToolMessage)
                    and message.name == "schedule_manage"
                    for message in messages
                ):
                    return AIMessage(content="抱歉，我无法设置定时任务")
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "schedule_manage", "args": {}, "id": "call-1"}
                    ],
                )

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
        task_id = self.store.create_many(
            "alice",
            "feishu",
            "chat",
            [
                {
                    "content": "更新机器人产品经理岗位",
                    "at": iso(self.future),
                    "rule": {
                        "kind": "once",
                        "interval": 1,
                        "weekdays": [],
                        "count": None,
                        "until": None,
                    },
                    "mode": "remind",
                    "scope": {"tools": [], "directories": []},
                    "missed_policy": "skip",
                    "grace_seconds": 300,
                    "auth_seconds": 900,
                }
            ],
        )[0]
        parsed = []

        class Parser:
            def parse(inner, text):
                parsed.append(text)
                if task_id in text and "删除这个任务" in text:
                    return {"operation": "delete", "task_id": task_id}
                return {"operation": "clarify", "question": "请提供任务 ID。"}

        self.service.parser = Parser()
        schedule_tool = next(
            t
            for t in build_core_tools(schedule_service=self.service)
            if t.name == "schedule_manage"
        )

        class Model:
            calls = 0

            def invoke(inner, messages, config=None):
                inner.calls += 1
                # Reproduce the actual model retrying the same empty call.
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "schedule_manage",
                            "args": {},
                            "id": f"call-{inner.calls}",
                        }
                    ],
                )

        model = Model()
        graph = build_react_graph(model, [schedule_tool])
        config = {"configurable": {"thread_id": "chat"}}
        graph.update_state(
            config,
            {
                "messages": [
                    HumanMessage(content="现在有哪些定时任务？"),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "schedule_manage", "args": {}, "id": "list"}
                        ],
                    ),
                    ToolMessage(
                        name="schedule_manage",
                        tool_call_id="list",
                        content=json.dumps(
                            {
                                "handled": True,
                                "text": f"当前活跃任务：\n{task_id} [active] 更新机器人产品经理岗位",
                            }
                        ),
                    ),
                    AIMessage(content=f"当前有一个活跃任务：{task_id}"),
                    HumanMessage(content="删除这个任务"),
                    AIMessage(content="请提供任务 ID。"),
                ]
            },
        )
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            result = ReactAgentExecutor(graph=graph, model_name="fake").handle(
                IncomingMessage("feishu", "alice", "chat", "前面提到的这个任务")
            )
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed)
        self.assertIn("待删除任务", result.reply_text)
        self.assertIn(task_id, result.reply_text)
        self.assertIn(task_id, parsed[0])
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.store.get(task_id, "alice")["status"], "active")
        confirmation = result.reply_text.rsplit("确认 ", 1)[1]
        confirmed = self.service.handle(
            IncomingMessage("feishu", "alice", "chat", f"确认 {confirmation}")
        )
        self.assertIn("已取消后续调度", confirmed.text)

    def test_structured_proposal_uses_same_turn_tool_result(self):
        @tool
        def read_requirements() -> str:
            """Read the requested job direction."""
            return "岗位方向：机器人/具身智能产品经理；输出新增和修改表格。"

        schedule_tool = next(
            t
            for t in build_core_tools(schedule_service=self.service)
            if t.name == "schedule_manage"
        )
        seen = []

        class Model:
            def invoke(inner, messages, config=None):
                seen.append(messages)
                if not any(
                    isinstance(m, ToolMessage) and m.name == "read_requirements"
                    for m in messages
                ):
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "read_requirements", "args": {}, "id": "read"}
                        ],
                    )
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "schedule_manage",
                            "id": "schedule",
                            "args": {
                                "request": {
                                    "operation": "create",
                                    "tasks": [
                                        {
                                            "content": "提醒更新机器人/具身智能产品经理岗位，输出新增和修改表格",
                                            "at": iso(self.future),
                                            "mode": "remind",
                                            "rule": {"kind": "once"},
                                            "scope": {"tools": [], "directories": []},
                                        }
                                    ],
                                }
                            },
                        }
                    ],
                )

        graph = build_react_graph(Model(), [read_requirements, schedule_tool])
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            result = ReactAgentExecutor(graph=graph, model_name="fake").handle(
                IncomingMessage(
                    "feishu", "alice", "chat", "先读岗位要求，明天按要求提醒我"
                )
            )
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
                result = await client.call_tool(
                    "schedule_manage",
                    {
                        "request": {
                            "operation": "authorize",
                            "user_id": "bob",
                            "task_id": "fake",
                        }
                    },
                )
                return result

        result = asyncio.run(call())
        self.assertFalse(result.is_error)
        self.assertIn("不支持", json.loads(result.content[0].text)["text"])

    def test_structured_request_cannot_use_another_users_task(self):
        proposed = self.service.handle(
            IncomingMessage("feishu", "alice", "chat", "明天提醒我检查报告")
        )
        token = proposed.text.split("确认 ")[1].splitlines()[0]
        created = self.service.handle(
            IncomingMessage("feishu", "alice", "chat", f"确认 {token}")
        )
        task_id = created.text.rsplit("：", 1)[1]
        result = self.service.handle(
            IncomingMessage("feishu", "bob", "chat", "删除这个任务"),
            request={"operation": "delete", "task_id": task_id, "user_id": "alice"},
        )
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
        schedule_tool = next(
            t
            for t in build_core_tools(schedule_service=self.service)
            if t.name == "schedule_manage"
        )

        class Model:
            calls = 0

            def invoke(inner, messages, config=None):
                inner.calls += 1
                name = "read_requirements" if inner.calls == 1 else "schedule_manage"
                return AIMessage(
                    content="",
                    tool_calls=[{"name": name, "args": {}, "id": str(inner.calls)}],
                )

        graph = build_react_graph(Model(), [read_requirements, schedule_tool])
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            ReactAgentExecutor(graph=graph, model_name="fake").handle(
                IncomingMessage("feishu", "alice", "chat", "先读要求，再创建定时任务")
            )
        finally:
            CURRENT_SCHEDULE_ALLOWED.reset(allowed)
        self.assertEqual(len(parsed), 1)
        self.assertIn("机器人/具身智能产品经理", parsed[0])
        self.assertIn('"role": "tool"', parsed[0])

    def test_context_keeps_summary_under_large_same_turn_results(self):
        context = [
            HumanMessage(
                content="Earlier context (reference only):\n"
                "用户要求删除任务 1b59197868ba，尚未完成。"
            )
        ]
        context.extend(
            ToolMessage(name="read", tool_call_id=str(i), content="大量结果" * 3000)
            for i in range(20)
        )
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
                return {
                    "operation": "clarify",
                    "question": "有两个任务，请说明要删除哪一个。",
                }

        self.service.parser = Parser()
        result = self.service.handle(
            IncomingMessage("feishu", "alice", "chat", "删除这个任务"),
            context_messages=[AIMessage(content="任务 a：报告；任务 b：岗位更新。")],
        )
        self.assertIn("有两个任务", result.text)
        self.assertIn("任务 a", seen[0])
        self.assertEqual(self.store.pending_for_chat("alice", "feishu", "chat")[0], [])

    def test_log_create_with_top_level_content_falls_back_to_original_request(self):
        workspace = Path(self.temp.name) / "job_workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("job workspace", encoding="utf-8")
        self.service.policy = WorkspacePolicy(
            (Path(self.temp.name),), workspace, Path(self.temp.name) / "quarantine"
        )
        original = (
            "今天凌晨三点半在job_workspace目录下用codex调用里面的autumn-recruiment skill，"
            "更新机器人/具身智能产品经理 相关的岗位，将新增和修改的部分总结成一个表格发给我。"
        )
        trigger = datetime(2026, 10, 7, 3, 30, tzinfo=timezone(timedelta(hours=8)))
        parsed = []

        class Parser:
            def parse(inner, text):
                parsed.append(text)
                return {
                    "operation": "create",
                    "tasks": [
                        {
                            "content": "更新岗位",
                            "at": iso(trigger),
                            "mode": "remind",
                            "rule": {"kind": "once"},
                            "scope": {"tools": [], "directories": []},
                        }
                    ],
                }

        self.service.parser = Parser()
        schedule_tool = next(
            t
            for t in build_core_tools(schedule_service=self.service)
            if t.name == "schedule_manage"
        )

        class Model:
            def invoke(inner, messages, config=None):
                # Exact shape recorded at 2026-10-07 02:09:41.
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "schedule_manage",
                            "id": "create",
                            "args": {
                                "request": {"operation": "create", "content": original}
                            },
                        }
                    ],
                )

        graph = build_react_graph(Model(), [schedule_tool])
        graph.update_state(
            {"configurable": {"thread_id": "chat"}},
            {
                "messages": [
                    HumanMessage(content="今天凌晨两点半更新岗位"),
                    AIMessage(content="之前的两点半任务已取消。"),
                ]
            },
        )
        allowed = CURRENT_SCHEDULE_ALLOWED.set(True)
        try:
            with (
                patch(
                    "tele_bot.scheduled.service.CodexRunner.from_env",
                    return_value=SimpleNamespace(
                        default_workspace=Path(self.temp.name)
                    ),
                ),
                patch(
                    "tele_bot.scheduled.service.now_utc",
                    return_value=trigger - timedelta(hours=1),
                ),
            ):
                result = ReactAgentExecutor(graph=graph, model_name="fake").handle(
                    IncomingMessage("feishu", "alice", "chat", original)
                )
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
        self.assertEqual(
            len(self.store.pending_for_chat("alice", "feishu", "chat")[0]), 1
        )

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
                result = self.service.handle(
                    IncomingMessage("feishu", "alice", "chat", original),
                    request=request,
                )
                self.assertEqual(result.text, "请说明要提醒的事项。")
                self.assertEqual(parsed[-1], original)
        self.assertEqual(len(parsed), len(requests))
        self.assertEqual(self.store.pending_for_chat("alice", "feishu", "chat")[0], [])

    def test_tool_schema_describes_nested_task_content_and_time(self):
        from langchain_core.utils.function_calling import convert_to_openai_tool

        schedule_tool = next(
            t
            for t in build_core_tools(schedule_service=self.service)
            if t.name == "schedule_manage"
        )
        schema = convert_to_openai_tool(schedule_tool)["function"]["parameters"]
        request = next(
            item
            for item in schema["properties"]["request"]["anyOf"]
            if item.get("type") == "object"
        )
        self.assertIn("tasks", request["properties"])
        item = request["properties"]["tasks"]["items"]
        self.assertIn("content", item["properties"])
        self.assertIn("at", item["properties"])
        self.assertNotIn("context_messages", schema["properties"])
