import unittest
from unittest.mock import patch

from tele_bot.channels.telegram import TelegramAdapter
from tele_bot.models import OutgoingMessage


class TelegramAdapterTests(unittest.TestCase):
    def test_parse_incoming_message(self) -> None:
        adapter = TelegramAdapter(bot_token=None)
        payload = {
            "message": {
                "text": "你好",
                "chat": {"id": 1001},
                "from": {"id": 42},
            }
        }

        incoming = adapter.parse_incoming(payload)

        self.assertIsNotNone(incoming)
        self.assertEqual(incoming.user_id, "42")
        self.assertEqual(incoming.chat_id, "1001")
        self.assertEqual(incoming.text, "你好")

    def test_whitelist_blocks_unknown_user(self) -> None:
        adapter = TelegramAdapter(bot_token=None, allowed_user_ids={"42"})
        payload = {
            "message": {
                "text": "hello",
                "chat": {"id": 1001},
                "from": {"id": 99},
            }
        }

        incoming = adapter.parse_incoming(payload)

        self.assertIsNotNone(incoming)
        self.assertFalse(adapter.is_allowed(incoming))

    def test_build_send_payload(self) -> None:
        adapter = TelegramAdapter(bot_token=None)
        outgoing = OutgoingMessage(channel="telegram", chat_id="1001", text="收到")

        payload = adapter.build_send_payload(outgoing)

        self.assertEqual(payload, {"chat_id": "1001", "text": "收到"})

    def test_build_get_updates_payload(self) -> None:
        adapter = TelegramAdapter(bot_token="token")

        payload = adapter.build_get_updates_payload(offset=11, timeout=20)

        self.assertEqual(payload, {"offset": 11, "timeout": 20})

    def test_builds_proxy_opener_when_proxy_url_is_set(self) -> None:
        adapter = TelegramAdapter(bot_token="token", proxy_url="http://127.0.0.1:7890")

        with patch("urllib.request.build_opener") as build_opener:
            build_opener.return_value = object()

            adapter._build_opener()

        build_opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()