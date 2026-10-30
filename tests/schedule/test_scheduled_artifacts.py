import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tele_bot.models import OutgoingMessage
from tele_bot.scheduled.codex_artifacts import artifact_snapshot, validate_artifacts
from tele_bot.scheduled.delivery import send_scheduled_message
from tele_bot.scheduled.store import ScheduleStore
from tele_bot.scheduled.store import iso, now_utc
from tele_bot.scheduled import execution
from tele_bot.scheduled.worker import ScheduleWorker


class ScheduledArtifactTests(unittest.TestCase):
    def test_six_file_codex_receipt_persists_all_delivery_notices_including_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            names = ["report.md", "added.csv", "modified.csv", "progress.csv", "evidence.json", "deliveries.csv"]
            paths = [str(root / "tracking_outputs" / name) for name in names]
            store = ScheduleStore(Path(directory) / "schedule.sqlite")
            at = iso(now_utc() - timedelta(seconds=1))
            task = {"content": "用 Codex 核对代码进度并交付报告", "mode": "auto", "at": at,
                    "rule": {"kind": "once"}, "scope": {"tools": ["codex_apply"], "directories": [str(root)]},
                    "missed_policy": "run_once", "grace_seconds": 300, "auth_seconds": 900}
            task_id = store.create_many("owner", "feishu", "original-chat", [task])[0]
            store.trigger(task_id, at, now_utc())

            def codex(*args, **kwargs):
                (root / "tracking_outputs").mkdir()
                for path in paths:
                    Path(path).write_text("{}" if path.endswith(".json") else "本轮结果", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "success", "result": "核对完成", "paths": paths}), stderr="")

            worker = ScheduleWorker(store, runner=lambda payload, timeout: execution.execute(payload))
            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.side_effect = codex
                self.assertTrue(worker.execute_one())
            self.assertEqual(store.history(task_id, "owner")[0]["status"], "success")
            notices = store.pending_notifications(now_utc())
            attached = [json.loads(n["file_paths_json"])[0] for n in notices if n["kind"] == "result_file"]
            self.assertCountEqual(attached, paths)
            self.assertTrue(all(n["chat_id"] == "original-chat" for n in notices))
            self.assertFalse(worker.execute_one())

    def test_real_child_round_trip_overrides_inherited_gbk_environment(self):
        store = Mock()
        store.is_run_cancelled.return_value = False
        worker = ScheduleWorker(store)
        expected = {"ok": True, "status": "success",
                    "result": "已完成：本轮首次收录 13 家企业的 15 个岗位，另记录 1 项描述更新。",
                    "paths": ["D:/岗位表格/新增和修改.xlsx"]}
        # Keep the actual OS pipe/codec boundary; substitute only the executor
        # body so this regression never launches Codex or calls a model API.
        original = "今天凌晨三点半更新机器人/具身智能产品经理岗位，将表格发给我。"
        script = ("import json,sys; payload=json.load(sys.stdin); receipt=" + repr(expected)
                  + "; receipt['result']+='\\n收到任务：'+payload['content']; "
                  "print(json.dumps(receipt,ensure_ascii=False))")
        real_popen = subprocess.Popen

        def start_receiver(args, **kwargs):
            return real_popen([sys.executable, "-c", script], **kwargs)

        with patch.dict(os.environ, {"PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"}), \
                patch("tele_bot.scheduled.worker.subprocess.Popen", side_effect=start_receiver):
            actual = worker._subprocess_runner({"run_id": "r1", "content": original}, 10)
        self.assertEqual(actual["result"], expected["result"] + "\n收到任务：" + original)
        self.assertEqual(actual["paths"], expected["paths"])
        self.assertNotIn("\ufffd", actual["result"])

    def test_subprocess_preserves_utf8_receipt_and_attachment_paths(self):
        store = Mock()
        store.is_run_cancelled.return_value = False
        worker = ScheduleWorker(store)
        process = Mock()
        process.returncode = 0
        receipt = {"ok": True, "status": "failed", "result": "部分完成", "paths": ["表格.xlsx"]}
        process.communicate.return_value = (json.dumps(receipt, ensure_ascii=False), "")
        with patch("tele_bot.scheduled.worker.subprocess.Popen", return_value=process) as popen:
            result = worker._subprocess_runner({"run_id": "r1", "content": "中文任务"}, 10)
        self.assertEqual(result["paths"], ["表格.xlsx"])
        self.assertEqual(result["result"], "部分完成")
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "strict")
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUTF8"], "1")

    def test_only_current_scoped_files_can_be_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            old = root / "old.xlsx"
            old.write_bytes(b"old")
            before = artifact_snapshot(root)
            current = root / "changes.csv"
            current.write_text("公司,变更\n", encoding="utf-8")
            self.assertEqual(validate_artifacts([str(current)], root, before), [str(current)])
            for invalid in ([str(old)], [str(root.parent / "secret.xlsx")], [str(root / ".codex" / "state.xlsx")], [str(root / "missing.xlsx")]):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    validate_artifacts(invalid, root, before)

    def test_json_evidence_keeps_scope_freshness_and_size_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            old = root / "old.json"
            old.write_text("{}", encoding="utf-8")
            before = artifact_snapshot(root)
            current = root / "evidence.json"
            current.write_text('{"passed": 30}', encoding="utf-8")
            self.assertEqual(validate_artifacts([str(current)], root, before), [str(current)])
            for invalid in (old, root.parent / "secret.json", root / ".codex" / "auth.json"):
                with self.subTest(path=invalid), self.assertRaises(ValueError):
                    validate_artifacts([str(invalid)], root, before)
            with current.open("wb") as output:
                output.truncate(30 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ValueError, "exceeds"):
                validate_artifacts([str(current)], root, before)

    def test_file_only_notification_uploads_to_the_original_chat(self):
        adapter = Mock()
        adapter.send_file.return_value = {"code": 0}
        self.assertEqual(send_scheduled_message(adapter, OutgoingMessage("feishu", "original-chat", "", file_paths=("changes.xlsx",))), {"code": 0})
        adapter.send_text.assert_not_called()
        adapter.send_file.assert_called_once_with("original-chat", "changes.xlsx")
        adapter.send_file.return_value = {"code": 999, "msg": "upload failed"}
        self.assertEqual(send_scheduled_message(adapter, OutgoingMessage("feishu", "original-chat", "", file_paths=("changes.xlsx",)))["code"], 999)

    def test_existing_database_migrates_notifications_without_losing_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite"
            ScheduleStore(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute("ALTER TABLE notifications DROP COLUMN file_paths_json")
                db.commit()
            store = ScheduleStore(path)
            notice = store.enqueue_notification({"id": "t", "owner_id": "u", "channel": "feishu", "chat_id": "c"}, "", "result_file", ("changes.xlsx",))
            self.assertEqual(json.loads(store.claim_notification(notice["id"])["file_paths_json"]), ["changes.xlsx"])
