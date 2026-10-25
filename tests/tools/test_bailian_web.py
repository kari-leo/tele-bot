"""Provider payloads, real tool registration, and the URL-to-summary loop."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tele_bot.tools.bailian_web import BailianWebSearchTool, build_web_tools, normalize_web_tool_names
from tele_bot.tools.lc_adapters import build_core_tools
from tele_bot.workflows.react_graph import build_react_graph


def response(kind="web_search_call", content="检索到的内容", status="completed"):
    return SimpleNamespace(
        status="completed", output_text=content,
        output=[{"type": kind, "status": status,
                 "urls": ["https://baike.baidu.com/item/1/31661"],
                 "output": "1 是自然数。",
                 "action": {"sources": [{"url": "https://example.com", "title": "来源"}]}}],
        usage={"x_tools": {kind.removesuffix("_call"): {"count": 1}}},
    )


class BailianWebTests(unittest.TestCase):
    def setUp(self):
        self.web = BailianWebSearchTool(api_key="test", base_url="https://example.com/v1",
                                        model="qwen3.7-plus", timeout_seconds=90)

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_search_uses_provider_builtin_and_preserves_sources(self, factory):
        factory.return_value.__enter__.return_value.responses.create.return_value = response()
        result = self.web.execute("最新消息")
        call = factory.return_value.__enter__.return_value.responses.create.call_args.kwargs
        self.assertEqual(call["tools"], [{"type": "web_search"}])
        self.assertEqual(call["extra_body"], {"enable_thinking": True})
        self.assertIn("最新消息", call["input"])
        self.assertEqual(result["content"], "检索到的内容")
        self.assertEqual(result["results"][0]["url"], "https://example.com")
        self.assertEqual(result["tool_calls"]["web_search"], 1)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_url_read_uses_both_builtins(self, factory):
        factory.return_value.__enter__.return_value.responses.create.return_value = response("web_extractor_call")
        result = self.web.read_page("https://baike.baidu.com/item/1/31661")
        call = factory.return_value.__enter__.return_value.responses.create.call_args.kwargs
        self.assertEqual(call["tools"], [{"type": "web_search"}, {"type": "web_extractor"}])
        self.assertIn(result["url"], call["input"])
        self.assertEqual(result["tool_calls"]["web_extractor"], 1)

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_refusal_or_search_only_is_not_a_successful_page_read(self, factory):
        for value in (response(), response("web_extractor_call", status="failed"),
                      SimpleNamespace(status="completed", output=[], usage={}, output_text="无法访问网页")):
            factory.return_value.__enter__.return_value.responses.create.return_value = value
            with self.assertRaises(RuntimeError):
                self.web.read_page("https://example.com")

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_blank_query_and_invalid_urls_do_not_call_api(self, factory):
        with self.assertRaises(ValueError):
            self.web.execute(" ")
        for url in ("file:///secret", "ftp://example.com", "https://", "https://user:pass@example.com"):
            with self.assertRaises(ValueError):
                self.web.read_page(url)
        factory.assert_not_called()

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_long_result_stays_valid_json_with_sources_and_truncation(self, factory):
        factory.return_value.__enter__.return_value.responses.create.return_value = response(content="内容" * 20000)
        result = json.loads(build_web_tools(self.web)[0].invoke({"query": "查询"}))
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"]), 12000)
        self.assertEqual(result["results"][0]["url"], "https://example.com")

    def test_production_registers_both_tools_and_removes_old_search(self):
        names = {tool.name for tool in build_core_tools(search_tool=self.web)}
        self.assertTrue({"web_search", "web_extractor"} <= names)
        self.assertNotIn("opencli_search", names)

    def test_old_search_authorization_does_not_grant_page_reading(self):
        self.assertEqual(normalize_web_tool_names(["opencli_search", "web_search", "write_report"]),
                         ["web_search", "write_report"])

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_timeout_and_incomplete_response_are_visible_errors(self, factory):
        create = factory.return_value.__enter__.return_value.responses.create
        create.side_effect = TimeoutError("request timed out")
        with self.assertRaisesRegex(RuntimeError, "request timed out"):
            self.web.execute("query")
        create.side_effect = None
        value = response()
        value.status = "incomplete"
        create.return_value = value
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.web.execute("query")

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_missing_credentials_do_not_make_request(self, factory):
        web = BailianWebSearchTool(api_key=None, base_url=self.web.base_url, model=self.web.model)
        with self.assertRaisesRegex(RuntimeError, "ALIBAILIAN_API_KEY"):
            web.execute("query")
        factory.assert_not_called()

    @patch("tele_bot.tools.bailian_web.OpenAI")
    def test_url_request_reaches_provider_and_returns_result_to_agent(self, factory):
        factory.return_value.__enter__.return_value.responses.create.return_value = response("web_extractor_call")

        class Model:
            def invoke(self, messages, config=None):
                if isinstance(messages[-1], ToolMessage):
                    receipt = json.loads(messages[-1].content)
                    return AIMessage(content="摘要：" + receipt["content"])
                return AIMessage(content="", tool_calls=[{
                    "name": "web_extractor", "args": {"url": "https://baike.baidu.com/item/1/31661"},
                    "id": "read-1", "type": "tool_call"}])

        graph = build_react_graph(Model(), build_web_tools(self.web))
        result = graph.invoke({"messages": [HumanMessage(content="阅读这个网页并摘要")], "iterations": 0},
                              config={"configurable": {"thread_id": "web-test"}})
        self.assertEqual(result["messages"][-1].content, "摘要：检索到的内容")
