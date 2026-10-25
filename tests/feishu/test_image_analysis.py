"""Exercise Feishu image events through the real agent and serialized model request."""

import base64
import json
from dataclasses import replace
from unittest.mock import Mock, patch

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.models import AttachmentDescriptor, MessageType
from tele_bot.persistence import build_sqlite_saver
from tele_bot.service import MessageService
from tele_bot.workflows.context_manager import ContextManager
from tele_bot.workflows.react_graph import build_react_graph
from tests.knowledge.support import KnowledgeServiceCase


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
)


def event(kind="image", content=None):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "user"}},
            "message": {
                "message_id": "om_image", "chat_id": "chat", "chat_type": "p2p",
                "message_type": kind,
                "content": json.dumps(content or {"image_key": "img_fixture"}),
            },
        },
    }


@pytest.fixture
def knowledge():
    fixture = KnowledgeServiceCase()
    fixture.setUp()
    try:
        yield fixture.service
    finally:
        fixture.tearDown()


@pytest.mark.parametrize("streaming", [False, True])
def test_image_reaches_multimodal_model_and_remains_available_for_followup(knowledge, streaming):
    requests = []

    def endpoint(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat-image", "object": "chat.completion", "created": 1,
            "model": "qwen3.8-max", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "图片分析结果"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        })

    adapter = FeishuWebhookAdapter("app", "secret")
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        llm = ChatOpenAI(api_key="fixture", base_url="https://model.invalid/v1",
                         model="qwen3.8-max", http_client=client)
        executor = ReactAgentExecutor(graph=build_react_graph(llm, []), model_name="qwen3.8-max")
        service = MessageService(AgentCore(executor=executor), feishu_adapter=adapter,
                                 knowledge_service=knowledge, streaming_enabled=streaming)
        with patch.object(adapter, "download_attachment", return_value=PNG) as download, patch(
            "tele_bot.service.FeishuProgressReporter"
        ):
            waiting = service.handle(adapter.parse_incoming(event()))
            assert waiting.already_sent and waiting.text == ""
            response = service.handle(adapter.parse_incoming(event("text", {"text": "分析这张图片"})))
            assert response.text == "图片分析结果"
            assert download.call_count == 1
            followup = service.handle(adapter.parse_incoming(event("text", {"text": "分析这张图片"})))
            assert followup.text == "图片分析结果"
            assert download.call_count == 1
            other_chat = event("text", {"text": "你好"})
            other_chat["event"]["message"]["chat_id"] = "other"
            service.handle(adapter.parse_incoming(other_chat))

    for request in requests[:2]:
        images = [block for msg in request["messages"] if isinstance(msg["content"], list)
                  for block in msg["content"] if block.get("type") == "image_url"]
        assert len(images) == 1
        assert images[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert not any(isinstance(msg["content"], list) for msg in requests[2]["messages"])
    assert knowledge.indexer.client.calls == []


def test_rich_text_preserves_question_and_all_images():
    adapter = FeishuWebhookAdapter("app", "secret")
    incoming = adapter.parse_incoming(event("post", {
        "title": "对比截图", "content": [
            [{"tag": "text", "text": "这两张图有什么区别？"}],
            [{"tag": "img", "image_key": "img_1"}, {"tag": "img", "image_key": "img_2"}],
        ],
    }))
    assert incoming is not None
    assert "这两张图有什么区别" in incoming.text
    assert [attachment.key for attachment in incoming.attachments] == ["img_1", "img_2"]


def test_image_download_failure_is_reported_without_calling_model(knowledge):
    adapter = FeishuWebhookAdapter("app", "secret")
    agent = Mock()
    service = MessageService(agent, feishu_adapter=adapter, knowledge_service=knowledge)
    with patch.object(adapter, "download_attachment", side_effect=RuntimeError("permission denied")):
        response = service.handle(replace(adapter.parse_incoming(event()), text="分析这张图片"))
    assert "图片读取失败" in response.text
    agent.handle_message.assert_not_called()


def test_context_budget_and_summary_do_not_treat_base64_as_text():
    manager = ContextManager()
    image = HumanMessage(content=[{"type": "text", "text": "分析这张图片"}, {
        "type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4_000_000},
    }], id="image")
    view = manager.prepare([image])
    assert view["llm_messages"][-1].content == image.content
    assert 1_000 < view["context_stats"]["input_tokens"] < 50_000
    assert "data:image" not in manager._compact(image)


@pytest.mark.parametrize("data", [b"", b'{"code":999,"msg":"not allowed"}', b"<html>error</html>"])
def test_invalid_download_content_does_not_reach_model(knowledge, data):
    adapter = FeishuWebhookAdapter("app", "secret")
    agent = Mock()
    service = MessageService(agent, feishu_adapter=adapter, knowledge_service=knowledge)
    with patch.object(adapter, "download_attachment", return_value=data):
        response = service.handle(replace(adapter.parse_incoming(event()), text="分析这张图片"))
    assert "图片读取失败" in response.text
    agent.handle_message.assert_not_called()


def test_size_metadata_and_image_count_are_checked_before_downloading():
    from dataclasses import replace
    from tele_bot.service import MAX_IMAGE_BYTES, MAX_IMAGES

    adapter = FeishuWebhookAdapter("app", "secret")
    service = MessageService(Mock(), feishu_adapter=adapter)
    incoming = replace(adapter.parse_incoming(event()), text="分析这张图片")
    oversized = AttachmentDescriptor(MessageType.IMAGE, "img_big", size_bytes=MAX_IMAGE_BYTES + 1)
    with patch.object(adapter, "download_attachment") as download:
        response = service.handle(replace(incoming, attachments=(oversized,)))
        assert "图片读取失败" in response.text
        response = service.handle(replace(incoming, attachments=tuple(
            replace(incoming.attachments[0], key=f"img_{index}") for index in range(MAX_IMAGES + 1))))
        assert "图片读取失败" in response.text
        download.assert_not_called()


def test_download_uses_message_resource_image_endpoint():
    adapter = FeishuWebhookAdapter("app", "secret")
    adapter._access_token = "fixture-token"
    adapter._token_expires_at = 9999999999
    incoming = replace(adapter.parse_incoming(event()), text="分析这张图片")
    with patch("tele_bot.channels.feishu.httpx.get", return_value=httpx.Response(200, content=PNG)) as get:
        response = MessageService(Mock(), feishu_adapter=adapter).handle(incoming)
    assert response is not None
    assert get.call_args.args[0].endswith("/messages/om_image/resources/img_fixture")
    assert get.call_args.kwargs["params"] == {"type": "image"}


def test_image_and_followup_survive_sqlite_graph_restart(knowledge):
    captured = []

    class Model:
        def invoke(self, messages, config=None):
            captured.append(messages)
            return AIMessage(content="图片分析结果")

    adapter = FeishuWebhookAdapter("app", "secret")
    path = knowledge.policy.workspace_root / "image-history.sqlite"
    for index in range(2):
        saver = build_sqlite_saver(path)
        try:
            graph = build_react_graph(Model(), [], checkpointer=saver)
            service = MessageService(AgentCore(executor=ReactAgentExecutor(graph=graph, model_name="fixture")),
                                     feishu_adapter=adapter)
            with patch.object(adapter, "download_attachment", return_value=PNG):
                if index == 0:
                    assert service.handle(adapter.parse_incoming(event())).already_sent
                incoming = event("text", {"text": "分析这张图片"})
                assert service.handle(adapter.parse_incoming(incoming)).text == "图片分析结果"
        finally:
            saver.conn.close()
    images = [m for m in captured[-1] if isinstance(m, HumanMessage) and isinstance(m.content, list)]
    assert len(images) == 1
    assert images[0].content[1]["type"] == "image_url"

