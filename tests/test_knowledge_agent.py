import json
from types import SimpleNamespace
import unittest

from langchain_core.messages import AIMessage, ToolMessage

from tele_bot.agent import AgentCore
from tele_bot.agents.react_executor import ReactAgentExecutor
from tele_bot.knowledge.config import RetrievalSettings
from tele_bot.knowledge.ingestion import DocumentParser
from tele_bot.knowledge.models import DocumentSource
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.models import IncomingMessage
from tele_bot.persistence.kb_store import KnowledgeStore
from tele_bot.service import MessageService
from tele_bot.tools.knowledge_search import CURRENT_KNOWLEDGE_MESSAGE, build_knowledge_search_tool
from tele_bot.workflows.react_graph import build_react_graph
from tests.test_knowledge_search import FakeEmbeddingClient, settings


class KnowledgeAgentTests(unittest.TestCase):
    def setUp(self):
        self.store = KnowledgeStore(":memory:")
        self.addCleanup(self.store.close)
        self.space = self.store.create_space("chat", "Team", "admin")
        self.store.add_member("chat", "admin", "reader")
        client = FakeEmbeddingClient()
        indexer = KnowledgeIndexer(self.store, client, settings())
        pending = indexer.enqueue(chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="manual.md", parsed=DocumentParser().parse_bytes("manual.md", b"# Device\n\nMX-204 alpha facts"))
        indexer.run("chat", "admin", pending)
        self.search = KnowledgeSearch(self.store, client, settings(), retrieval=RetrievalSettings(mode="hybrid"))
        self.tool = build_knowledge_search_tool(self.search)

    def message(self, user="reader", chat="chat"):
        return IncomingMessage(channel="feishu", user_id=user, chat_id=chat, text="查一下 MX-204 的项目资料")

    def invoke_as(self, message, args):
        token = CURRENT_KNOWLEDGE_MESSAGE.set(message)
        try:
            return json.loads(self.tool.invoke(args))
        finally:
            CURRENT_KNOWLEDGE_MESSAGE.reset(token)

    def test_schema_has_no_identity_and_forged_args_cannot_change_scope(self):
        self.assertEqual(set(self.tool.args), {"query", "limit"})
        other = self.store.create_space("other", "Other", "outsider")
        result = self.invoke_as(self.message(), {"query": "MX-204", "project_space_id": other.id,
                                                 "user_id": "outsider", "chat_id": "other"})
        self.assertEqual(result["hits"][0]["source_ref"], "manual.md")
        with self.assertRaises(PermissionError):
            self.tool.invoke({"query": "MX-204"})
        for message in (self.message("stranger"), self.message(chat="other")):
            with self.assertRaises(PermissionError):
                self.invoke_as(message, {"query": "MX-204", "user_id": "admin", "chat_id": "chat"})

    def test_natural_language_graph_uses_real_tool_with_source_excerpts(self):
        outer = self

        class Model:
            calls = 0
            def invoke(self, messages, config=None):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content="", tool_calls=[{"name": "knowledge_search", "args": {"query": "MX-204"},
                                                             "id": "kb1", "type": "tool_call"}])
                result = next(m for m in reversed(messages) if isinstance(m, ToolMessage))
                hit = json.loads(result.content)["hits"][0]
                outer.assertEqual(hit["section_path"], ["Device"])
                outer.assertTrue(hit["document_version_id"])
                outer.assertIn("MX-204 alpha facts", hit["text"])
                return AIMessage(content="根据 manual.md 的 Device 章节：MX-204 alpha facts。")

        graph = build_react_graph(Model(), [self.tool])
        service = MessageService(AgentCore(executor=ReactAgentExecutor(graph=graph, model_name="fixture", knowledge_store=self.store)),
                                 feishu_adapter=SimpleNamespace(name="feishu"))
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

        service = MessageService(Agent(), feishu_adapter=SimpleNamespace(name="feishu"), knowledge_service=Command())
        service.handle(self.message(), allow_knowledge=False, allow_schedule=False)

    def test_checkpoint_scope_changes_with_user_binding_and_authentication(self):
        class Graph:
            configs = []
            def invoke(self, state, config):
                self.configs.append(config["configurable"])
                return {"messages": state["messages"] + [AIMessage(content="done")]}

        graph = Graph()
        service = MessageService(AgentCore(executor=ReactAgentExecutor(graph=graph, model_name="fixture", knowledge_store=self.store)),
                                 feishu_adapter=SimpleNamespace(name="feishu"))
        service.handle(self.message())
        service.handle(self.message("admin"))
        service.handle(self.message(), allow_knowledge=False)
        new_space = self.store.create_space("another", "Another", "reader")
        self.store.bind_space("chat", "reader", new_space.id)
        service.handle(self.message())
        self.assertEqual(len({c["thread_id"] for c in graph.configs}), 4)
        self.assertEqual({c["chat_id"] for c in graph.configs}, {"chat"})


if __name__ == "__main__":
    unittest.main()
