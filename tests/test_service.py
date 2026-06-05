import unittest

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
            channel="telegram",
            user_id="7",
            chat_id="99",
            text="ping",
        )

        response = service.handle(message)

        self.assertEqual(response.text, "reply:ping")
        self.assertEqual(response.chat_id, "99")


if __name__ == "__main__":
    unittest.main()
