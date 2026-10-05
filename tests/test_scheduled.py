"""Lifecycle and authorization tests for the persistent scheduler."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from tele_bot.mcp.schedule import create_schedule_server
from tele_bot.models import IncomingMessage, OutgoingMessage
from tele_bot.scheduled.service import ScheduleService
from tele_bot.scheduled.store import ScheduleStore, iso, next_time, now_utc
from tele_bot.scheduled.worker import ScheduleWorker
from tele_bot.service import MessageService


def spec(at, mode="remind", rule=None, scope=None):
    return {
        "content": "查询资料并生成报告" if mode != "remind" else "提醒检查报告",
        "at": iso(at), "rule": rule or {"kind": "once"}, "mode": mode,
        "scope": scope or {"tools": [], "directories": []},
        "missed_policy": "skip", "grace_seconds": 300, "auth_seconds": 900,
    }


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ScheduleStore(Path(self.temp.name) / "tasks.sqlite")
        self.service = ScheduleService(self.store)

    def test_worker_timeout_defaults_to_twenty_minutes_and_is_capped(self):
        self.assertEqual(ScheduleWorker(self.store).execution_timeout, 1200)
        self.assertEqual(ScheduleWorker(self.store, execution_timeout=3600).execution_timeout, 1200)

    @staticmethod
    def msg(user, text):
        return IncomingMessage("feishu", user, "chat", text)

    def test_create_requires_confirmation_and_owner_check(self):
        future = now_utc() + timedelta(hours=1)
        response = self.service.handle(self.msg("alice", "/schedule create " + json.dumps(spec(future))))
        self.assertEqual(self.store.list_tasks("alice"), [])
        token = response.text.split("确认 ")[1].splitlines()[0]
        self.assertIn("不属于当前用户", self.service.handle(self.msg("bob", f"确认 {token}")).text)
        created = self.service.handle(self.msg("alice", f"确认 {token}"))
        task_id = created.text.rsplit("：", 1)[1]
        self.assertIn(task_id, self.service.handle(self.msg("alice", "/schedule list")).text)
        self.assertIn("找不到", self.service.handle(self.msg("bob", f"/schedule get {task_id}")).text)

    def test_confirmation_after_trigger_time_returns_clarification(self):
        trigger = now_utc() + timedelta(minutes=1)
        response = self.service.handle(self.msg("alice", "/schedule create " + json.dumps(spec(trigger))))
        token = response.text.split("确认 ")[1].splitlines()[0]
        with patch("tele_bot.scheduled.service.now_utc", return_value=trigger + timedelta(seconds=1)):
            result = self.service.handle(self.msg("alice", f"确认 {token}"))
        self.assertIn("触发时间必须在未来", result.text)

    def test_preauthorization_requires_distinct_confirmation(self):
        future = now_utc() + timedelta(hours=1)
        task = spec(future, "auto", scope={"tools": ["opencli_search"], "directories": []})
        response = self.service.handle(self.msg("alice", "/schedule create " + json.dumps(task)))
        self.assertIn("确认预授权", response.text)
        token = response.text.split("确认预授权 ")[1].splitlines()[0]
        reply = self.service.handle(self.msg("alice", f"确认 {token}"))
        self.assertIn("请明确回复", reply.text)
        fresh = reply.text.rsplit(" ", 1)[1]
        self.assertIn("已创建", self.service.handle(self.msg("alice", f"确认预授权 {fresh}")).text)

    def test_chinese_codex_request_preserves_original_intent(self):
        from tele_bot.tools.workspace_policy import WorkspacePolicy
        workspace = Path(self.temp.name) / "job_workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("project", encoding="utf-8")
        child = workspace / "tracking_outputs"
        child.mkdir()
        (child / "report.md").write_text("child", encoding="utf-8")
        self.service.policy = WorkspacePolicy((Path(self.temp.name),), workspace, Path(self.temp.name) / "quarantine")
        future = now_utc() + timedelta(hours=1)
        parsed = spec(future, "remind", scope={"tools": [], "directories": []})
        parsed["content"] = "更新机器人/具身智能产品经理岗位并总结表格"
        seen = []
        self.service.parser = type("Parser", (), {"parse": lambda _, text: seen.append(text) or {"operation": "create", "tasks": [parsed]}})()
        original = "今天下午四点半点在job_workspace目录下用codex调用里面的autumn-recruiment skill，更新机器人/具身智能产品经理相关的岗位，将新增和修改的部分总结成一个表格发给我。"
        from types import SimpleNamespace
        with patch("tele_bot.scheduled.service.CodexRunner.from_env", return_value=SimpleNamespace(default_workspace=Path(self.temp.name))):
            response = self.service.handle(self.msg("alice", original))
        self.assertIn("确认预授权", response.text)
        self.assertIn("autumn-recruiment", response.text)
        self.assertIn("四点半在", seen[0])
        self.assertIn("job_workspace", response.text)
        self.assertNotIn("tracking_outputs", response.text)
        token = response.text.split("确认预授权 ")[1].splitlines()[0]
        self.assertIn("已创建", self.service.handle(self.msg("alice", f"确认预授权 {token}")).text)

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
        original = ("今天下午五点四十在job_workspace目录下用codex调用里面的"
                    "autumn-recruiment skill，更新机器人/具身智能产品经理相关的岗位，"
                    "将新增和修改的部分总结成一个表格发给我。")

        async def call(text):
            async with Client(create_schedule_server(self.service, self.msg("alice", text))) as client:
                result = await client.call_tool("schedule_manage", {})
                return json.loads(result.content[0].text)["text"]

        with patch("tele_bot.scheduled.service.now_utc", return_value=local_now), patch(
            "tele_bot.scheduled.service.CodexRunner.from_env",
            return_value=SimpleNamespace(default_workspace=Path(self.temp.name)),
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
                return OutgoingMessage(message.channel, message.chat_id, "待创建定时任务")
        with patch("tele_bot.service.FeishuProgressReporter") as reporter_type:
            response = MessageService(
                agent_core=Agent(), feishu_adapter=Adapter(), streaming_enabled=True,
            ).handle(self.msg("alice", "今天下午四点半点更新岗位"))
        reporter_type.return_value.start.assert_called_once()
        reporter_type.return_value.update.assert_called_once_with("调用工具: schedule_manage")
        reporter_type.return_value.finish.assert_called_once_with(response.text)
        self.assertTrue(response.already_sent)

    def test_worker_announces_execution_start(self):
        future = now_utc() + timedelta(hours=1)
        task = spec(future, "auto", scope={"tools": ["opencli_search"], "directories": []})
        task_id = self.store.create_many("alice", "feishu", "chat", [task])[0]
        self.store.trigger(task_id, iso(future), future)
        notices = []
        worker = ScheduleWorker(self.store, lambda outgoing: notices.append(outgoing.text) or {"code": 0},
                                runner=lambda payload, timeout: "完成")
        worker.execute_one()
        self.assertTrue(any("开始执行" in item for item in notices))

    def test_once_confirmation_expiry_and_history(self):
        future = now_utc() + timedelta(hours=1)
        task = spec(future, "confirm", scope={"tools": ["opencli_search"], "directories": []})
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
        rule = {"kind": "weekly", "weekdays": [future.astimezone(timezone(timedelta(hours=8))).weekday()], "interval": 1}
        task_id = self.store.create_many("alice", "feishu", "chat", [spec(future, rule=rule)])[0]
        ScheduleWorker(self.store, lambda outgoing: {"code": 0}).tick(future + timedelta(hours=2))
        run = self.store.history(task_id, "alice")[0]
        self.assertEqual(run["status"], "skipped")
        task = self.store.get(task_id, "alice")
        self.assertEqual(task["status"], "active")
        self.assertIsNotNone(task["next_at"])

    def test_recovery_does_not_repeat_running_execution(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many("alice", "feishu", "chat", [spec(future, "auto", scope={"tools": ["opencli_search"], "directories": []})])[0]
        self.store.trigger(task_id, iso(future), future)
        run = self.store.claim_run()
        self.assertIsNotNone(run)
        self.assertEqual(len(self.store.recover()), 1)
        self.assertIsNone(self.store.claim_run())
        self.assertEqual(self.store.history(task_id, "alice")[0]["status"], "failed")

    def test_offline_daily_cycles_coalesce_into_one_catchup(self):
        past = now_utc() - timedelta(days=3, minutes=1)
        task = spec(past, "auto", rule={"kind": "daily", "interval": 1},
                    scope={"tools": ["opencli_search"], "directories": []})
        task["missed_policy"] = "run_once"
        task_id = self.store.create_many("alice", "feishu", "chat", [task])[0]
        current = now_utc()
        ScheduleWorker(self.store, lambda outgoing: {"code": 0}).tick(current)
        runs = self.store.history(task_id, "alice")
        self.assertGreaterEqual(len(runs), 3)
        self.assertEqual(sum(run["status"] == "queued" for run in runs), 1)
        self.assertEqual(sum(run["status"] == "skipped" for run in runs), len(runs) - 1)
        self.assertGreater(datetime.fromisoformat(self.store.get(task_id, "alice")["next_at"]), current)

    def test_cancel_during_execution_is_visible_to_runner(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many("alice", "feishu", "chat", [
            spec(future, "auto", scope={"tools": ["opencli_search"], "directories": []})])[0]
        self.store.trigger(task_id, iso(future), future)
        run = self.store.claim_run()
        self.assertTrue(self.store.cancel(task_id, "alice"))
        self.assertTrue(self.store.is_run_cancelled(run["id"]))
        self.store.finish_run(run["id"], "cancelled", "cancelled by user")
        self.assertEqual(self.store.history(task_id, "alice")[0]["status"], "cancelled")

    def test_expired_authorization_skips_only_this_cycle(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many("alice", "feishu", "chat", [
            spec(future, "confirm", rule={"kind": "daily", "interval": 1},
                 scope={"tools": ["opencli_search"], "directories": []})])[0]
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
            notice = db.execute("SELECT status,attempts FROM notifications WHERE task_id=?", (task_id,)).fetchone()
            self.assertEqual((notice["status"], notice["attempts"]), ("pending", 1))
        with patch("tele_bot.scheduled.store.now_utc", return_value=now_utc() + timedelta(seconds=10)):
            worker.tick(future + timedelta(seconds=10))
        with self.store._db() as db:
            notice = db.execute("SELECT status,attempts FROM notifications WHERE task_id=?", (task_id,)).fetchone()
            self.assertEqual((notice["status"], notice["attempts"]), ("sent", 2))
        self.assertEqual(len(sent), 2)

    def test_expired_authorization_request_is_not_retried(self):
        future = now_utc() + timedelta(hours=1)
        task_id = self.store.create_many("alice", "feishu", "chat", [
            spec(future, "confirm", scope={"tools": ["opencli_search"], "directories": []})])[0]
        sent = []

        def send(message):
            sent.append(message.text)
            return {"code": -1 if len(sent) == 1 else 0}

        worker = ScheduleWorker(self.store, send)
        base = now_utc()
        with patch("tele_bot.scheduled.store.now_utc", return_value=base):
            worker.tick(future)
            worker.tick(future + timedelta(seconds=901))
        with patch("tele_bot.scheduled.store.now_utc", return_value=base + timedelta(seconds=10)):
            worker.tick(future + timedelta(seconds=911))
        with self.store._db() as db:
            statuses = {row["kind"]: row["status"] for row in db.execute(
                "SELECT kind,status FROM notifications WHERE task_id=?", (task_id,)).fetchall()}
        self.assertEqual(statuses["auth_request"], "superseded")
        self.assertEqual(len(sent), 2)  # initial failed request and expiry notice


if __name__ == "__main__":
    unittest.main()
