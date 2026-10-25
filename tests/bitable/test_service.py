from copy import deepcopy
from dataclasses import replace
import json
import time

from tele_bot.bitable.models import BitableError
from tele_bot.bitable.policy import BitablePolicy
from tele_bot.bitable.service import BitableService
from tele_bot.router.bitable_service import BitableCommandService
from tests.bitable.support import ServiceCase, ENTRIES


class BaseServiceTests(ServiceCase):
    def test_live_style_null_field_property_is_valid(self):
        self.client.definitions[0]["property"] = None
        result = self.service.inspect("项目", self.message)
        self.assertEqual(result["fields"][0]["options"], [])
        self.assertTrue(result["fields"][0]["writable"])

    def test_registry_rejects_untrusted_inputs_and_opens_registered_targets_to_all_callers(self):
        for message in [None, self.message_for(channel="api")]:
            with self.subTest(message=message), self.assertRaises(BitableError):
                self.service.query("项目", message)
        self.assertFalse(self.client.calls)
        self.assertEqual(self.service.inspect("", self.message)["targets"][0]["alias"], "项目")
        self.assertEqual(self.service.inspect("", self.message_for(user_id="other", chat_id="other"))["targets"][0]["alias"], "项目")
        for message in [self.message_for(user_id="other"), self.message_for(chat_id="other")]:
            self.assertEqual(self.service.query("项目", message, record_id="rec1")["records"][0]["record_id"], "rec1")
        self.client.calls.clear()
        with self.assertRaises(BitableError) as error:
            self.service.query("未登记表", self.message)
        self.assertEqual(error.exception.kind, "not_found")
        self.assertFalse(self.client.calls)

    def test_pagination_scope_budget_duplicates_and_projection(self):
        result = self.service.query("项目", self.message, fields=["名称"], limit=1)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["records"][0]["fields"], {"名称": "A"})
        next_result = self.service.query("项目", self.message, fields=["名称"], limit=1, cursor=result["next_cursor"])
        self.assertEqual(next_result["records"][0]["record_id"], "rec2")
        self.assertFalse(next_result["has_more"])
        for kwargs in [{"fields": ["数量"]}, {"fields": ["名称"], "message": self.message_for(user_id="other")}]:
            args = {"target": "项目", "message": self.message, "cursor": result["next_cursor"], **kwargs}
            with self.assertRaises(BitableError):
                self.service.query(**args)
        self.client.rows = {"rec1": self.client.rows["rec1"], "duplicate": self.client.rows["rec1"]}
        with self.assertRaises(BitableError):
            self.service.query("项目", self.message, cursor=result["next_cursor"], fields=["名称"])

    def test_record_limit_and_long_field_are_explicit(self):
        self.service.settings = replace(self.settings, max_query_records=1)
        result = self.service.query("项目", self.message, limit=1)
        self.assertTrue(result["limit_reached"])
        self.assertIsNone(result["next_cursor"])
        self.client.rows["rec1"]["fields"]["名称"] = "x" * 25000
        result = self.service.query("项目", self.message, record_id="rec1")
        self.assertTrue(result["truncated"])
        self.assertIn("名称", result["records"][0]["omitted_fields"])
        chunk = self.service.query("项目", self.message, record_id="rec1", fields=["名称"])
        follow = self.service.query("项目", self.message, record_id="rec1", fields=["名称"],
                                    start_char=chunk["field_content"]["next_start_char"])
        self.assertEqual(chunk["field_content"]["value_hash"], follow["field_content"]["value_hash"])
        self.assertEqual(follow["field_content"]["start_char"], 4000)

    def test_create_replay_new_message_and_update_clear(self):
        payload = [{"fields": {"名称": "C", "数量": 0, "复选": False}}]
        first = self.service.write("create", "项目", self.message, payload)
        self.assertEqual(first["status"], "succeeded")
        repeated = self.service.write("create", "项目", self.message, payload)
        self.assertEqual(first, repeated)
        self.assertEqual(len(self.client.writes), 1)
        second = self.service.write("create", "项目", self.message_for(message_id="msg2"), payload)
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        result = self.service.write("update", "项目", self.message, [
            {"record_id": "rec1", "fields": {"数量": 3}, "clear_fields": ["名称"]}])
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(self.client.rows["rec1"]["fields"]["名称"])
        self.assertEqual(self.client.rows["rec1"]["fields"]["状态"], "进行中")

    def test_confirmation_bound_to_user_policy_expiry_and_exact_frozen_rows(self):
        result = self.service.write("delete", "项目", self.message, ["rec1"])
        self.assertEqual(result["status"], "awaiting_confirmation")
        token = result["confirmation_token"]
        with self.assertRaises(BitableError):
            self.service.confirm(token, self.message_for(user_id="other"))
        restarted = BitableService(self.settings, self.client, self.policy, self.store)
        confirmed = restarted.confirm(token, self.message)
        self.assertEqual(confirmed["status"], "succeeded")
        self.assertEqual(restarted.confirm(token, self.message), confirmed)
        self.assertEqual(len(self.client.writes), 1)
        self.assertIn("rec2", self.client.rows)
        result = self.service.write("delete", "项目", self.message_for(message_id="msg2"), ["rec2"])
        with self.store.connect() as db:
            db.execute("UPDATE operations SET expires=? WHERE id=?", (time.time() - 1, result["operation_id"]))
        self.assertEqual(self.service.confirm(result["confirmation_token"], self.message)["status"], "expired")

    def test_changed_schema_or_record_prevents_confirmation_and_cancel_does_not_write(self):
        result = self.service.write("delete", "项目", self.message, ["rec1"])
        self.client.rows["rec1"]["fields"]["数量"] = 99
        self.assertEqual(self.service.confirm(result["confirmation_token"], self.message)["status"], "conflict")
        result = self.service.write("delete", "项目", self.message_for(message_id="msg2"), ["rec1"])
        self.client.definitions[0]["field_name"] = "改名"
        self.assertEqual(self.service.confirm(result["confirmation_token"], self.message)["status"], "conflict")
        result = self.service.write("delete", "项目", self.message_for(message_id="msg3"), ["rec2"])
        self.assertEqual(self.service.confirm(result["confirmation_token"], self.message, cancel=True)["status"], "cancelled")
        self.assertFalse(self.client.writes)

    def test_unknown_write_and_failed_verification_never_replay(self):
        self.client.error = "transient"
        payload = [{"fields": {"名称": "C"}}]
        first = self.service.write("create", "项目", self.message, payload)
        self.assertEqual(first["status"], "unknown")
        self.client.error = None
        self.assertEqual(self.service.write("create", "项目", self.message, payload), first)
        self.assertEqual(len(self.client.writes), 1)
        with self.assertRaises(BitableError):
            self.service.write("create", "项目", self.message, [{"fields": {"名称": "改写内容再次创建"}}])
        self.client.verify_error = True
        second = self.service.write("create", "项目", self.message_for(message_id="msg2"), payload)
        self.assertEqual(second["status"], "unknown")
        self.assertTrue(second["submitted"])

    def test_explicit_write_permission_denial_is_failed_and_checkbox_clear_is_false(self):
        self.client.error = "unauthorized"
        result = self.service.write("create", "项目", self.message, [{"fields": {"名称": "C"}}])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "unauthorized")
        self.assertEqual(self.service._encode(self.client.definitions, {}, ["复选"]), {"复选": False})

    def test_field_values_invalid_operations_and_intent_do_not_write(self):
        for values in [{"数量": True}, {"状态": "新选项"}, {"公式": 1}, {"名称": None}, {"日期": -1}]:
            with self.subTest(values=values), self.assertRaises(BitableError):
                self.service.write("create", "项目", self.message, [{"fields": values}])
        for text in ["查看项目", "不要删除或修改项目"]:
            with self.assertRaises(BitableError):
                self.service.write("delete", "项目", self.message_for(text=text), ["rec1"])
        self.assertFalse(self.client.writes)
        encoded = self.service._encode(self.client.definitions, {"日期": "2026-10-20", "数量": 0, "复选": False})
        self.assertEqual(encoded["日期"], 1792425600000)
        with self.assertRaises(BitableError):
            self.service.write("create", "项目", self.message_for(text="删除 A"), [{"fields": {"名称": "C"}}])

    def test_policy_version_and_cross_process_lease(self):
        result = self.service.write("delete", "项目", self.message, ["rec1"])
        changed = deepcopy(ENTRIES)
        changed[0]["confirm_all"] = True
        self.service.policy = BitablePolicy(changed)
        with self.assertRaises(BitableError):
            self.service.confirm(result["confirmation_token"], self.message)
        with self.store.lease("table"):
            with self.assertRaises(BitableError):
                with self.store.lease("table"):
                    self.fail("concurrent lease acquired")

    def test_explicit_status_route_does_not_execute_or_leak_other_users(self):
        result = self.service.write("delete", "项目", self.message, ["rec1"])
        commands = BitableCommandService(self.service)
        output = commands.handle(self.message_for(text="/bitable status " + result["operation_id"]))
        self.assertIsNotNone(output.card)
        denied = commands.handle(self.message_for(user_id="other", text="/bitable status " + result["operation_id"]))
        self.assertNotIn("rec1", denied.text)
        self.assertFalse(self.client.writes)

    def test_many_updates_freeze_before_values_and_persist_same_client_token(self):
        payload = [{"record_id": "rec1", "fields": {"数量": 5}}, {"record_id": "rec2", "fields": {"数量": 6}}]
        prepared = self.service.write("update", "项目", self.message, payload)
        self.assertEqual(prepared["status"], "awaiting_confirmation")
        self.assertEqual(prepared["preview"]["before"]["rec1"]["fields"]["数量"], 0)
        result = self.service.confirm(prepared["confirmation_token"], self.message)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.client.rows["rec2"]["fields"]["数量"], 6)
        row = self.store.get(prepared["operation_id"])
        self.assertEqual(json.loads(row["plan"])["records"][0]["fields"], {"数量": 5})

    def test_policy_change_cannot_replay_the_same_event_as_new_write(self):
        payload = [{"fields": {"名称": "C"}}]
        self.service.write("create", "项目", self.message, payload)
        changed = deepcopy(ENTRIES)
        changed[0]["confirm_all"] = True
        self.service.policy = BitablePolicy(changed)
        with self.assertRaises(BitableError):
            self.service.write("create", "项目", self.message, payload)
        self.assertEqual(len(self.client.writes), 1)

    def test_preview_card_overflow_is_rejected_without_hidden_writes(self):
        self.client.rows["rec1"]["fields"]["名称"] = "long" * 1500
        with self.assertRaises(BitableError):
            self.service.write("delete", "项目", self.message, ["rec1"])
        self.assertFalse(self.client.writes)
