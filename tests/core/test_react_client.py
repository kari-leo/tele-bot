"""Exercise reasoning starvation through the actual HTTP adapter and ReAct graph."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from tele_bot.llm.react_client import build_chat_openai, build_context_manager
from tele_bot.workflows.react_graph import build_react_graph


def test_reasoning_only_length_response_does_not_starve_tool_result_reply():
    requests = []
    reads = []

    @tool
    def bitable_query() -> str:
        """Read today's todo records."""
        reads.append(True)
        return '{"status":"succeeded","records":[{"task":"面试"}]}'

    def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "query", "type": "function",
                "function": {"name": "bitable_query", "arguments": "{}"}}]}
            finish, output, reasoning = "tool_calls", 100, 50
        elif body.get("thinking_budget", 131072) >= body.get("max_completion_tokens", 4096):
            # Captured failure: every completion token was reasoning, content empty.
            message = {"role": "assistant", "content": ""}
            finish, output, reasoning = "length", 4096, 4096
        else:
            message = {"role": "assistant", "content": "今天有面试。"}
            finish, output, reasoning = "stop", 1100, 1024
        return httpx.Response(200, json={
            "id": "recorded-response", "object": "chat.completion", "created": 1,
            "model": "qwen3.8-max", "choices": [{"index": 0, "message": message,
                "finish_reason": finish}], "usage": {"prompt_tokens": 100,
                "completion_tokens": output, "total_tokens": 100 + output,
                "completion_tokens_details": {"reasoning_tokens": reasoning}}})

    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client, patch(
        "tele_bot.llm.react_client.ChatOpenAI",
        side_effect=lambda **kwargs: ChatOpenAI(http_client=client, **kwargs),
    ):
        llm = build_chat_openai(api_key="fixture", model="qwen3.8-max")
        graph = build_react_graph(llm.bind_tools([bitable_query], max_tokens=4096), [bitable_query])
        result = graph.invoke({"messages": [HumanMessage(content="todo里今天有哪些事要做")],
                               "iterations": 0}, {"configurable": {"thread_id": "reasoning"}})
    assert result["messages"][-1].content == "今天有面试。"
    assert len(requests) == 2
    assert len(reads) == 1


def test_summary_reasoning_budget_leaves_room_for_summary_text():
    settings = SimpleNamespace(api_key="fixture", base_url="https://example.invalid/v1",
                               model="qwen3.8-max", timeout_seconds=60)
    manager = build_context_manager(settings, [])
    payload = manager.summarizer.bound._get_request_payload(
        [HumanMessage(content="汇总记录")], **manager.summarizer.kwargs)
    assert 0 < payload["extra_body"]["thinking_budget"] < payload["max_completion_tokens"]
    assert payload["max_completion_tokens"] >= (
        manager.policy.summary_tokens + payload["extra_body"]["thinking_budget"])


def test_legacy_model_does_not_receive_unsupported_thinking_parameter():
    llm = build_chat_openai(api_key="fixture", model="qwen-plus")
    payload = llm._get_request_payload([HumanMessage(content="你好")])
    assert "thinking_budget" not in payload.get("extra_body", {})
