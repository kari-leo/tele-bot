import unittest

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.knowledge.ingestion import DocumentParser, TextChunk, TextChunker, sha256_text
from tele_bot.knowledge.models import DocumentSource
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.llm.embeddings import EmbeddingBatchResult
from tele_bot.persistence.kb_store import KnowledgeStore


class FakeEmbeddingClient:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        vectors = tuple((1.0, 0.0) if "alpha" in text.lower() else (0.0, 1.0) for text in texts)
        return [EmbeddingBatchResult(vectors, len(texts), f"req-{len(self.calls)}", "embed")]


class LineChunker:
    def split(self, text):
        return [
            TextChunk(index, line, sha256_text(line))
            for index, line in enumerate(text.splitlines()) if line
        ]


def settings(token_budget=1000):
    return AliBailianSettings(
        api_key="chat", base_url="https://chat/v1", model="qwen", reasoning_model="qwen",
        timeout_seconds=10, embedding_api_key="embed", embedding_base_url="https://embed/v1",
        embedding_dimensions=2, embedding_daily_token_budget=token_budget,
    )


class KnowledgeSearchTests(unittest.TestCase):
    def setUp(self):
        self.store = KnowledgeStore(":memory:")
        self.store.create_space("chat", "Space", "admin")
        self.store.add_member("chat", "admin", "member")
        self.client = FakeEmbeddingClient()

    def tearDown(self):
        self.store.close()

    def test_indexes_then_searches_only_current_authorized_space(self):
        parser = DocumentParser()
        indexer = KnowledgeIndexer(
            self.store, self.client, settings(), chunker=TextChunker(max_chars=20, overlap_chars=2)
        )
        pending = indexer.enqueue(
            chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="reports/a.md", parsed=parser.parse_bytes("a.md", b"alpha facts\n\nbeta facts")
        )
        job = indexer.run("chat", "admin", pending)
        self.assertEqual(job.status.value, "succeeded")
        hits = KnowledgeSearch(self.store, self.client, settings()).search(
            "chat", "member", "alpha question", limit=1
        )
        self.assertEqual(hits[0].title, "a.md")
        self.assertGreater(hits[0].score, 0.9)
        with self.assertRaises(PermissionError):
            KnowledgeSearch(self.store, self.client, settings()).search("chat", "stranger", "alpha")

    def test_unchanged_document_does_not_call_embedding_again(self):
        parsed = DocumentParser().parse_bytes("a.md", b"alpha")
        indexer = KnowledgeIndexer(self.store, self.client, settings())
        first = indexer.enqueue(
            chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="a", parsed=parsed
        )
        indexer.run("chat", "admin", first)
        calls = len(self.client.calls)
        second = indexer.enqueue(
            chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="a", parsed=parsed
        )
        self.assertFalse(second.changed)
        self.assertIsNone(indexer.run("chat", "admin", second))
        self.assertEqual(len(self.client.calls), calls)

    def test_changed_version_only_embeds_new_chunks(self):
        indexer = KnowledgeIndexer(self.store, self.client, settings(), chunker=LineChunker())
        first = indexer.enqueue(
            chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="incremental", parsed=DocumentParser().parse_bytes("a.md", b"alpha\nbeta")
        )
        indexer.run("chat", "admin", first)
        second = indexer.enqueue(
            chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="incremental", parsed=DocumentParser().parse_bytes("a.md", b"alpha\ngamma")
        )
        indexer.run("chat", "admin", second)
        self.assertEqual(self.client.calls, [["alpha", "beta"], ["gamma"]])

    def test_budget_exhaustion_pauses_job_before_external_call(self):
        client = FakeEmbeddingClient()
        indexer = KnowledgeIndexer(self.store, client, settings(token_budget=1))
        pending = indexer.enqueue(
            chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="budget", parsed=DocumentParser().parse_bytes("a.md", b"alpha facts")
        )
        job = indexer.run("chat", "admin", pending)
        self.assertEqual(job.status.value, "paused_budget")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
