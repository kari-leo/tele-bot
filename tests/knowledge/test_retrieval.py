"""Embedding transport, incremental indexing and keyword/vector/hybrid retrieval."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import httpx

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.knowledge.config import IndexingSettings, RetrievalSettings
from tele_bot.knowledge.evaluation import CASES, evaluate
from tele_bot.knowledge.ingestion import (
    DocumentParser,
    TextChunk,
    TextChunker,
    sha256_text,
)
from tele_bot.knowledge.models import DocumentSource
from tele_bot.knowledge.search import (
    KnowledgeIndexer,
    KnowledgeSearch,
    reciprocal_rank_fusion,
)
from tele_bot.llm.embeddings import (
    AliBailianEmbeddingClient,
    EmbeddingAuthenticationError,
)
from tele_bot.persistence.kb_store import KnowledgeStore
from tests.knowledge.support import FakeEmbeddingClient, embedding_settings


def embedding_client_settings(**overrides):
    values = dict(
        api_key="chat",
        base_url="https://chat.test/v1",
        model="qwen-plus",
        reasoning_model="qwen",
        timeout_seconds=60,
        embedding_api_key="embed",
        embedding_base_url="https://embed.test/v1",
        embedding_dimensions=3,
        embedding_batch_size=2,
    )
    values.update(overrides)
    return AliBailianSettings(**values)


class EmbeddingClientTests(unittest.TestCase):
    def test_batches_and_preserves_response_order_and_usage(self) -> None:
        post = Mock()
        responses = []
        for count, tokens in ((2, 5), (1, 2)):
            response = Mock(spec=httpx.Response)
            response.status_code = 200
            response.is_success = True
            response.headers = {"x-request-id": f"req-{count}"}
            response.json.return_value = {
                "model": "text-embedding-v4",
                "data": [
                    {"index": index, "embedding": [float(index), 1.0, 2.0]}
                    for index in reversed(range(count))
                ],
                "usage": {"prompt_tokens": tokens},
            }
            responses.append(response)
        post.side_effect = responses
        batches = AliBailianEmbeddingClient(
            embedding_client_settings(), post=post
        ).embed(["a", "b", "c"])
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].input_tokens, 5)
        self.assertEqual(batches[0].vectors[0], (0.0, 1.0, 2.0))
        self.assertEqual(post.call_args_list[0].kwargs["json"]["dimensions"], 3)

    def test_authentication_error_is_classified_without_retry(self) -> None:
        response = Mock(spec=httpx.Response)
        response.status_code = 401
        response.is_success = False
        post = Mock(return_value=response)
        with self.assertRaises(EmbeddingAuthenticationError):
            AliBailianEmbeddingClient(embedding_client_settings(), post=post).embed(
                ["a"]
            )
        self.assertEqual(post.call_count, 1)

    def test_rejects_empty_or_oversized_input_before_request(self) -> None:
        client = AliBailianEmbeddingClient(
            embedding_client_settings(), post=Mock(), max_input_chars=3
        )
        with self.assertRaises(ValueError):
            client.embed([" "])
        with self.assertRaises(ValueError):
            client.embed(["long"])


class LineChunker:
    def split(self, text):
        return [
            TextChunk(index, line, sha256_text(line))
            for index, line in enumerate(text.splitlines())
            if line
        ]


class KnowledgeSearchTests(unittest.TestCase):
    def setUp(self):
        self.store = KnowledgeStore(":memory:")
        self.store.create_space("chat", "Space", "admin")
        self.store.add_member("chat", "admin", "member")
        self.client = FakeEmbeddingClient()

    def tearDown(self):
        self.store.close()

    def test_unchanged_document_does_not_call_embedding_again(self):
        parsed = DocumentParser().parse_bytes("a.md", b"alpha")
        indexer = KnowledgeIndexer(self.store, self.client, embedding_settings())
        first = indexer.enqueue(
            chat_id="chat",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="a",
            parsed=parsed,
        )
        indexer.run("chat", "admin", first)
        calls = len(self.client.calls)
        second = indexer.enqueue(
            chat_id="chat",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="a",
            parsed=parsed,
        )
        self.assertFalse(second.changed)
        self.assertIsNone(indexer.run("chat", "admin", second))
        self.assertEqual(len(self.client.calls), calls)

    def test_changed_version_only_embeds_new_chunks(self):
        indexer = KnowledgeIndexer(
            self.store, self.client, embedding_settings(), chunker=LineChunker()
        )
        first = indexer.enqueue(
            chat_id="chat",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="incremental",
            parsed=DocumentParser().parse_bytes("a.md", b"alpha\nbeta"),
        )
        indexer.run("chat", "admin", first)
        second = indexer.enqueue(
            chat_id="chat",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="incremental",
            parsed=DocumentParser().parse_bytes("a.md", b"alpha\ngamma"),
        )
        indexer.run("chat", "admin", second)
        self.assertEqual(self.client.calls, [["alpha", "beta"], ["gamma"]])

    def test_budget_exhaustion_pauses_job_before_external_call(self):
        client = FakeEmbeddingClient()
        indexer = KnowledgeIndexer(
            self.store, client, embedding_settings(token_budget=1)
        )
        pending = indexer.enqueue(
            chat_id="chat",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="budget",
            parsed=DocumentParser().parse_bytes("a.md", b"alpha facts"),
        )
        job = indexer.run("chat", "admin", pending)
        self.assertEqual(job.status.value, "paused_budget")
        self.assertEqual(client.calls, [])


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.store = KnowledgeStore(":memory:")
        self.store.create_space("chat", "Team", "admin")
        self.store.add_member("chat", "admin", "reader")
        self.client = FakeEmbeddingClient()
        self.indexer = KnowledgeIndexer(self.store, self.client, embedding_settings())

    def tearDown(self):
        self.store.close()

    def index(self, text, ref="a", indexer=None):
        parsed = DocumentParser().parse_bytes(ref + ".md", text.encode())
        engine = indexer or self.indexer
        pending = engine.enqueue(
            chat_id="chat",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref=ref,
            parsed=parsed,
        )
        job = engine.run("chat", "admin", pending)
        if pending.changed:
            self.assertEqual(job.status.value, "succeeded")
        else:
            self.assertIsNone(job)
        return pending

    def search(self, mode="hybrid", **kwargs):
        return KnowledgeSearch(
            self.store,
            self.client,
            embedding_settings(),
            retrieval=RetrievalSettings(mode=mode),
            **kwargs,
        )

    def test_rrf_uses_rank_not_raw_score_and_ties_are_stable(self):
        self.index("# Device\n\nMX-204 alpha", "z")
        self.index("alpha generic", "a")
        vector = self.search("vector").search("chat", "reader", "MX-204 alpha")
        keyword = self.search("keyword").search("chat", "reader", "MX-204")
        scores = reciprocal_rank_fusion([vector, keyword], 60)
        target = keyword[0].chunk_id
        vrank = next(i for i, hit in enumerate(vector, 1) if hit.chunk_id == target)
        self.assertAlmostEqual(scores[target], 1 / (60 + vrank) + 1 / 61)
        hybrid = self.search().search("chat", "reader", "MX-204")
        self.assertEqual(hybrid[0].source_ref, "z")
        self.assertIsNotNone(hybrid[0].keyword_rank)
        self.assertEqual(hybrid[0].section_path, ("Device",))
        self.assertEqual(hybrid[0].rank, 1)
        self.assertEqual(vector[0].source_ref, "a")

    def test_lexical_and_vector_work_overlap(self):
        self.index("alpha MX-204")
        lexical_started, vector_started = Event(), Event()
        from tele_bot.knowledge.search import keyword_candidates

        original_embed = self.client.embed

        def embed(texts):
            vector_started.set()
            self.assertTrue(lexical_started.wait(3))
            return original_embed(texts)

        def lexical(*args):
            lexical_started.set()
            self.assertTrue(vector_started.wait(3))
            return keyword_candidates(*args)

        with (
            patch.object(self.client, "embed", side_effect=embed),
            patch("tele_bot.knowledge.search.keyword_candidates", side_effect=lexical),
        ):
            self.assertTrue(self.search().search("chat", "reader", "MX-204"))

    def test_all_modes_enforce_current_version_space_and_delete(self):
        old = self.index("alpha oldmodel")
        current = self.index("beta newmodel")
        self.store.create_space("private", "Private", "outsider")
        for mode in ("hybrid", "keyword", "vector"):
            engine = self.search(mode)
            hits = engine.search("chat", "reader", "newmodel beta")
            self.assertTrue(hits)
            self.assertEqual(hits[0].title, "a.md")
            if mode == "vector":
                self.assertGreater(hits[0].score, 0.9)
            self.assertTrue(
                all(hit.document_version_id == current.version.id for hit in hits)
            )
            self.assertFalse(
                any(hit.document_version_id == old.version.id for hit in hits)
            )
            calls = len(self.client.calls)
            with self.assertRaises(PermissionError):
                engine.search("chat", "stranger", "newmodel")
            with self.assertRaises(PermissionError):
                engine.search("private", "reader", "newmodel")
            self.assertEqual(len(self.client.calls), calls)
        self.store.delete_document("chat", "admin", current.document.id)
        for mode in ("hybrid", "keyword", "vector"):
            self.assertEqual(self.search(mode).search("chat", "reader", "newmodel"), [])

    def test_historical_reimport_becomes_current_without_reembedding(self):
        first = self.index("alpha old")
        self.index("beta new")
        calls = len(self.client.calls)
        restored = self.index("alpha old")
        self.assertEqual(restored.version.id, first.version.id)
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(self.store.list_chunks("chat", "reader")[0].text, "alpha old")

    def test_prefix_and_model_change_invalidate_reuse_but_display_is_original(self):
        self.index("# Section\n\nalpha facts")
        prefix_indexer = KnowledgeIndexer(
            self.store, self.client, embedding_settings(), embedding_context_prefix=True
        )
        self.index("# Section\n\nalpha facts", indexer=prefix_indexer)
        chunk = self.store.list_chunks("chat", "reader")[0]
        self.assertIn("Document: a.md", chunk.embedding_prefix)
        self.assertNotIn("Document:", chunk.text)
        calls = len(self.client.calls)
        self.index("# Section\n\nalpha facts", indexer=prefix_indexer)
        self.assertEqual(len(self.client.calls), calls)
        from dataclasses import replace

        new_indexer = KnowledgeIndexer(
            self.store,
            self.client,
            replace(embedding_settings(), embedding_model="new-model"),
        )
        self.index("# Section\n\nalpha facts", indexer=new_indexer)
        self.assertEqual(len(self.client.calls), calls + 1)

    def test_identical_chunks_at_different_locations_share_vector_but_keep_citations(
        self,
    ):
        self.index(
            "# One\n\nalpha\n\n# Two\n\nalpha",
            indexer=KnowledgeIndexer(
                self.store,
                self.client,
                embedding_settings(),
                chunker=TextChunker(max_chars=7, overlap_chars=0),
            ),
        )
        chunks = [
            chunk
            for chunk in self.store.list_chunks("chat", "reader")
            if chunk.text == "alpha"
        ]
        self.assertEqual(len(chunks), 2)
        self.assertNotEqual(chunks[0].section_path, chunks[1].section_path)
        self.assertNotEqual(chunks[0].source_locator, chunks[1].source_locator)
        self.assertEqual(self.client.calls[0].count("alpha"), 1)

    def test_expansion_keeps_original_caps_variants_and_failure_falls_back(self):
        self.index("alpha model")
        engine = KnowledgeSearch(
            self.store,
            self.client,
            embedding_settings(),
            retrieval=RetrievalSettings(
                mode="hybrid", expansion_enabled=True, max_expansions=1
            ),
            query_expander=lambda query: ["model", "ignored"],
        )
        engine.search("chat", "reader", "alpha")
        self.assertEqual(self.client.calls[-1], ["alpha", "model"])
        engine.query_expander = lambda query: 1 / 0
        with self.assertWarns(UserWarning):
            engine.search("chat", "reader", "alpha")
        self.assertEqual(self.client.calls[-1], ["alpha"])

    def test_keyword_works_without_vectors_and_budget(self):
        pending = self.index("alpha MX-204")
        with self.store._transaction() as conn:
            conn.execute(
                "UPDATE knowledge_chunks SET embedding=NULL WHERE document_id=?",
                (pending.document.id,),
            )
        self.assertEqual(
            self.search("keyword").search("chat", "reader", "MX-204")[0].source_ref, "a"
        )
        self.assertEqual(self.search("vector").search("chat", "reader", "MX-204"), [])
        self.assertEqual(
            self.search("keyword").search("chat", "reader", "not-found"), []
        )

    def test_report_is_reproducible_and_distinguishes_negatives(self):
        corpus = json.loads(CASES.read_text(encoding="utf-8"))
        baseline, hybrid, expanded, prefixed = evaluate(corpus)
        self.assertGreater(hybrid["mrr5"], baseline["mrr5"])
        self.assertEqual(hybrid["recall5"], baseline["recall5"])
        self.assertEqual(hybrid["denials"], 2)
        self.assertEqual(hybrid["false_positives"], 2)

    def test_configuration_reads_local_file_and_environment_overrides(self):
        with (
            patch(
                "tele_bot.knowledge.config._load_local_env",
                return_value={
                    "KB_SEARCH_MODE": "keyword",
                    "KB_SEARCH_RESULTS": "3",
                    "KB_CHUNK_MAX_CHARS": "500",
                },
            ),
            patch.dict(os.environ, {"KB_SEARCH_MODE": "hybrid"}, clear=True),
        ):
            config = RetrievalSettings.from_env()
            self.assertEqual((config.mode, config.result_limit), ("hybrid", 3))
            self.assertEqual(IndexingSettings.from_env().max_chars, 500)
        with self.assertRaises(ValueError):
            RetrievalSettings(result_limit=21)

    def test_configured_aliases_are_opt_in_and_original_query_is_preserved(self):
        self.index("alpha MX-204")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.json"
            path.write_text(json.dumps({"device": ["MX-204"]}), encoding="utf-8")
            with patch.dict(os.environ, {"KB_QUERY_EXPANSIONS_FILE": str(path)}):
                engine = KnowledgeSearch(
                    self.store,
                    self.client,
                    embedding_settings(),
                    retrieval=RetrievalSettings(mode="hybrid", expansion_enabled=True),
                )
                engine.search("chat", "reader", "device")
                self.assertEqual(self.client.calls[-1], ["device", "MX-204"])

    def test_reused_vectors_need_no_remaining_embedding_budget(self):
        self.index("alpha old")
        self.index("beta new")
        calls = len(self.client.calls)
        self.store.record_embedding_usage(
            self.store.get_space("chat", "admin").id, "fixture", 100, 1
        )
        indexer = KnowledgeIndexer(
            self.store, self.client, embedding_settings(token_budget=1)
        )
        restored = self.index("alpha old", indexer=indexer)
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(
            self.store.list_chunks("chat", "reader")[0].document_version_id,
            restored.version.id,
        )
