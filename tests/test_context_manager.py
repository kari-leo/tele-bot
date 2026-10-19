import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tele_bot.workflows.context_manager import ContextManager, ContextPolicy


class Summarizer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def invoke(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise TimeoutError("summary timeout")
        return AIMessage(content="用户要发布三个文档；保留源文件，尚未发布。")


class ContextManagerTests(unittest.TestCase):
    def policy(self):
        return ContextPolicy(hard_tokens=2400, trigger_tokens=1800,
                             target_tokens=1200, summary_tokens=180,
                             summary_input_tokens=900, keep_recent=4)

    def history(self):
        result = []
        for i in range(20):
            result.extend([HumanMessage(content="历史问题" * 80, id=f"u{i}"),
                           AIMessage(content="历史回答" * 80, id=f"a{i}")])
        result.append(HumanMessage(content="发布这三个文档", id="current"))
        return result

    def test_budgeted_view_preserves_full_history_and_incremental_cursor(self):
        summarizer = Summarizer()
        manager = ContextManager(summarizer=summarizer, policy=self.policy())
        history = self.history()
        original = [m.content for m in history]
        view = manager.prepare(history, system_prompt="system")
        self.assertLessEqual(view["context_stats"]["input_tokens"], 2400)
        self.assertIn("发布这三个文档", [m.content for m in view["llm_messages"]])
        self.assertEqual([m.content for m in history], original)
        self.assertTrue(view["summary_until_message_id"])
        self.assertTrue(summarizer.calls)
        count = len(summarizer.calls)
        again = manager.prepare(history, system_prompt="system",
                                summary=view["context_summary"],
                                cursor=view["summary_until_message_id"])
        self.assertEqual(len(summarizer.calls), count)
        self.assertEqual(again["context_summary"], view["context_summary"])

    def test_summary_failure_still_bounds_view_and_preserves_file_references(self):
        manager = ContextManager(summarizer=Summarizer(fail=True), policy=self.policy())
        history = self.history()
        history[2:2] = [
            AIMessage(content="", id="call", tool_calls=[{
                "name": "filesystem_read_file", "args": {"path": "D:/docs/original.md"},
                "id": "read", "type": "tool_call"}]),
            ToolMessage(id="result", name="filesystem_read_file", tool_call_id="read",
                        content=json.dumps({"path": "D:/docs/original.md",
                                            "line_count": 100, "next_start_line": None,
                                            "lines": [{"line": 1, "content": "正文" * 5000}]})),
        ]
        view = manager.prepare(history, summary="已有摘要")
        self.assertLessEqual(view["context_stats"]["input_tokens"], 2400)
        self.assertTrue(view["context_stats"]["degraded"])
        self.assertIn("已有摘要", view["context_summary"])
        self.assertTrue(any(ref["path"] == "D:/docs/original.md"
                            for ref in view["context_refs"]))
        self.assertIn("发布这三个文档", [m.content for m in view["llm_messages"]])

    def test_parallel_tool_results_are_kept_as_a_complete_group(self):
        manager = ContextManager(policy=self.policy())
        history = self.history()
        history.extend([AIMessage(content="", id="parallel", tool_calls=[
            {"name": "read", "args": {}, "id": key, "type": "tool_call"}
            for key in ("x", "y")]),
            ToolMessage(content="one", tool_call_id="x", id="rx"),
            ToolMessage(content="two", tool_call_id="y", id="ry")])
        view = manager.prepare(history)
        messages = view["llm_messages"]
        index = next(i for i, m in enumerate(messages) if m.id == "parallel")
        self.assertEqual({m.tool_call_id for m in messages[index + 1:index + 3]}, {"x", "y"})

    def test_interrupted_old_tool_call_is_not_sent_as_an_orphan(self):
        manager = ContextManager(policy=self.policy())
        messages = [HumanMessage(content="old", id="old"),
                    AIMessage(content="", id="orphan", tool_calls=[{
                        "name": "read", "args": {}, "id": "missing", "type": "tool_call"}]),
                    HumanMessage(content="new", id="new")]
        view = manager.prepare(messages)
        self.assertFalse(any(getattr(m, "tool_calls", None) for m in view["llm_messages"]))

    def test_chinese_estimation_and_usage_calibration(self):
        manager = ContextManager(policy=self.policy())
        self.assertGreater(manager.count([HumanMessage(content="中文" * 100)]), 200)
        self.assertGreater(manager.calibrate(1.0, 100, 300), 1.0)

    def test_error_receipt_retains_source_path_and_failure_status(self):
        manager = ContextManager(policy=self.policy())
        messages = [HumanMessage(content="publish", id="goal"),
                    AIMessage(content="", id="call", tool_calls=[{
                        "name": "publish", "args": {"source_path": "D:/docs/original.md"},
                        "id": "publish", "type": "tool_call"}]),
                    ToolMessage(content="permission denied", status="error",
                                tool_call_id="publish", id="result")]
        view = manager.prepare(messages)
        self.assertEqual(view["context_refs"][0]["path"], "D:/docs/original.md")
        self.assertEqual(view["context_refs"][0]["status"], "error")

    def test_fixed_tool_schema_budget_is_included_and_oversize_goal_is_rejected(self):
        manager = ContextManager(policy=self.policy(), tool_tokens=300)
        messages = [HumanMessage(content="goal", id="goal")]
        view = manager.prepare(messages, system_prompt="system")
        self.assertEqual(view["context_stats"]["input_tokens"],
                         manager.count(view["llm_messages"]) + 300)
        with self.assertRaisesRegex(ValueError, "超过上下文预算"):
            manager.prepare([HumanMessage(content="巨大请求" * 1000, id="large")])

    def test_summary_timeout_reports_fallback_and_completion(self):
        events = []
        manager = ContextManager(summarizer=Summarizer(fail=True), policy=self.policy())
        view = manager.prepare(self.history(), on_progress=events.append)
        self.assertTrue(view["context_stats"]["degraded"])
        self.assertIn("压缩历史上下文", events[0])
        self.assertTrue(any("摘要生成失败" in event for event in events))
        self.assertIn("历史上下文整理完成（使用精简历史）", events[-1])

    def test_progress_callback_failure_does_not_break_summary(self):
        def broken_callback(text):
            raise RuntimeError("message edit failed")

        manager = ContextManager(summarizer=Summarizer(), policy=self.policy())
        view = manager.prepare(self.history(), on_progress=broken_callback)
        self.assertFalse(view["context_stats"]["degraded"])
        self.assertLessEqual(view["context_stats"]["input_tokens"], 2400)

    def test_short_context_does_not_report_unnecessary_compression(self):
        events = []
        manager = ContextManager(summarizer=Summarizer(), policy=self.policy())
        manager.prepare([HumanMessage(content="hi", id="hi")], on_progress=events.append)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
