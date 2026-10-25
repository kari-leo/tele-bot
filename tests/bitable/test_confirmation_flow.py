"""Reproduce real write wording, Feishu callbacks and human-readable receipts."""
import os
from pathlib import Path
import runpy
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.bitable.models import BitableError
from tele_bot.router.bitable_service import BitableCommandService, render_bitable_receipt
from tele_bot.service import MessageService
from tests.bitable.support import ServiceCase


class ConfirmationFlowTests(ServiceCase):
    def test_logged_todo_increase_request_is_explicit_create_intent(self):
        message = self.message_for(text="在todo中增加：问李玉妍产品经理相关问题，明天，紧急重要")
        result = self.service.write("create", "项目", message,
                                    [{"fields": {"名称": "问李玉妍产品经理相关问题"}}])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.client.writes), 1)

    def test_negated_or_hypothetical_increase_does_not_create(self):
        for text in ("不要增加todo", "别增加todo", "别再增加todo", "如何在todo中增加任务", "如果增加todo会怎样"):
            with self.subTest(text=text), self.assertRaises(BitableError) as error:
                self.service.write("create", "项目", self.message_for(text=text),
                                   [{"fields": {"名称": "不应写入"}}])
            self.assertEqual(error.exception.kind, "write_intent")
        self.assertFalse(self.client.writes)

    def updates(self):
        return [{"record_id": "rec1", "fields": {"日期": "2026-11-02"}},
                {"record_id": "rec2", "fields": {"日期": "2026-10-21"}}]

    def prepare(self, message_id="msg1"):
        return self.service.write("update", "项目", self.message_for(message_id=message_id), self.updates())

    def test_natural_deadline_assignment_is_explicit_write_intent(self):
        message = self.message_for(text="A的截止时间定在十一月二号，B的截止时间定在今天")
        result = self.service.write("update", "项目", message, self.updates())
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertFalse(self.client.writes)

    def test_plain_confirmation_executes_frozen_plan_without_calling_model(self):
        self.prepare()

        class Agent:
            def handle_message(self, message):
                raise AssertionError("A real confirmation must bypass the model")

        adapter = FeishuWebhookAdapter("app", "secret")
        messages = MessageService(Agent(), adapter, bitable_service=self.service)
        result = messages.handle(self.message_for(text="确认", message_id="reply"))
        self.assertIn("已修改 2 条记录", result.text)
        self.assertEqual(len(self.client.writes), 1)

    def test_actual_feishu_operator_shape_reaches_confirmation_and_is_idempotent(self):
        prepared = self.prepare()
        adapter = FeishuWebhookAdapter("app", "secret")
        payload = {"header": {"event_type": "card.action.trigger", "event_id": "callback"}, "event": {
            "operator": {"user_id": "user", "open_id": "ou_user"},
            "context": {"open_chat_id": "chat", "open_message_id": "card"},
            "action": {"tag": "button", "value": {
                "action_id": "bitable_confirm", "value": prepared["confirmation_token"]}}}}
        incoming = adapter.parse_incoming(payload)
        self.assertIsNotNone(incoming)
        commands = BitableCommandService(self.service)
        result = commands.handle(incoming)
        self.assertIn("已修改 2 条记录", result.text)
        commands.handle(incoming)
        self.assertEqual(len(self.client.writes), 1)

    def test_multiple_pending_confirmations_require_selection(self):
        self.prepare("msg1")
        self.prepare("msg2")
        result = BitableCommandService(self.service).handle(self.message_for(text="确认"))
        self.assertIsNotNone(result)
        self.assertIn("多个", result.text)
        self.assertFalse(self.client.writes)

    def test_confirmation_ignores_changes_to_unrelated_columns(self):
        result = self.prepare()
        self.client.rows["rec1"]["fields"]["数量"] = 99
        receipt = self.service.confirm(result["confirmation_token"], self.message)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(self.client.rows["rec1"]["fields"]["数量"], 99)

    def test_preview_and_result_show_names_dates_and_only_changed_fields(self):
        self.client.rows["rec1"]["fields"]["内部详情"] = "无关私密内容" * 500
        result = self.prepare()
        preview = render_bitable_receipt(self.message, result)
        self.assertIn("A", preview.card.body)
        self.assertIn("2026-11-02", preview.card.body)
        self.assertNotIn("无关私密内容", preview.card.body)
        self.assertNotIn("1793548800000", preview.card.body)
        receipt = self.service.confirm(result["confirmation_token"], self.message)
        output = render_bitable_receipt(self.message, receipt)
        self.assertIn("已修改 2 条记录", output.text)
        self.assertIn("2026-11-02", output.text)
        self.assertIn("2026-10-21", output.text)
        self.assertNotIn('"verification"', output.text)
        self.assertNotIn('"record_ids"', output.text)

    def test_changed_target_field_still_prevents_overwriting(self):
        result = self.prepare()
        self.client.rows["rec1"]["fields"]["日期"] = 1792512000000
        receipt = self.service.confirm(result["confirmation_token"], self.message)
        self.assertEqual(receipt["status"], "conflict")
        self.assertFalse(self.client.writes)

    def test_cancel_and_other_user_do_not_write(self):
        self.prepare()
        commands = BitableCommandService(self.service)
        self.assertIsNone(commands.handle(self.message_for(text="确认", user_id="other")))
        output = commands.handle(self.message_for(text="取消"))
        self.assertIn("已取消", output.text)
        self.assertFalse(self.client.writes)

    def test_real_webhook_acknowledges_and_refreshes_card_without_duplicate_write(self):
        prepared = self.prepare()
        adapter = FeishuWebhookAdapter("app", "secret")
        adapter.edit_card = Mock(return_value={"code": 0})
        adapter.send_text = Mock(return_value={"code": 0})
        message_service = MessageService(Mock(), adapter, bitable_service=self.service)
        environment = {"KB_SQLITE_PATH": str(Path(self.tmp.name) / "kb.sqlite"),
                       "SQLITE_CHECKPOINT_PATH": str(Path(self.tmp.name) / "conversations.sqlite"),
                       "TELE_BOT_SCHEDULE_DB": str(Path(self.tmp.name) / "schedule.sqlite"),
                       "WINDBORNE_POSTS_DIR": str(Path(self.tmp.name) / "missing-blog")}
        # Load the actual application with temporary stores and no live clients.
        from langgraph.checkpoint.memory import MemorySaver
        with patch.dict(os.environ, environment), \
             patch("tele_bot.config.feishu.FeishuSettings.from_env", side_effect=ValueError("test adapter")), \
             patch("tele_bot.llm.react_client.build_chat_openai", return_value=Mock()), \
             patch("tele_bot.persistence.build_sqlite_saver", return_value=MemorySaver()), \
             patch("tele_bot.bitable.bootstrap.build_bitable_service", return_value=self.service), \
             patch("tele_bot.tools.lc_adapters.build_core_tools", return_value=[]):
            application = runpy.run_path(str(Path(__file__).resolve().parents[2] / "app.py"))
        runtime = application["handle_feishu_webhook"].__globals__
        initial_attachment_store = application["message_service"].attachment_store
        runtime.update(feishu_adapter=adapter, message_service=message_service)
        payload = {"header": {"event_type": "card.action.trigger", "event_id": "click"}, "event": {
            "operator": {"user_id": "user", "open_id": "ou_user"},
            "context": {"open_chat_id": "chat", "open_message_id": "card"},
            "action": {"tag": "button", "value": {
                "action_id": "bitable_confirm", "value": prepared["confirmation_token"]}}}}
        try:
            client = TestClient(application["app"])
            response = client.post("/feishu/webhook", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertIn("toast", response.json())
            self.assertTrue(runtime["feishu_message_batcher"].wait_idle(5))
            self.assertEqual(len(self.client.writes), 1)
            adapter.edit_card.assert_called_once()
            card = adapter.edit_card.call_args.args[1]
            self.assertEqual(card.status, "success")
            self.assertFalse(card.actions)
            self.assertIn("已修改 2 条记录", card.body)
            adapter.send_text.assert_not_called()
            client.post("/feishu/webhook", json=payload)
            self.assertEqual(len(self.client.writes), 1)
        finally:
            runtime["feishu_message_batcher"].wait_idle(5)
            initial_attachment_store.close()
            message_service.attachment_store.close()
            runtime["knowledge_store"]._conn.close()

