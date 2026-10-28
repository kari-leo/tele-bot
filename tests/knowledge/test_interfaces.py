"""Authenticated Agent/tool and read-only MCP contracts, citations and coverage metadata."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from mcp import Client

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.config.mcp import KnowledgeMCPSettings
from tele_bot.knowledge.config import RetrievalSettings
from tele_bot.knowledge.ingestion import DocumentParser
from tele_bot.knowledge.models import DocumentSource, SearchHit
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.mcp.knowledge import KnowledgeMCPService
from tele_bot.mcp_server import create_server
from tele_bot.models import IncomingMessage
from tele_bot.persistence.kb_store import KnowledgeStore
from tele_bot.service import MessageService
from tele_bot.tools.knowledge_search import (
    CURRENT_KNOWLEDGE_MESSAGE,
    build_knowledge_search_tool,
)
from tele_bot.workflows.react_graph import build_react_graph
from tests.knowledge.support import (
    FakeEmbeddingClient,
    KnowledgeServiceCase,
    embedding_settings,
)


class FakeSearch:
    def __init__(self) -> None:
        self.calls = []

    def search_space(self, space_id, user_id, query, *, limit=5):
        self.calls.append((space_id, user_id, query, limit))
        return [
            SearchHit(
                "chunk",
                "doc",
                "Guide",
                DocumentSource.REPORT,
                "reports/guide.md",
                0,
                "matched text",
                0.9,
            )
        ]


class KnowledgeMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = KnowledgeStore(":memory:")
        self.space = self.store.create_space("chat", "Team", "admin")
        self.search = FakeSearch()
        self.service = KnowledgeMCPService(self.store, self.search, "admin")

    def tearDown(self) -> None:
        self.store.close()

    def test_lists_only_spaces_for_fixed_identity(self) -> None:
        self.store.create_space("other-chat", "Other", "other-user")
        result = self.service.list_spaces()
        self.assertEqual([item["name"] for item in result["spaces"]], ["Team"])

    def test_search_uses_fixed_identity_and_clamps_limit(self) -> None:
        result = self.service.search(self.space.id, "question", 99)
        self.assertEqual(self.search.calls, [(self.space.id, "admin", "question", 20)])
        self.assertEqual(result["hits"][0]["title"], "Guide")

    def test_document_and_budget_access_reject_non_member(self) -> None:
        denied = KnowledgeMCPService(self.store, self.search, "stranger")
        with self.assertRaises(PermissionError):
            denied.list_documents(self.space.id)
        with self.assertRaises(PermissionError):
            denied.budget(self.space.id)

    def test_mcp_identity_loads_from_feishu_config(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "tele_bot.config.mcp._load_feishu_local_env",
                return_value={"KB_MCP_USER_ID": "ou_file", "FEISHU_ADMIN_USER_IDS": "ou_file"},
            ),
        ):
            self.assertEqual(KnowledgeMCPSettings.from_env().user_id, "ou_file")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("tele_bot.config.mcp._load_feishu_local_env", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "FEISHU_ADMIN_USER_IDS"):
                KnowledgeMCPSettings.from_env()

    def test_official_mcp_client_lists_and_calls_tools(self) -> None:
        async def verify() -> None:
            async with Client(create_server(self.service)) as client:
                tools = await client.list_tools()
                self.assertEqual(
                    {tool.name for tool in tools.tools},
                    {"kb_list_spaces", "kb_search", "kb_list_documents", "kb_budget"},
                )
                self.assertTrue(
                    all(tool.annotations.read_only_hint for tool in tools.tools)
                )
                result = await client.call_tool("kb_list_spaces", {})
                payload = json.loads(result.content[0].text)
                self.assertEqual(
                    payload["spaces"][0]["name"],
                    "Team",
                )

        asyncio.run(verify())


class KnowledgeAgentTests(unittest.TestCase):
    def setUp(self):
        self.store = KnowledgeStore(":memory:")
        self.addCleanup(self.store.close)
        self.space = self.store.create_space("chat", "Team", "admin")
        self.store.add_member("chat", "admin", "reader")
        client = FakeEmbeddingClient()
        indexer = KnowledgeIndexer(self.store, client, embedding_settings())
        pending = indexer.enqueue(
            chat_id="chat",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="manual.md",
            parsed=DocumentParser().parse_bytes(
                "manual.md", b"# Device\n\nMX-204 alpha facts"
            ),
        )
        indexer.run("chat", "admin", pending)
        self.search = KnowledgeSearch(
            self.store,
            client,
            embedding_settings(),
            retrieval=RetrievalSettings(mode="hybrid"),
        )
        self.tool = build_knowledge_search_tool(self.search)

    def message(self, user="reader", chat="chat"):
        return IncomingMessage(
            channel="feishu",
            user_id=user,
            chat_id=chat,
            text="查一下 MX-204 的项目资料",
        )

    def invoke_as(self, message, args):
        token = CURRENT_KNOWLEDGE_MESSAGE.set(message)
        try:
            return json.loads(self.tool.invoke(args))
        finally:
            CURRENT_KNOWLEDGE_MESSAGE.reset(token)

    def test_schema_has_no_identity_and_forged_args_cannot_change_scope(self):
        self.assertEqual(set(self.tool.args), {"query", "limit"})
        other = self.store.create_space("other", "Other", "outsider")
        result = self.invoke_as(
            self.message(),
            {
                "query": "MX-204",
                "project_space_id": other.id,
                "user_id": "outsider",
                "chat_id": "other",
            },
        )
        self.assertEqual(result["hits"][0]["source_ref"], "manual.md")
        with self.assertRaises(PermissionError):
            self.tool.invoke({"query": "MX-204"})
        for message in (self.message("stranger"), self.message(chat="other")):
            with self.assertRaises(PermissionError):
                self.invoke_as(
                    message, {"query": "MX-204", "user_id": "admin", "chat_id": "chat"}
                )

    def test_natural_language_graph_uses_real_tool_with_source_excerpts(self):
        outer = self

        class Model:
            calls = 0

            def invoke(self, messages, config=None):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "knowledge_search",
                                "args": {"query": "MX-204"},
                                "id": "kb1",
                                "type": "tool_call",
                            }
                        ],
                    )
                result = next(
                    m for m in reversed(messages) if isinstance(m, ToolMessage)
                )
                hit = json.loads(result.content)["hits"][0]
                outer.assertEqual(hit["section_path"], ["Device"])
                outer.assertTrue(hit["document_version_id"])
                outer.assertIn("MX-204 alpha facts", hit["text"])
                return AIMessage(
                    content="根据 manual.md 的 Device 章节：MX-204 alpha facts。"
                )

        graph = build_react_graph(Model(), [self.tool])
        service = MessageService(
            AgentCore(
                executor=ReactAgentExecutor(
                    graph=graph, model_name="fixture", knowledge_store=self.store
                )
            ),
            feishu_adapter=SimpleNamespace(name="feishu"),
        )
        response = service.handle(self.message())
        self.assertIn("manual.md", response.text)
        self.assertIsNone(CURRENT_KNOWLEDGE_MESSAGE.get())

    def test_untrusted_api_has_no_tool_identity_and_skips_kb_commands(self):
        outer = self

        class Agent:
            def handle_message(self, message):
                outer.assertIsNone(CURRENT_KNOWLEDGE_MESSAGE.get())
                with outer.assertRaises(PermissionError):
                    outer.tool.invoke({"query": "MX-204"})
                return message

        class Command:
            def handle(self, message):
                outer.fail("untrusted payload reached the knowledge command surface")

        service = MessageService(
            Agent(),
            feishu_adapter=SimpleNamespace(name="feishu"),
            knowledge_service=Command(),
        )
        service.handle(self.message(), allow_knowledge=False, allow_schedule=False)

    def test_checkpoint_scope_changes_with_user_binding_and_authentication(self):
        class Graph:
            configs = []

            def invoke(self, state, config):
                self.configs.append(config["configurable"])
                return {"messages": state["messages"] + [AIMessage(content="done")]}

        graph = Graph()
        service = MessageService(
            AgentCore(
                executor=ReactAgentExecutor(
                    graph=graph, model_name="fixture", knowledge_store=self.store
                )
            ),
            feishu_adapter=SimpleNamespace(name="feishu"),
        )
        service.handle(self.message())
        service.handle(self.message("admin"))
        service.handle(self.message(), allow_knowledge=False)
        new_space = self.store.create_space("another", "Another", "reader")
        self.store.bind_space("chat", "reader", new_space.id)
        service.handle(self.message())
        self.assertEqual(len({c["thread_id"] for c in graph.configs}), 4)
        self.assertEqual({c["chat_id"] for c in graph.configs}, {"chat"})


class TableInterfaceTests(KnowledgeServiceCase):
    def setUp(self):
        super().setUp()
        self.service.handle(self.message("admin", "/kb create Team"))
        self.confirm(
            self.service.handle(self.message("admin", f"/kb bind {self.docs}"))
        )
        self.service.search.retrieval = RetrievalSettings(mode="keyword")

    def test_table_search_mcp_and_agent_signal_partial_coverage(self):
        import json

        from tele_bot.mcp.knowledge import KnowledgeMCPService
        from tele_bot.tools.knowledge_search import (
            CURRENT_KNOWLEDGE_MESSAGE,
            build_knowledge_search_tool,
        )

        (self.docs / "a.csv").write_text("项目,数量\nAlpha,1\nBeta,2", encoding="utf-8")
        self.confirm(
            self.service.handle(self.message("admin", f"/kb import {self.docs}"))
        )
        space = self.store.get_space("chat", "admin")
        result = KnowledgeMCPService(self.store, self.service.search, "admin").search(
            space.id, "Alpha"
        )
        self.assertEqual(result["coverage"], "retrieved_chunks_only")
        self.assertEqual(result["full_source_coverage"], "not_verified")
        self.assertTrue(result["hits"][0]["indexed_at"])
        token = CURRENT_KNOWLEDGE_MESSAGE.set(
            self.message("admin", "统计整个表数量合计")
        )
        try:
            result = json.loads(
                build_knowledge_search_tool(self.service.search).invoke(
                    {"query": "Alpha"}
                )
            )
            self.assertEqual(result["full_source_coverage"], "not_verified")
            self.assertIn("record:2", result["hits"][0]["source_locator"])
        finally:
            CURRENT_KNOWLEDGE_MESSAGE.reset(token)
