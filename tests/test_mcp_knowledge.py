import asyncio
import json
import os
import unittest
from unittest.mock import patch

from mcp import Client

from tele_bot.config.mcp import KnowledgeMCPSettings
from tele_bot.knowledge.models import DocumentSource, SearchHit
from tele_bot.mcp.knowledge import KnowledgeMCPService
from tele_bot.mcp_server import create_server
from tele_bot.persistence.kb_store import KnowledgeStore


class FakeSearch:
    def __init__(self) -> None:
        self.calls = []

    def search_space(self, space_id, user_id, query, *, limit=5):
        self.calls.append((space_id, user_id, query, limit))
        return [
            SearchHit(
                "chunk", "doc", "Guide", DocumentSource.REPORT,
                "reports/guide.md", 0, "matched text", 0.9,
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
        self.assertEqual(
            self.search.calls, [(self.space.id, "admin", "question", 20)]
        )
        self.assertEqual(result["hits"][0]["title"], "Guide")

    def test_document_and_budget_access_reject_non_member(self) -> None:
        denied = KnowledgeMCPService(self.store, self.search, "stranger")
        with self.assertRaises(PermissionError):
            denied.list_documents(self.space.id)
        with self.assertRaises(PermissionError):
            denied.budget(self.space.id)

    def test_mcp_identity_loads_from_feishu_config(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "tele_bot.config.mcp._load_feishu_local_env",
            return_value={"KB_MCP_USER_ID": "ou_file"},
        ):
            self.assertEqual(KnowledgeMCPSettings.from_env().user_id, "ou_file")

        with patch.dict(os.environ, {}, clear=True), patch(
            "tele_bot.config.mcp._load_feishu_local_env", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "KB_MCP_USER_ID"):
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


if __name__ == "__main__":
    unittest.main()
