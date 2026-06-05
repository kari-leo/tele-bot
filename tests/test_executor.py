import unittest

from tele_bot.agents import ControlledAgentExecutor
from tele_bot.models import IncomingMessage
from tele_bot.router import InMemoryConversationStateStore, Router, RouterConfig


class FakeLLMResponse:
    def __init__(self, reply_text: str, report_path: str | None = None) -> None:
        self.reply_text = reply_text
        self.model = "qwen-plus"
        self.report_path = report_path
        self.tool_result_summary = "搜索 `openai` 首条结果：OpenAI https://openai.com"
        self.used_tool = True


class FakeLLMClient:
    def __init__(self) -> None:
        self.calls = []

    def generate_response(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["save_markdown"]:
            return FakeLLMResponse(
                reply_text="摘要\n\n报告: reports/demo.md",
                report_path="reports/demo.md",
            )
        return FakeLLMResponse(reply_text="普通回复")


class ControlledAgentExecutorTests(unittest.TestCase):
    def test_executor_routes_markdown_and_persists_state(self) -> None:
        llm_client = FakeLLMClient()
        state_store = InMemoryConversationStateStore()
        executor = ControlledAgentExecutor(
            llm_client=llm_client,
            router=Router(
                config=RouterConfig(
                    default_model="qwen-plus",
                    reasoning_model="qwen3-max",
                )
            ),
            state_store=state_store,
        )

        result = executor.handle(
            IncomingMessage(
                channel="telegram",
                user_id="42",
                chat_id="1001",
                text="请整理成 Markdown 文档并保存成 .md 文件",
            )
        )

        self.assertEqual(result.report_path, "reports/demo.md")
        self.assertTrue(llm_client.calls[0]["allow_markdown"])
        self.assertTrue(llm_client.calls[0]["save_markdown"])

        state = state_store.load("1001")
        self.assertEqual(state.mode.value, "markdown")
        self.assertEqual(state.report_paths, ["reports/demo.md"])
        self.assertEqual(len(state.turns), 2)


if __name__ == "__main__":
    unittest.main()