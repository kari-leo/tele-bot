"""Replay a write request followed by a value-only clarification through real tools."""
import json
from dataclasses import replace
from unittest.mock import patch

from tele_bot.bitable.intents import CURRENT_INTENT, write_actions
from tele_bot.bitable.models import CURRENT_BITABLE_MESSAGE
from tele_bot.bitable.policy import BitablePolicy
from tele_bot.bitable.service import BitableService
from tele_bot.persistence.bitable_store import BitableStore

from tele_bot.models import OutgoingMessage
from tele_bot.service import MessageService
from tele_bot.tools.bitable import build_bitable_tools
from tests.bitable.support import ServiceCase


class IntentLifecycleTests(ServiceCase):
    def messages(self):
        tools = {t.name: t for t in build_bitable_tools(self.service)}
        owner = self
        self.write_target = "项目"
        self.write_record = "rec1"
        self.write_fields = None
        self.write_action = "update"

        class Agent:
            def handle_message(self, message):
                if message.text == "请修改 A 的网址":
                    tools["bitable_query"].invoke({"target": "项目", "record_id": "rec1", "fields": ["网址"]})
                    return OutgoingMessage(message.channel, message.chat_id, "请提供完整 URL")
                if message.text.startswith("https://"):
                    fields = owner.write_fields or {"网址": {"text": "投递", "link": message.text}}
                    args = {"target": owner.write_target}
                    if owner.write_action == "delete":
                        args["record_ids"] = [owner.write_record]
                    else:
                        args["records"] = [{"record_id": owner.write_record, "fields": fields}]
                    owner.last_result = json.loads(tools[f"bitable_{owner.write_action}_records"].invoke(args))
                return OutgoingMessage(message.channel, message.chat_id, "正常回复")

        return MessageService(Agent(), type("Adapter", (), {"name": "feishu"})(), bitable_service=self.service)

    def request(self, messages):
        return messages.handle(self.message_for(text="请修改 A 的网址", message_id="request"))

    def supplement(self, messages, **kwargs):
        return messages.handle(self.message_for(**{"text": "https://example.com/job", "message_id": "url", **kwargs}))

    def test_url_clarification_previews_then_confirms_once(self):
        messages = self.messages()
        self.request(messages)
        preview = self.supplement(messages)
        self.assertEqual(self.last_result["status"], "awaiting_confirmation")
        self.assertIsNotNone(preview.card)
        self.assertFalse(self.client.writes)
        result = messages.handle(self.message_for(text="确认", message_id="confirm"))
        self.assertIn("已修改 1 条记录", result.text)
        self.assertEqual(len(self.client.writes), 1)
        self.supplement(messages, message_id="another")
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_persists_across_service_and_message_service_restart(self):
        messages = self.messages()
        self.request(messages)
        self.service = BitableService(self.settings, self.client, self.policy, BitableStore(self.settings.sqlite_path))
        preview = self.supplement(self.messages())
        self.assertIsNotNone(preview.card)
        self.assertFalse(self.client.writes)

    def test_other_users_chats_channels_and_raw_api_cannot_inherit(self):
        messages = self.messages()
        self.request(messages)
        for changes in ({"user_id": "other"}, {"chat_id": "other"}, {"channel": "raw"}):
            with self.subTest(changes=changes):
                self.supplement(messages, **changes)
                self.assertIn(self.last_result["error"], {"write_intent", "unauthorized"})
        raw = self.message_for(text="https://example.com/job", message_id="raw")
        messages.handle(raw, allow_bitable=False)
        self.assertEqual(self.last_result["error"], "unauthorized")
        self.assertIsNotNone(self.supplement(messages).card)

    def test_record_field_and_action_cannot_expand(self):
        for changes in ({"write_record": "rec2"}, {"write_fields": {"数量": 99}}, {"write_action": "delete"}):
            with self.subTest(changes=changes):
                messages = self.messages()
                # Each explicit new request has a fresh transport ID.
                messages.handle(self.message_for(text="请修改 A 的网址", message_id=str(changes)))
                for key, value in changes.items():
                    setattr(self, key, value)
                self.supplement(messages)
                self.assertEqual(self.last_result["error"], "write_intent")
        self.assertFalse(self.client.writes)

    def test_followup_query_cannot_expand_original_record_scope(self):
        messages = self.messages()
        self.request(messages)
        query = next(t for t in build_bitable_tools(self.service) if t.name == "bitable_query")
        agent = messages.agent_core

        class QueryThenWrite:
            def handle_message(inner, message):
                query.invoke({"target": "项目", "record_id": "rec2", "fields": ["网址"]})
                return agent.handle_message(message)

        messages.agent_core = QueryThenWrite()
        self.write_record = "rec2"
        self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_different_table_cannot_inherit(self):
        self.service.policy = BitablePolicy([
            {"alias": "项目", "app_token": "base1", "table_id": "tbl1"},
            {"alias": "其他表", "app_token": "base1", "table_id": "tbl2"}])
        messages = self.messages()
        self.request(messages)
        self.write_target = "其他表"
        self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_expiry_is_fixed_and_old_transport_replay_does_not_renew(self):
        messages = self.messages()
        with patch("tele_bot.bitable.intents.time.time", return_value=1000):
            self.request(messages)
        with patch("tele_bot.bitable.intents.time.time", return_value=1601):
            self.request(messages)  # redelivery must not grant another ten minutes
            self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_cancellation_topic_switch_and_new_request_end_old_scope(self):
        for text in ("取消", "算了", "不要修改", "今天天气怎么样", "/kb list", "请删除 B"):
            with self.subTest(text=text):
                messages = self.messages()
                messages.handle(self.message_for(text="请修改 A 的网址", message_id="root-" + text))
                messages.handle(self.message_for(text=text, message_id="switch-" + text))
                self.supplement(messages)
                self.assertEqual(self.last_result["error"], "write_intent")
        self.assertFalse(self.client.writes)

    def test_preview_consumes_request_and_repeated_delivery_returns_same_preview(self):
        messages = self.messages()
        self.request(messages)
        first = self.supplement(messages)
        replay = self.supplement(messages)
        self.assertEqual(first.card.actions, replay.card.actions)
        self.supplement(messages, message_id="fresh-url")
        self.assertEqual(self.last_result["error"], "write_intent")
        messages.handle(self.message_for(text="取消", message_id="cancel"))
        self.assertFalse(self.client.writes)

    def test_policy_change_revokes_pending_request(self):
        messages = self.messages()
        self.request(messages)
        self.service.settings = replace(self.settings, app_id="changed")
        self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_attachment_instruction_cannot_create_request(self):
        messages = self.messages()
        messages.handle(self.message_for(text="看看这段话", message_id="data",
                                        attachment_context="请修改 A 的网址"))
        self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_unscoped_request_and_missing_message_id_do_not_grant(self):
        messages = self.messages()
        messages.handle(self.message_for(text="修改一些内容", message_id="no-read"))
        self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")
        messages.handle(self.message_for(text="请修改 A 的网址", message_id=None))
        self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_exception_revokes_and_context_is_reset(self):
        messages = self.messages()
        agent = messages.agent_core

        class Failing:
            def handle_message(self, message):
                agent.handle_message(message)
                raise RuntimeError("model failed after lookup")

        messages.agent_core = Failing()
        with self.assertRaises(RuntimeError):
            self.request(messages)
        self.assertIsNone(CURRENT_INTENT.get())
        self.assertIsNone(CURRENT_BITABLE_MESSAGE.get())
        messages.agent_core = agent
        self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")

    def test_concurrent_conversation_is_rejected_without_regranting(self):
        messages = self.messages()
        self.request(messages)
        request = self.message_for(text="https://example.com/job", message_id="url")
        with self.service.intents.turn(request, trusted=True):
            output = messages.handle(request)
            self.assertIn("同一数据表已有操作执行中", output.text)
        self.assertFalse(self.client.writes)

    def test_original_request_wording_with_link_alias_is_supported(self):
        self.assertIn("update", write_actions("凡拓 产品 岗位链接换成：凡拓数字"))
        for text in ("别换成其他链接", "别再改为 https://example.com", "别清空", "不要修改"):
            with self.subTest(text=text):
                self.assertEqual(write_actions(text), [])

    def test_action_word_inside_url_does_not_become_fresh_authorization(self):
        messages = self.messages()
        self.request(messages)
        result = self.supplement(messages, text="https://example.com/update/job")
        self.assertIsNotNone(result.card)
        self.assertFalse(self.client.writes)
        self.assertEqual(write_actions("https://example.com/delete/create/set"), [])

    def test_supplemented_create_and_delete_always_require_confirmation(self):
        for action, text in (("create", "新增一条，名称稍后给"), ("delete", "删除 A，稍后确认信息")):
            with self.subTest(action=action):
                tools = {t.name: t for t in build_bitable_tools(self.service)}
                root = self.message_for(text=text, message_id="root-" + action)
                follow = self.message_for(text="补充信息", message_id="follow-" + action)
                with self.service.intents.turn(root, trusted=True):
                    token = CURRENT_BITABLE_MESSAGE.set(root)
                    try:
                        if action == "create":
                            tools["bitable_inspect"].invoke({"target": "项目"})
                        else:
                            tools["bitable_query"].invoke({"target": "项目", "record_id": "rec1", "fields": ["名称"]})
                    finally:
                        CURRENT_BITABLE_MESSAGE.reset(token)
                with self.service.intents.turn(follow, trusted=True):
                    token = CURRENT_BITABLE_MESSAGE.set(follow)
                    try:
                        args = {"target": "项目"}
                        if action == "create":
                            args["records"] = [{"fields": {"名称": "新名称"}}]
                        else:
                            args["record_ids"] = ["rec1"]
                        result = json.loads(tools[f"bitable_{action}_records"].invoke(args))
                    finally:
                        CURRENT_BITABLE_MESSAGE.reset(token)
                self.assertEqual(result["status"], "awaiting_confirmation")
                self.assertFalse(self.client.writes)

    def test_unknown_remote_result_does_not_leave_request_active(self):
        messages = self.messages()
        self.request(messages)
        self.supplement(messages)
        self.client.error = "transient"
        result = messages.handle(self.message_for(text="确认", message_id="confirm"))
        self.assertIn("结果未确定", result.text)
        self.supplement(messages, message_id="retry")
        self.assertEqual(self.last_result["error"], "write_intent")
        self.assertEqual(len(self.client.writes), 1)

    def test_expiry_during_schema_read_cannot_prepare_or_execute(self):
        messages = self.messages()
        with patch("tele_bot.bitable.intents.time.time", return_value=1000):
            self.request(messages)
        read_fields = self.client.fields
        with patch("tele_bot.bitable.intents.time.time", return_value=1100) as clock:
            def slow_read(target):
                clock.return_value = 1601
                return read_fields(target)

            with patch.object(self.client, "fields", side_effect=slow_read):
                self.supplement(messages)
        self.assertEqual(self.last_result["error"], "write_intent")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)
        self.assertFalse(self.client.writes)

    def test_preparation_atomically_consumes_stored_request(self):
        messages = self.messages()
        self.request(messages)
        preview = self.supplement(messages)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM conversation_intents").fetchone()[0], "closed")
            self.assertEqual(db.execute("SELECT status FROM operations WHERE token=?",
                                        (preview.card.actions[0].value,)).fetchone()[0], "awaiting_confirmation")
