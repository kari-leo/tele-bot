"""Budgeted indexing and project-scoped cosine search."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.knowledge.ingestion import ParsedDocument, TextChunker
from tele_bot.knowledge.models import (
    DocumentSource,
    DocumentVersion,
    IndexJob,
    IndexJobStatus,
    KnowledgeDocument,
    SearchHit,
)
from tele_bot.llm.embeddings import AliBailianEmbeddingClient, EmbeddingBatchResult
from tele_bot.persistence.kb_store import KnowledgeStore


class EmbeddingBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingIndex:
    document: KnowledgeDocument
    version: DocumentVersion
    job: IndexJob | None
    parsed: ParsedDocument
    changed: bool


class KnowledgeIndexer:
    def __init__(
        self,
        store: KnowledgeStore,
        embedding_client: AliBailianEmbeddingClient,
        settings: AliBailianSettings,
        *,
        chunker: TextChunker | None = None,
        cost_per_1000_tokens_cny: float = 0.00025,
    ) -> None:
        self.store, self.client, self.settings = store, embedding_client, settings
        self.chunker = chunker or TextChunker()
        self.cost_per_1000_tokens_cny = cost_per_1000_tokens_cny

    def enqueue(
        self, *, chat_id: str, actor_user_id: str, source: DocumentSource,
        source_ref: str, parsed: ParsedDocument, source_url: str | None = None
    ) -> PendingIndex:
        document, version, changed = self.store.upsert_document_version(
            chat_id=chat_id, actor_user_id=actor_user_id, source=source,
            source_ref=source_ref, title=parsed.name, content_sha256=parsed.content_sha256,
            source_url=source_url,
        )
        job = self.store.create_index_job(chat_id, actor_user_id, document.id) if changed else None
        return PendingIndex(document, version, job, parsed, changed)

    def run(self, chat_id: str, actor_user_id: str, pending: PendingIndex) -> IndexJob | None:
        if not pending.changed or pending.job is None:
            return None
        job = self.store.transition_index_job(pending.job.id, IndexJobStatus.RUNNING)
        try:
            chunks = self.chunker.split(pending.parsed.text)
            reused = self.store.find_chunk_embeddings(
                chat_id, actor_user_id, [chunk.content_sha256 for chunk in chunks]
            )
            missing = [chunk for chunk in chunks if chunk.content_sha256 not in reused]
            estimated_tokens = sum(max(1, math.ceil(len(chunk.text) / 4)) for chunk in missing)
            self._ensure_budget(pending.document.project_space_id, estimated_tokens)
            results = self.client.embed([chunk.text for chunk in missing])
            vectors = [vector for result in results for vector in result.vectors]
            for chunk, vector in zip(missing, vectors, strict=True):
                reused[chunk.content_sha256] = encode_vector(vector)
            for result in results:
                self._record_usage(pending.document.project_space_id, result)
            self.store.replace_chunks(
                chat_id=chat_id, actor_user_id=actor_user_id,
                document_id=pending.document.id, document_version_id=pending.version.id,
                chunks=[
                    (chunk.text, chunk.content_sha256, reused[chunk.content_sha256],
                     max(1, math.ceil(len(chunk.text) / 4)))
                    for chunk in chunks
                ],
            )
        except EmbeddingBudgetExceeded as exc:
            return self.store.transition_index_job(
                job.id, IndexJobStatus.PAUSED_BUDGET, error=str(exc)
            )
        except Exception as exc:
            return self.store.transition_index_job(job.id, IndexJobStatus.FAILED, error=str(exc))
        return self.store.transition_index_job(job.id, IndexJobStatus.SUCCEEDED)

    def _ensure_budget(self, space_id: str, estimated_tokens: int) -> None:
        used_tokens, used_cost = self.store.daily_embedding_usage(space_id)
        estimated_cost = estimated_tokens / 1000 * self.cost_per_1000_tokens_cny
        if (
            used_tokens + estimated_tokens > self.settings.embedding_daily_token_budget
            or used_cost + estimated_cost > self.settings.embedding_daily_cost_budget_cny
        ):
            raise EmbeddingBudgetExceeded("project daily embedding budget is exhausted")

    def _record_usage(self, space_id: str, result: EmbeddingBatchResult) -> None:
        self.store.record_embedding_usage(
            space_id, result.model, result.input_tokens,
            result.input_tokens / 1000 * self.cost_per_1000_tokens_cny,
            result.request_id,
        )


class KnowledgeSearch:
    def __init__(
        self, store: KnowledgeStore, embedding_client: AliBailianEmbeddingClient,
        settings: AliBailianSettings, *, cost_per_1000_tokens_cny: float = 0.00025
    ) -> None:
        self.store, self.client, self.settings = store, embedding_client, settings
        self.cost_per_1000_tokens_cny = cost_per_1000_tokens_cny

    def search(self, chat_id: str, user_id: str, query: str, *, limit: int = 5) -> list[SearchHit]:
        space = self.store.require_member(chat_id, user_id)
        return self._search(
            space.id,
            user_id,
            query,
            limit=limit,
            material=self.store.list_search_material(chat_id, user_id),
        )

    def search_space(
        self, project_space_id: str, user_id: str, query: str, *, limit: int = 5
    ) -> list[SearchHit]:
        """Search one project space after direct membership authorization."""
        self.store.require_space_member(project_space_id, user_id)
        return self._search(
            project_space_id,
            user_id,
            query,
            limit=limit,
            material=self.store.list_search_material_for_space(
                project_space_id, user_id
            ),
        )

    def _search(
        self,
        project_space_id: str,
        user_id: str,
        query: str,
        *,
        limit: int,
        material,
    ) -> list[SearchHit]:
        if not query.strip() or limit <= 0:
            raise ValueError("query and a positive limit are required")
        estimate = max(1, math.ceil(len(query) / 4))
        used_tokens, used_cost = self.store.daily_embedding_usage(project_space_id)
        if (
            used_tokens + estimate > self.settings.embedding_daily_token_budget
            or used_cost + estimate / 1000 * self.cost_per_1000_tokens_cny
            > self.settings.embedding_daily_cost_budget_cny
        ):
            raise EmbeddingBudgetExceeded("project daily embedding budget is exhausted")
        results = self.client.embed([query])
        if len(results) != 1 or len(results[0].vectors) != 1:
            raise RuntimeError("query embedding returned an unexpected batch shape")
        result = results[0]
        self.store.record_embedding_usage(
            project_space_id, result.model, result.input_tokens,
            result.input_tokens / 1000 * self.cost_per_1000_tokens_cny, result.request_id,
        )
        query_vector = result.vectors[0]
        hits = []
        for chunk, document in material:
            vector = decode_vector(chunk.embedding or b"")
            if len(vector) != len(query_vector):
                continue
            hits.append(
                SearchHit(chunk.id, document.id, document.title, document.source,
                    document.source_ref, chunk.ordinal, chunk.text,
                    cosine_similarity(query_vector, vector))
            )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]


def encode_vector(vector: tuple[float, ...] | list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_vector(value: bytes) -> tuple[float, ...]:
    if not value or len(value) % 4:
        return ()
    return struct.unpack(f"<{len(value) // 4}f", value)


def cosine_similarity(left, right) -> float:
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0
