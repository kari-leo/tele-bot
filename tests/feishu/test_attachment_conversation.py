"""Separate Feishu uploads and instructions must reach a single agent turn."""

from dataclasses import replace
from unittest.mock import Mock, patch
from threading import Event
import time

import httpx
import pytest

from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.models import AttachmentDescriptor, IncomingMessage, MessageType, OutgoingMessage
from tele_bot.service import MessageService
from tele_bot.persistence.attachment_store import AttachmentStore
from tele_bot.channels.feishu_batching import FeishuMessageBatcher, merge_messages


def message(text="", **kwargs):
    return IncomingMessage("feishu", "user", "chat", text, **kwargs)


def upload(kind=MessageType.FILE):
    return message("[文件] jobs.csv" if kind is MessageType.FILE else "[图片]",
                   message_id="om_upload", message_type=kind,
                   attachments=(AttachmentDescriptor(kind, "resource", "jobs.csv"),))


def service():
    adapter = FeishuWebhookAdapter("app", "secret")
    agent = Mock()
    agent.handle_message.return_value = OutgoingMessage("feishu", "chat", "done")
    return MessageService(agent, feishu_adapter=adapter), adapter, agent


@pytest.mark.parametrize("kind", [MessageType.FILE, MessageType.IMAGE])
def test_bare_file_is_silent_and_does_not_start_knowledge_import(kind):
    svc, adapter, agent = service()
    svc.knowledge_service = Mock()
    with patch.object(adapter, "download_attachment") as download:
        result = svc.handle(upload(kind))
    assert result.already_sent and result.text == ""
    svc.knowledge_service.handle.assert_not_called()
    agent.handle_message.assert_not_called()
    download.assert_not_called()


def test_file_then_text_is_parsed_for_model_without_knowledge_base():
    svc, adapter, agent = service()
    svc.handle(upload())
    with patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer"):
        svc.handle(message("解析这个表格并加入校招岗位多维表", message_id="om_request"))
    received = agent.handle_message.call_args.args[0]
    assert received.text == "解析这个表格并加入校招岗位多维表"
    assert "Alpha" in received.attachment_context
    assert "Engineer" in received.attachment_context


def test_quote_resolves_previous_upload_after_pending_has_been_consumed():
    svc, adapter, agent = service()
    svc.handle(upload())
    with patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer"):
        svc.handle(message("读一下"))
        svc.handle(message("重新读一下这个文件", quote_target_message_id="om_upload"))
    assert "Alpha" in agent.handle_message.call_args.args[0].attachment_context


def test_knowledge_service_does_not_interpret_upload_as_import():
    from tests.knowledge.support import KnowledgeServiceCase
    fixture = KnowledgeServiceCase()
    fixture.setUp()
    try:
        assert fixture.service.handle(replace(upload(), user_id="admin")) is None
    finally:
        fixture.tearDown()


def test_pending_upload_survives_service_restart(tmp_path):
    path = tmp_path / "attachments.sqlite"
    store = AttachmentStore(path)
    svc, _, _ = service()
    svc.attachment_store = store
    svc.handle(upload())
    store.close()
    restored = AttachmentStore(path)
    try:
        svc, adapter, agent = service()
        svc.attachment_store = restored
        with patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer") as download:
            svc.handle(message("解析附件", message_id="om_request"))
        assert "Alpha" in agent.handle_message.call_args.args[0].attachment_context
        assert download.call_args.args[0] == "om_upload"
    finally:
        restored.close()


@pytest.mark.parametrize("scope", [{"user_id": "other"}, {"chat_id": "other"}, {"channel": "other"}])
def test_pending_upload_is_private_to_sender_and_chat(scope):
    svc, adapter, agent = service()
    svc.handle(upload())
    with patch.object(adapter, "download_attachment") as download:
        svc.handle(replace(message("你好"), **scope))
    download.assert_not_called()
    assert not agent.handle_message.call_args.args[0].attachments


def test_failed_download_retains_attachment_for_retry():
    svc, adapter, agent = service()
    svc.handle(upload())
    with patch.object(adapter, "download_attachment", side_effect=RuntimeError("download failed")):
        assert "读取失败" in svc.handle(message("解析文件")).text
    agent.handle_message.assert_not_called()
    with patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer"):
        assert svc.handle(message("再试一次")).text == "done"
    assert "Alpha" in agent.handle_message.call_args.args[0].attachment_context


def test_quote_fetches_pre_fix_file_and_uses_original_resource_id():
    svc, adapter, agent = service()
    original = upload()
    with (patch.object(adapter, "get_quoted_message", return_value=original) as lookup,
          patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer") as download):
        svc.handle(message("解析引用文件", message_id="om_new", quote_target_message_id="om_upload"))
    lookup.assert_called_once()
    assert download.call_args.args[0] == "om_upload"
    assert "Alpha" in agent.handle_message.call_args.args[0].attachment_context


@pytest.mark.parametrize("overrides", [{"chat_id": "different"}, {"deleted": True}, {"message_id": "wrong"}])
def test_remote_quote_rejects_invalid_or_cross_chat_response(overrides):
    svc, adapter, agent = service()
    adapter._access_token, adapter._token_expires_at = "fixture", 9999999999
    item = {"message_id": "om_old", "chat_id": "chat", "msg_type": "file",
            "body": {"content": '{"file_key":"resource","file_name":"jobs.csv"}'}, **overrides}
    with patch("tele_bot.channels.feishu.httpx.get", return_value=httpx.Response(
            200, json={"code": 0, "data": {"items": [item]}})):
        result = svc.handle(message("读取引用", quote_target_message_id="om_old"))
    assert "附件读取失败" in result.text
    agent.handle_message.assert_not_called()


def test_remote_quote_downloads_file_through_real_adapter_contract():
    import json
    svc, adapter, agent = service()
    adapter._access_token, adapter._token_expires_at = "fixture", 9999999999
    item = {"message_id": "om_old", "chat_id": "chat", "msg_type": "file",
            "body": {"content": json.dumps({"file_key": "file_1", "file_name": "jobs.csv"})}}
    with patch("tele_bot.channels.feishu.httpx.get", side_effect=[
        httpx.Response(200, json={"code": 0, "data": {"items": [item]}}),
        httpx.Response(200, content=b"company,job\nAlpha,Engineer"),
    ]) as get:
        assert svc.handle(message("解析文件", message_id="om_new", quote_target_message_id="om_old")).text == "done"
    assert get.call_args_list[0].args[0].endswith("/messages/om_old")
    assert get.call_args_list[1].args[0].endswith("/messages/om_old/resources/file_1")
    assert "Alpha" in agent.handle_message.call_args.args[0].attachment_context


@pytest.mark.parametrize("reverse", [False, True])
def test_nearby_upload_and_instruction_execute_once_in_either_order(reverse):
    svc, adapter, agent = service()
    completed = Event()

    def handle(batch):
        try:
            svc.handle(batch)
        finally:
            completed.set()

    batcher = FeishuMessageBatcher(handle, wait_seconds=0.04)
    messages = [upload(), message("把表格岗位加入多维表", message_id="om_request")]
    with patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer") as download:
        for item in reversed(messages) if reverse else messages:
            batcher.submit(item)
        assert completed.wait(3)
    agent.handle_message.assert_called_once()
    assert agent.handle_message.call_args.args[0].text == "把表格岗位加入多维表"
    assert download.call_args.args[0] == "om_upload"


def test_multi_file_batch_keeps_each_resource_source():
    svc, adapter, agent = service()
    second = replace(upload(), message_id="om_second")
    incoming = merge_messages([upload(), second, message("对比这两个表", message_id="om_request")])
    with patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer") as download:
        svc.handle(incoming)
    assert [call.args[0] for call in download.call_args_list] == ["om_upload", "om_second"]
    assert agent.handle_message.call_count == 1


def test_image_sent_as_file_and_document_can_be_used_in_one_turn():
    from tests.feishu.test_image_analysis import PNG
    svc, adapter, agent = service()
    image = replace(upload(), message_id="om_picture", attachments=(
        AttachmentDescriptor(MessageType.FILE, "picture", "photo.png"),))
    incoming = merge_messages([upload(), image, message("对比文档和截图", message_id="om_request")])
    with patch.object(adapter, "download_attachment", side_effect=[b"company,job\nAlpha,Engineer", PNG]) as download:
        assert svc.handle(incoming).text == "done"
    received = agent.handle_message.call_args.args[0]
    assert "Alpha" in received.attachment_context
    assert received.image_data_urls[0].startswith("data:image/png;base64,")
    assert download.call_args.args[1].kind is MessageType.FILE


def test_commands_do_not_consume_unrelated_pending_uploads():
    svc, adapter, agent = service()
    svc.handle(upload())
    with patch.object(adapter, "download_attachment") as download:
        svc.handle(message("/help"))
    download.assert_not_called()
    assert svc.attachment_store.find(message())


def test_file_text_reaches_real_model_request_without_changing_user_write_intent():
    from langchain_core.messages import AIMessage, HumanMessage
    from tele_bot.agent import AgentCore
    from tele_bot.agents.react_executor import ReactAgentExecutor
    from tele_bot.workflows.react_graph import build_react_graph
    from tests.knowledge.support import workbook_bytes

    captured = []

    class Model:
        def invoke(self, messages, config=None):
            captured.extend(messages)
            return AIMessage(content="已解析")

    svc, adapter, _ = service()
    svc.agent_core = AgentCore(executor=ReactAgentExecutor(graph=build_react_graph(Model(), []), model_name="fixture"))
    file = replace(upload(), attachments=(AttachmentDescriptor(MessageType.FILE, "file", "jobs.xlsx"),))
    svc.handle(file)
    data = workbook_bytes(lambda workbook: (workbook.active.append(["公司", "岗位"]),
                                           workbook.active.append(["Alpha", "Engineer"])))
    with patch.object(adapter, "download_attachment", return_value=data):
        assert svc.handle(message("把表格中的岗位加入多维表", message_id="om_request")).text == "已解析"
    human = next(item for item in captured if isinstance(item, HumanMessage))
    assert "Alpha" in human.content and "Engineer" in human.content
    assert "把表格中的岗位加入多维表" in human.content


def test_large_parse_is_explicit_about_incomplete_coverage():
    svc, adapter, agent = service()
    svc.handle(upload())
    with (patch("tele_bot.service.MAX_ATTACHMENT_CONTEXT_CHARS", 15),
          patch.object(adapter, "download_attachment", return_value=b"company,job\nAlpha,Engineer")):
        svc.handle(message("解析全部岗位"))
    assert "内容已截断" in agent.handle_message.call_args.args[0].attachment_context


def test_messages_queued_behind_slow_model_keep_separate_turns_when_sent_apart():
    started, release = Event(), Event()
    received = []

    def callback(batch):
        received.append(batch.text)
        if len(received) == 1:
            started.set()
            assert release.wait(3)

    batcher = FeishuMessageBatcher(callback, wait_seconds=0.01)
    batcher.submit(message("first"))
    assert started.wait(3)
    batcher.submit(message("second"))
    time.sleep(0.04)
    batcher.submit(message("third"))
    release.set()
    assert batcher.wait_idle(3)
    assert received == ["first", "second", "third"]


def test_explicit_knowledge_import_has_short_preview_and_requires_confirmation():
    from tests.knowledge.support import KnowledgeServiceCase, workbook_bytes
    fixture = KnowledgeServiceCase()
    fixture.setUp()
    try:
        fixture.service.handle(replace(message("/kb create Team"), user_id="admin"))
        svc, adapter, agent = service()
        svc.knowledge_service = fixture.service
        fixture.service.feishu_adapter = adapter
        file = replace(upload(), user_id="admin", attachments=(
            AttachmentDescriptor(MessageType.FILE, "resource", "wide.xlsx"),))
        svc.handle(file)
        def fill(workbook):
            workbook.active.append(["字段" + str(index) for index in range(100)])
            workbook.active.append([str(index) for index in range(100)])
        with patch.object(adapter, "download_attachment", return_value=workbook_bytes(fill)):
            result = svc.handle(replace(message("把这个文件加入知识库", message_id="om_request"), user_id="admin"))
        assert result.card is not None
        assert len(result.card.body) < 1000
        assert "字段99" not in result.card.body
        assert "100 列" in result.card.body
        assert "空间成员可读取" in result.card.body
        assert fixture.store.list_documents("chat", "admin") == []
        assert fixture.service.indexer.client.calls == []
        agent.handle_message.assert_not_called()
        cancelled = svc.handle(replace(message("取消"), user_id="admin", card_action=result.card.actions[1]))
        assert cancelled.text == "已取消知识库操作。"
    finally:
        fixture.tearDown()


@pytest.mark.parametrize("text", ["不需要加入知识库，只分析", "知识库没有这个文件，把岗位加入多维表", "不要导入知识库"])
def test_knowledge_words_and_negated_imports_do_not_grant_intent(text):
    from tests.knowledge.support import KnowledgeServiceCase
    fixture = KnowledgeServiceCase()
    fixture.setUp()
    try:
        assert fixture.service.handle(replace(upload(), user_id="admin", text=text)) is None
    finally:
        fixture.tearDown()
