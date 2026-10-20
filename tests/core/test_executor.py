"""ReactAgentExecutor tool calls, authorization and error handling."""

import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.models import IncomingMessage
from tele_bot.workflows.react_graph import PROGRESS_CONTEXTVAR, build_react_graph


class ReactAgentExecutorTests(unittest.TestCase):
    def message(self, text="将对应三个本地文档作为 telebot 子文档推出去"):
        return IncomingMessage(channel="feishu", user_id="user", chat_id="chat", text=text)

    def test_nonempty_but_incomplete_publication_cannot_claim_three_documents_completed(self):
        @tool
        def blog_push() -> str:
            """Return exactly one confirmed publication."""
            return "git push 已完成。\n文件：projectDocuments/telebot/one.md\n"

        class Model:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages, config=None):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{
                        "name": "blog_push", "args": {}, "id": "one", "type": "tool_call"}])
                return AIMessage(content="三个文档已经全部发布成功")

        model = Model()
        result = ReactAgentExecutor(graph=build_react_graph(model, [blog_push]), model_name="test").handle(self.message())
        self.assertEqual(model.calls, 3)
        self.assertIn("1/3", result.reply_text)
        self.assertIn("其余 2 份尚未确认完成", result.reply_text)
        self.assertNotIn("全部发布成功", result.reply_text)

    def test_empty_response_after_one_push_recovers_and_completes_remaining_documents(self):
        calls = []
        progress = []

        @tool
        def blog_publish_from_file(slug: str) -> str:
            """Return the deterministic publication result."""
            calls.append(slug)
            return f"git push 已完成。\n文件：projectDocuments/telebot/{slug}.md\n"

        class Model:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages, config=None):
                self.calls += 1
                if self.calls == 2:
                    return AIMessage(content="", response_metadata={"finish_reason": "stop"},
                                     usage_metadata={"input_tokens": 100, "output_tokens": 0, "total_tokens": 100})
                if self.calls == 4:
                    return AIMessage(content="三个文档都已推送。")
                slugs = ["scheduled-prd"] if self.calls == 1 else ["product-case", "prd-mvp"]
                return AIMessage(content="", tool_calls=[{
                    "name": "blog_publish_from_file", "args": {"slug": slug},
                    "id": f"call-{slug}", "type": "tool_call"} for slug in slugs])

        model = Model()
        graph = build_react_graph(model, [blog_publish_from_file])
        executor = ReactAgentExecutor(graph=graph, model_name="test")
        token = PROGRESS_CONTEXTVAR.set(progress.append)
        try:
            result = executor.handle(self.message())
        finally:
            PROGRESS_CONTEXTVAR.reset(token)
        self.assertEqual(result.reply_text, "三个文档都已推送。")
        self.assertEqual(calls, ["scheduled-prd", "product-case", "prd-mvp"])
        self.assertTrue(any("未返回有效内容" in item for item in progress), progress)

    def test_repeated_empty_response_is_bounded_and_does_not_report_historical_success(self):
        class Model:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages, config=None):
                self.calls += 1
                return AIMessage(content="   ")

        model = Model()
        graph = build_react_graph(model, [])
        config = {"configurable": {"thread_id": "chat"}}
        graph.update_state(config, {"messages": [HumanMessage(content="old request"),
                                                 AIMessage(content="历史任务已全部推送成功")], "iterations": 0})
        executor = ReactAgentExecutor(graph=graph, model_name="test")
        first = executor.handle(self.message())
        self.assertEqual(model.calls, 2)
        self.assertNotIn("无回复", first.reply_text)
        self.assertNotIn("历史任务已全部推送成功", first.reply_text)
        self.assertIn("尚未确认完成", first.reply_text)
        executor.handle(self.message("重新推送这三个文档"))
        self.assertEqual(model.calls, 4)

    def test_timeout_during_empty_response_recovery_preserves_successful_tool_receipt(self):
        @tool
        def blog_push() -> str:
            """Return a successful push receipt."""
            return "git push 已完成。\n文件：projectDocuments/telebot/scheduled.md\n"

        class Model:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages, config=None):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{
                        "name": "blog_push", "args": {}, "id": "push", "type": "tool_call"}])
                if self.calls == 2:
                    return AIMessage(content="")
                raise TimeoutError("API timed out")

        executor = ReactAgentExecutor(graph=build_react_graph(Model(), [blog_push]), model_name="test")
        result = executor.handle(self.message())
        self.assertIn("scheduled.md", result.reply_text)
        self.assertIn("尚未确认完成", result.reply_text)
        self.assertIn("timed out", result.reply_text)
        self.assertIn("1/3", result.reply_text)

    def test_empty_reply_after_complete_batch_reports_verified_three_of_three(self):
        @tool
        def blog_publish_files() -> str:
            """Return verified batch publication receipts."""
            return json.dumps({"status": "pushed", "requested_count": 3,
                               "written_count": 3, "pushed_count": 3,
                               "files": [{"path": f"D:/site/telebot/doc-{i}.md", "status": "pushed"}
                                         for i in range(3)]})

        class Model:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages, config=None):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{
                        "name": "blog_publish_files", "args": {}, "id": "batch", "type": "tool_call"}])
                return AIMessage(content="")

        result = ReactAgentExecutor(graph=build_react_graph(Model(), [blog_publish_files]),
                                    model_name="test").handle(self.message())
        self.assertIn("3/3", result.reply_text)
        self.assertNotIn("尚未确认完成", result.reply_text)
        for i in range(3):
            self.assertIn(f"doc-{i}.md", result.reply_text)



if __name__ == "__main__":
    unittest.main()
