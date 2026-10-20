"""Message contracts, AgentCore delegation and application message dispatch."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tele_bot.agent import AgentCore
from tele_bot.agents import AgentExecutionResult
from tele_bot.models import IncomingMessage, MessageType, OutgoingMessage
from tele_bot.router.codex_service import CodexCommandService
from tele_bot.service import MessageService
from tele_bot.tools.codex_runner import CodexResult


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
            channel="feishu",
            user_id="42",
            chat_id="1001",
            text="hello",
        )

        response = agent.handle_message(message)

        self.assertEqual(response.channel, "feishu")
        self.assertEqual(response.chat_id, "1001")
        self.assertEqual(response.text, "reply:hello")

    def test_uses_executor_when_available(self) -> None:
        agent = AgentCore(executor=FakeExecutor())
        message = IncomingMessage(
            channel="feishu",
            user_id="42",
            chat_id="1001",
            text="hello",
        )

        response = agent.handle_message(message)

        self.assertEqual(response.text, "exec:hello")


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

    def test_pending_codex_confirmation_does_not_reach_agent(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.calls = []

            def run(
                self, mode: str, prompt: str, workspace: str | None = None
            ) -> CodexResult:
                self.calls.append((mode, prompt, workspace))
                return CodexResult(
                    mode, workspace or "D:/files_data", 0, "done", "", False
                )

        class FakeAgent:
            def __init__(self) -> None:
                self.calls = 0

            def handle_message(self, message: IncomingMessage):
                self.calls += 1
                return message

        class Adapter:
            name = "feishu"

        runner = FakeRunner()
        agent = FakeAgent()
        codex = CodexCommandService(runner)
        codex.create_apply_request(
            chat_id="99",
            task="修改 README",
            workspace_hint="D:/files_data",
        )

        with patch("tele_bot.service.FeishuProgressReporter") as reporter_type:
            response = MessageService(
                agent_core=agent,
                feishu_adapter=Adapter(),
                streaming_enabled=True,
                codex_service=codex,
            ).handle(IncomingMessage("feishu", "7", "99", "确认"))

        self.assertIn("Codex apply 完成", response.text)
        self.assertEqual(agent.calls, 0)
        self.assertEqual(len(runner.calls), 1)
        reporter_type.return_value.start.assert_called_once_with()
        reporter_type.return_value.finish.assert_called_once_with(response.text)
        self.assertTrue(response.already_sent)


class MessageContractTests(unittest.TestCase):
    def test_legacy_positional_message_contract_remains_compatible(self) -> None:
        incoming = IncomingMessage("feishu", "user", "chat", "hello")
        outgoing = OutgoingMessage("feishu", "chat", "world", True)

        self.assertEqual(incoming.message_type, MessageType.TEXT)
        self.assertEqual(incoming.attachments, ())
        self.assertIsNone(incoming.message_id)
        self.assertTrue(outgoing.already_sent)
        self.assertIsNone(outgoing.card)
