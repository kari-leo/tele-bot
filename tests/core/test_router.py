import unittest

from tele_bot.router import AgentMode, ConversationState, Router


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = Router()
        self.state = ConversationState(chat_id="1001")

    def test_defaults_to_chat_mode(self) -> None:
        decision = self.router.route("你好，今天怎么样", self.state)

        self.assertEqual(decision.mode, AgentMode.CHAT)
        self.assertFalse(decision.allow_tools)
        self.assertFalse(decision.allow_markdown)

    def test_explicit_markdown_request_enables_markdown(self) -> None:
        decision = self.router.route("请整理成 Markdown 文档并保存成 .md 文件", self.state)

        self.assertEqual(decision.mode, AgentMode.MARKDOWN)
        self.assertTrue(decision.allow_markdown)
        self.assertTrue(decision.save_markdown)

    def test_reasoning_request_uses_reasoning_model(self) -> None:
        decision = self.router.route("帮我做一个 CUDA 架构设计分析", self.state)

        self.assertEqual(decision.mode, AgentMode.REASONING)
        self.assertEqual(decision.model, "qwen3-max")

    def test_search_request_requires_tools(self) -> None:
        decision = self.router.route("帮我搜索一下 OpenAI Responses API 最新文档", self.state)

        self.assertEqual(decision.mode, AgentMode.CHAT)
        self.assertTrue(decision.allow_tools)
        self.assertTrue(decision.search_required)


if __name__ == "__main__":
    unittest.main()