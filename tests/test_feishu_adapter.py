import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tele_bot.agents.feishu_streaming import FeishuProgressReporter
from tele_bot.channels.feishu import FeishuWebhookAdapter


class FakeFeishuAdapter:
    name = "feishu"

    def __init__(self) -> None:
        self.sent = []
        self.edited = []
        self.deleted = []

    def send_text(self, message):
        self.sent.append(message)
        return {"code": 0, "data": {"message_id": "om_1"}}

    def edit_message(self, message_id: str, text: str):
        self.edited.append((message_id, text))
        return {"code": 0}

    def delete_message(self, message_id: str):
        self.deleted.append(message_id)
        return {"code": 0}


class FeishuWebhookAdapterTests(unittest.TestCase):
    def test_url_verification_returns_challenge(self) -> None:
        adapter = FeishuWebhookAdapter(app_id="app", app_secret="secret")

        response = adapter.challenge_response(
            {"type": "url_verification", "challenge": "abc"}
        )

        self.assertEqual(response, {"challenge": "abc"})

    def test_parse_text_message_event(self) -> None:
        adapter = FeishuWebhookAdapter(app_id="app", app_secret="secret")
        payload = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"user_id": "ou_1", "open_id": "ou_open"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": " hello "}),
                },
            },
        }

        incoming = adapter.parse_incoming(payload)

        self.assertIsNotNone(incoming)
        self.assertEqual(incoming.channel, "feishu")
        self.assertEqual(incoming.user_id, "ou_1")
        self.assertEqual(incoming.chat_id, "oc_1")
        self.assertEqual(incoming.text, "hello")

    def test_parse_message_without_chat_id_is_ignored(self) -> None:
        adapter = FeishuWebhookAdapter(app_id="app", app_secret="secret")
        payload = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_open"}},
                "message": {
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": "hello"}),
                },
            },
        }

        self.assertIsNone(adapter.parse_incoming(payload))

    def test_progress_reporter_edits_one_message_and_finishes_in_place(self) -> None:
        adapter = FakeFeishuAdapter()
        reporter = FeishuProgressReporter(adapter=adapter, chat_id="oc_1")

        message_id = reporter.start()
        updated = reporter.update("thinking")
        reporter.update("calling tool: search")
        finished = reporter.finish("最终结果")

        self.assertEqual(message_id, "om_1")
        self.assertTrue(updated)
        self.assertEqual(len(adapter.sent), 1)
        self.assertTrue(finished)
        self.assertEqual(adapter.deleted, [])
        self.assertEqual(
            adapter.edited,
            [
                ("om_1", "thinking"),
                ("om_1", "calling tool: search"),
                ("om_1", "最终结果"),
            ],
        )

    @patch("tele_bot.channels.feishu.httpx.put")
    def test_edit_message_uses_feishu_message_endpoint(self, patch_request) -> None:
        adapter = FeishuWebhookAdapter(app_id="app", app_secret="secret")
        adapter._access_token = "token"
        adapter._token_expires_at = 9999999999
        response = Mock()
        response.is_success = True
        response.json.return_value = {"code": 0}
        patch_request.return_value = response

        result = adapter.edit_message("om_1", "正在调用工具")

        self.assertEqual(result["code"], 0)
        self.assertEqual(patch_request.call_args.args[0], "https://open.feishu.cn/open-apis/im/v1/messages/om_1")
        self.assertEqual(patch_request.call_args.kwargs["json"]["msg_type"], "text")

    @patch("tele_bot.channels.feishu.httpx.delete")
    def test_delete_message_uses_feishu_message_endpoint(self, delete_request) -> None:
        adapter = FeishuWebhookAdapter(app_id="app", app_secret="secret")
        adapter._access_token = "token"
        adapter._token_expires_at = 9999999999
        response = Mock()
        response.is_success = True
        response.json.return_value = {"code": 0}
        delete_request.return_value = response

        result = adapter.delete_message("om_1")

        self.assertEqual(result["code"], 0)
        self.assertEqual(delete_request.call_args.args[0], "https://open.feishu.cn/open-apis/im/v1/messages/om_1")

    @patch("tele_bot.channels.feishu.httpx.post")
    def test_send_file_uploads_then_sends_file_message(self, post) -> None:
        adapter = FeishuWebhookAdapter(app_id="app", app_secret="secret")
        adapter._access_token = "token"
        adapter._token_expires_at = 9999999999
        upload_response = Mock()
        upload_response.json.return_value = {"code": 0, "data": {"file_key": "file_1"}}
        send_response = Mock()
        send_response.json.return_value = {"code": 0, "data": {"message_id": "om_1"}}
        post.side_effect = [upload_response, send_response]

        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.txt"
            file_path.write_bytes(b"hello")
            result = adapter.send_file("oc_1", file_path)

        self.assertEqual(result["code"], 0)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["data"]["file_type"], "stream")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["msg_type"], "file")
        self.assertEqual(
            json.loads(post.call_args_list[1].kwargs["json"]["content"]),
            {"file_key": "file_1"},
        )

    def test_upload_file_rejects_files_over_feishu_limit(self) -> None:
        adapter = FeishuWebhookAdapter(app_id="app", app_secret="secret")

        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "large.bin"
            file_path.write_bytes(b"x" * (30 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, "30 MB"):
                adapter.upload_file(file_path)


if __name__ == "__main__":
    unittest.main()
