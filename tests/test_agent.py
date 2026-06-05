import unittest

from tele_bot.agents import AgentExecutionResult
from tele_bot.agent import AgentCore
from tele_bot.models import IncomingMessage


class FakeLLMClient:
    def generate_reply(self, prompt: str) -> str:
        return f"reply:{prompt}"


class FakeExecutor:
    def handle(self, message: IncomingMessage) -> AgentExecutionResult:
        return AgentExecutionResult(
            reply_text=f"exec:{message.text}",
            mode="chat",
            model="qwen-plus",
        )


class AgentCoreTests(unittest.TestCase):
    def test_returns_received_message(self) -> None:
        agent = AgentCore(llm_client=FakeLLMClient())
        message = IncomingMessage(
            channel="telegram",
            user_id="42",
            chat_id="1001",
            text="hello",
        )

        response = agent.handle_message(message)

        self.assertEqual(response.channel, "telegram")
        self.assertEqual(response.chat_id, "1001")
        self.assertEqual(response.text, "reply:hello")

    def test_uses_executor_when_available(self) -> None:
        agent = AgentCore(executor=FakeExecutor())
        message = IncomingMessage(
            channel="telegram",
            user_id="42",
            chat_id="1001",
            text="hello",
        )

        response = agent.handle_message(message)

        self.assertEqual(response.text, "exec:hello")


if __name__ == "__main__":
    unittest.main()
