import unittest
from unittest.mock import patch

from tele_bot.agent import AgentCore
from tele_bot.models import IncomingMessage
from tele_bot.service import MessageService


class FakeLLMClient:
    def generate_reply(self, prompt: str) -> str:
        return f"reply:{prompt}"


class MessageServiceTests(unittest.TestCase):
    def test_service_delegates_to_agent_core(self) -> None:
        service = MessageService(agent_core=AgentCore(llm_client=FakeLLMClient()))
        message = IncomingMessage(
            channel="feishu",
            user_id="7",
            chat_id="99",
            text="ping",
        )

        response = service.handle(message)

        self.assertEqual(response.text, "reply:ping")
        self.assertEqual(response.chat_id, "99")

    def test_streaming_status_is_cleared_after_agent_returns(self) -> None:
        class FakeFeishuAdapter:
            name = "feishu"

        message = IncomingMessage(
            channel="feishu",
            user_id="7",
            chat_id="99",
            text="ping",
        )
        with patch("tele_bot.service.FeishuProgressReporter") as reporter_type:
            response = MessageService(
                agent_core=AgentCore(llm_client=FakeLLMClient()),
                feishu_adapter=FakeFeishuAdapter(),
                streaming_enabled=True,
            ).handle(message)

        reporter = reporter_type.return_value
        reporter.start.assert_called_once_with()
        reporter.finish.assert_called_once_with("reply:ping")
        self.assertEqual(response.text, "reply:ping")
        self.assertTrue(response.already_sent)


if __name__ == "__main__":
    unittest.main()
