import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.knowledge.ingestion import DocumentParser
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.llm.embeddings import EmbeddingBatchResult
from tele_bot.models import CardAction, IncomingMessage, MessageType
from tele_bot.persistence.kb_store import KnowledgeStore
from tele_bot.router.knowledge_service import KnowledgeCommandService
from tele_bot.tools.workspace_policy import WorkspacePolicy


class FakeEmbedding:
    def embed(self, texts):
        return [EmbeddingBatchResult(tuple((1.0, 0.0) for _ in texts), len(texts), "req", "embed")]


class ProjectSpacePermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        self.docs = workspace / "docs"
        self.docs.mkdir()
        self.store = KnowledgeStore(":memory:")
        settings = AliBailianSettings(
            api_key="chat", base_url="https://chat/v1", model="qwen", reasoning_model="qwen",
            timeout_seconds=10, embedding_api_key="embed", embedding_base_url="https://embed/v1",
            embedding_dimensions=2,
        )
        client = FakeEmbedding()
        self.service = KnowledgeCommandService(
            store=self.store, parser=DocumentParser(),
            indexer=KnowledgeIndexer(self.store, client, settings),
            search=KnowledgeSearch(self.store, client, settings),
            policy=WorkspacePolicy(
                allowed_roots=(root,), workspace_root=workspace, quarantine_root=root / "quarantine"
            ),
            creator_user_ids=("admin",),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, user, text, chat_id="chat", **kwargs):
        return IncomingMessage("feishu", user, chat_id, text, **kwargs)

    def test_confirmation_token_is_bound_to_user_and_chat(self):
        self.service.handle(self.message("admin", "/kb create Team"))
        preview = self.service.handle(self.message("admin", f"/kb bind {self.docs}"))
        token = preview.card.actions[0].value
        denied = self.service.handle(
            self.message(
                "attacker", "kb_confirm", message_type=MessageType.INTERACTIVE,
                card_action=CardAction("kb_confirm", "确认", token),
            )
        )
        self.assertIn("不属于", denied.text)
        confirmed = self.service.handle(
            self.message(
                "admin", "kb_confirm", message_type=MessageType.INTERACTIVE,
                card_action=CardAction("kb_confirm", "确认", token),
            )
        )
        self.assertIn("绑定完成", confirmed.text)

    def test_member_cannot_prepare_admin_import(self):
        self.service.handle(self.message("admin", "/kb create Team"))
        self.store.add_member("chat", "admin", "member")
        response = self.service.handle(self.message("member", f"/kb bind {self.docs}"))
        self.assertIn("管理员", response.text)

    def test_fuzzy_directory_import_previews_then_indexes_all_documents(self):
        (self.docs / "one.md").write_text("alpha", encoding="utf-8")
        (self.docs / "two.txt").write_text("beta", encoding="utf-8")
        (self.docs / "skip.png").write_bytes(b"png")
        self.service.handle(self.message("admin", "/kb create Team"))
        bind = self.service.handle(self.message("admin", f"/kb bind {self.docs}"))
        self.service.handle(
            self.message(
                "admin", "kb_confirm", message_type=MessageType.INTERACTIVE,
                card_action=bind.card.actions[0],
            )
        )

        preview = self.service.handle(
            self.message("admin", "/kb import 将docs目录里的文档全部导入")
        )
        self.assertIn("批量导入 2 个文档", preview.text)
        completed = self.service.handle(
            self.message(
                "admin", "kb_confirm", message_type=MessageType.INTERACTIVE,
                card_action=preview.card.actions[0],
            )
        )

        self.assertIn("已索引 2", completed.text)
        self.assertEqual(len(self.store.list_documents("chat", "admin")), 2)

    def test_space_member_can_bind_same_space_in_another_chat(self):
        created = self.service.handle(self.message("admin", "/kb create Shared"))
        self.assertIn("已创建", created.text)
        self.store.add_member("chat", "admin", "member")
        space = self.store.require_member("chat", "admin")

        preview = self.service.handle(
            self.message("member", f"/kb use {space.id}", chat_id="chat-two")
        )
        confirmed = self.service.handle(
            self.message(
                "member", "kb_confirm", chat_id="chat-two",
                message_type=MessageType.INTERACTIVE,
                card_action=preview.card.actions[0],
            )
        )

        self.assertIn("已绑定项目空间", confirmed.text)
        self.assertEqual(self.store.require_member("chat-two", "member").id, space.id)
        with self.assertRaises(PermissionError):
            self.store.require_member("chat-two", "stranger")

    def test_unlisted_user_cannot_create_space(self):
        response = self.service.handle(self.message("member", "/kb create Forbidden"))
        self.assertIn("没有创建项目空间的权限", response.text)

    def test_whoami_is_available_before_space_binding(self):
        response = self.service.handle(
            self.message("future-creator", "/kb whoami", chat_id="unbound-chat")
        )
        self.assertIn("future-creator", response.text)


if __name__ == "__main__":
    unittest.main()
