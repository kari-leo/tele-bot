"""Shared offline clients, document builders and command-service fixture."""

from __future__ import annotations

import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.knowledge.ingestion import (
    DocumentParser,
)
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.llm.embeddings import EmbeddingBatchResult
from tele_bot.models import (
    IncomingMessage,
    MessageType,
)
from tele_bot.persistence.kb_store import KnowledgeStore
from tele_bot.router.knowledge_service import KnowledgeCommandService
from tele_bot.tools.workspace_policy import WorkspacePolicy


class FakeEmbeddingClient:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        vectors = tuple(
            (1.0, 0.0) if "alpha" in text.lower() else (0.0, 1.0) for text in texts
        )
        return [
            EmbeddingBatchResult(vectors, len(texts), f"req-{len(self.calls)}", "embed")
        ]


def embedding_settings(token_budget=1000):
    return AliBailianSettings(
        api_key="chat",
        base_url="https://chat/v1",
        model="qwen",
        reasoning_model="qwen",
        timeout_seconds=10,
        embedding_api_key="embed",
        embedding_base_url="https://embed/v1",
        embedding_dimensions=2,
        embedding_daily_token_budget=token_budget,
    )


class KnowledgeServiceCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        self.docs = workspace / "docs"
        self.docs.mkdir()
        self.store = KnowledgeStore(":memory:")
        settings = embedding_settings()
        client = FakeEmbeddingClient()
        self.service = KnowledgeCommandService(
            store=self.store,
            parser=DocumentParser(),
            indexer=KnowledgeIndexer(self.store, client, settings),
            search=KnowledgeSearch(self.store, client, settings),
            policy=WorkspacePolicy(
                allowed_roots=(root,),
                workspace_root=workspace,
                quarantine_root=root / "quarantine",
            ),
            creator_user_ids=("admin",),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, user, text, chat_id="chat", **kwargs):
        return IncomingMessage("feishu", user, chat_id, text, **kwargs)

    def confirm(self, preview):
        self.assertIsNotNone(preview.card, preview.text)
        return self.service.handle(
            self.message(
                "admin",
                "confirm",
                message_type=MessageType.INTERACTIVE,
                card_action=preview.card.actions[0],
            )
        )


def workbook_bytes(fill):
    workbook = Workbook()
    fill(workbook)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def docx_bytes(body):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + body
            + "</w:body></w:document>",
        )
    return output.getvalue()


def tc(value, properties=""):
    return f"<w:tc><w:tcPr>{properties}</w:tcPr><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>"


def assert_chunk_locations(case, parsed, chunks, max_chars=None):
    for chunk in chunks:
        case.assertEqual(parsed.text[chunk.start_char : chunk.end_char], chunk.text)
        case.assertTrue(chunk.source_locator)
        if max_chars is not None:
            case.assertLessEqual(len(chunk.text), max_chars)
    return chunks
