"""Exercise iteration limits using the real graph and deterministic tools."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from tele_bot.workflows.react_graph import PROGRESS_CONTEXTVAR, build_react_graph
from tele_bot.workflows.context_manager import ContextManager, ContextPolicy
from tele_bot.persistence.sqlite_checkpointer import build_sqlite_saver


class LoopingModel:
    def __init__(self, finish_at=None):
        self.calls = 0
        self.finish_at = finish_at

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.calls == self.finish_at:
            return AIMessage(content="done")
        return AIMessage(content="", tool_calls=[{
            "name": "read_page", "args": {}, "id": f"page-{self.calls}",
            "type": "tool_call",
        }])


@tool
def read_page() -> str:
    """Read the next deterministic page."""
    return "page read"


class ReactGraphTests(unittest.TestCase):
    def test_history_summary_reports_progress_before_waiting_for_model(self):
        events = []

        class SummaryModel:
            def invoke(self, messages):
                events.append("summary API")
                return AIMessage(content="保留用户要求，正在发布三个原文。")

        class Model:
            def invoke(self, messages, config=None):
                events.append("main API")
                return AIMessage(content="done")

        policy = ContextPolicy(hard_tokens=2400, trigger_tokens=1800, target_tokens=1200,
                               summary_tokens=180, summary_input_tokens=900, keep_recent=4)
        history = [message for i in range(20) for message in (
            HumanMessage(content="旧需求" * 80, id=f"u{i}"),
            AIMessage(content="旧回答" * 80, id=f"a{i}"))]
        history.append(HumanMessage(content="将三个文档作为 telebot 子文档推出去", id="goal"))
        graph = build_react_graph(Model(), [], context_manager=ContextManager(
            summarizer=SummaryModel(), policy=policy))
        token = PROGRESS_CONTEXTVAR.set(events.append)
        try:
            graph.invoke({"messages": history, "iterations": 0},
                         config={"configurable": {"thread_id": "summary-progress"}})
        finally:
            PROGRESS_CONTEXTVAR.reset(token)
        first_summary = events.index("summary API")
        self.assertTrue(any("压缩历史" in event for event in events[:first_summary]), events)
        for index, event in enumerate(events):
            if event == "summary API":
                self.assertIn("生成历史摘要", events[index - 1])
        main_index = events.index("main API")
        self.assertTrue(any("历史上下文整理完成" in event for event in events[:main_index]), events)
        self.assertEqual(events[main_index - 1], "正在思考...")

    def test_summary_survives_api_timeout_and_process_restart(self):
        class SummaryModel:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                return AIMessage(content="用户要求发布原文；源文件不可修改。")

        class FailingModel:
            def invoke(self, messages, config=None):
                raise TimeoutError("main API timeout")

        class SuccessModel:
            def invoke(self, messages, config=None):
                self.messages = messages
                return AIMessage(content="done", usage_metadata={
                    "input_tokens": 2000, "output_tokens": 1, "total_tokens": 2001})

        policy = ContextPolicy(hard_tokens=2400, trigger_tokens=1800, target_tokens=1200,
                               summary_tokens=180, summary_input_tokens=900, keep_recent=4)
        summary_model = SummaryModel()
        config = {"configurable": {"thread_id": "persisted"}}
        history = [message for i in range(20) for message in (
            HumanMessage(content="旧需求" * 80, id=f"u{i}"),
            AIMessage(content="旧回答" * 80, id=f"a{i}"))]
        history.append(HumanMessage(content="完整发布三个文件", id="goal"))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conversation.sqlite"
            saver = build_sqlite_saver(path)
            graph = build_react_graph(FailingModel(), [], checkpointer=saver,
                                      context_manager=ContextManager(summarizer=summary_model, policy=policy))
            with self.assertRaises(TimeoutError):
                graph.invoke({"messages": history, "iterations": 0}, config=config)
            state = graph.get_state(config).values
            self.assertEqual(len(state["messages"]), len(history))
            self.assertTrue(state["summary_until_message_id"])
            self.assertIn("源文件不可修改", state["context_summary"])
            self.assertLessEqual(state["context_stats"]["input_tokens"], 2400)
            calls = summary_model.calls
            saver.conn.close()
            reopened = build_sqlite_saver(path)
            success = SuccessModel()
            graph = build_react_graph(success, [], checkpointer=reopened,
                                      context_manager=ContextManager(summarizer=summary_model, policy=policy))
            result = graph.invoke(None, config=config)
            self.assertEqual(summary_model.calls, calls)
            self.assertEqual(len(result["messages"]), len(history) + 1)
            self.assertIn("完整发布三个文件", [m.content for m in success.messages])
            self.assertGreater(result["token_scale"], 1)
            reopened.conn.close()

    def test_task_can_finish_on_fifteenth_model_round(self):
        model = LoopingModel(finish_at=15)
        graph = build_react_graph(model, [read_page])
        result = graph.invoke({"messages": [HumanMessage(content="read document")],
                               "iterations": 0},
                              config={"configurable": {"thread_id": "finish"}})
        self.assertEqual(model.calls, 15)
        self.assertEqual(result["messages"][-1].content, "done")

    def test_unfinished_task_gets_fifteen_round_message(self):
        model = LoopingModel()
        graph = build_react_graph(model, [read_page])
        result = graph.invoke({"messages": [HumanMessage(content="read document")],
                               "iterations": 0},
                              config={"configurable": {"thread_id": "limit"}})
        self.assertEqual(model.calls, 15)
        self.assertIn("(15 轮)", result["messages"][-1].content)
        self.assertEqual(sum(isinstance(m, ToolMessage) for m in result["messages"]), 14)


if __name__ == "__main__":
    unittest.main()
