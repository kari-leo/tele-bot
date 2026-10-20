import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.router import AgentMode, JsonFileConversationStateStore


class JsonFileConversationStateStoreTests(unittest.TestCase):
    def test_persists_mode_turns_and_report_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = JsonFileConversationStateStore(directory=Path(temp_dir))
            store.set_mode("chat-1", AgentMode.MARKDOWN)
            store.append_turn("chat-1", role="user", content="请生成 md")
            store.append_turn("chat-1", role="assistant", content="# 标题")
            store.set_tool_summary("chat-1", "搜索结果摘要")
            store.add_report_path("chat-1", "reports/demo.md")

            reloaded = JsonFileConversationStateStore(directory=Path(temp_dir)).load("chat-1")

        self.assertEqual(reloaded.mode, AgentMode.MARKDOWN)
        self.assertEqual(len(reloaded.turns), 2)
        self.assertEqual(reloaded.last_tool_result_summary, "搜索结果摘要")
        self.assertEqual(reloaded.report_paths, ["reports/demo.md"])

    def test_chat_context_filters_markdown_turns(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = JsonFileConversationStateStore(directory=Path(temp_dir))
            store.append_turn("chat-2", role="user", content="你好")
            store.append_turn("chat-2", role="assistant", content="# 报告")
            store.append_turn("chat-2", role="assistant", content="普通回复")

            turns = store.build_prompt_context("chat-2", AgentMode.CHAT)

        self.assertEqual([turn.content for turn in turns], ["你好", "普通回复"])


if __name__ == "__main__":
    unittest.main()