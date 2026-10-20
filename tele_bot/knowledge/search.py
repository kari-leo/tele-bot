"""Budgeted indexing and authorized keyword/vector retrieval with rank fusion."""

from __future__ import annotations

import math
import struct
import json
import re
import warnings
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.knowledge.ingestion import ParsedDocument, TextChunker, sha256_text
from tele_bot.knowledge.config import RetrievalSettings, kb_option
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
        embedding_context_prefix: bool = False,
    ) -> None:
        self.store, self.client, self.settings = store, embedding_client, settings
        self.chunker = chunker or TextChunker()
        self.cost_per_1000_tokens_cny = cost_per_1000_tokens_cny
        self.embedding_context_prefix = embedding_context_prefix

    def _profile(self) -> str:
        return sha256_text(json.dumps({
            "format": "structure-v2", "chunker": type(self.chunker).__name__,
            "max_chars": getattr(self.chunker, "max_chars", None),
            "overlap_chars": getattr(self.chunker, "overlap_chars", None),
            "prefix": self.embedding_context_prefix,
            "model": self.settings.embedding_model, "dimensions": self.settings.embedding_dimensions,
            "endpoint": self.settings.embedding_base_url,
        }, sort_keys=True))

    def enqueue(
        self, *, chat_id: str, actor_user_id: str, source: DocumentSource,
        source_ref: str, parsed: ParsedDocument, source_url: str | None = None
    ) -> PendingIndex:
        document, version, changed = self.store.upsert_document_version(
            chat_id=chat_id, actor_user_id=actor_user_id, source=source,
            source_ref=source_ref, title=parsed.name, content_sha256=parsed.content_sha256,
            source_url=source_url,
            source_version=self._profile(),
        )
        job = self.store.create_index_job(chat_id, actor_user_id, document.id) if changed else None
        return PendingIndex(document, version, job, parsed, changed)

    def run(self, chat_id: str, actor_user_id: str, pending: PendingIndex) -> IndexJob | None:
        if not pending.changed or pending.job is None:
            return None
        job = self.store.transition_index_job(pending.job.id, IndexJobStatus.RUNNING)
        try:
            chunks = (self.chunker.split_document(pending.parsed) if isinstance(self.chunker, TextChunker)
                      else self.chunker.split(pending.parsed.text))
            metadata, inputs = [], {}
            for chunk in chunks:
                prefix = (f"Document: {pending.parsed.name}\nSection: {' / '.join(chunk.section_path)}\n\n"
                          if self.embedding_context_prefix else "")
                if len(prefix) > 2000:
                    raise ValueError("embedding context prefix exceeds 2000 characters")
                embedding_input = prefix + chunk.text
                key = sha256_text(json.dumps([
                    self.settings.embedding_base_url, self.settings.embedding_model,
                    self.settings.embedding_dimensions, embedding_input,
                ], ensure_ascii=False))
                inputs[key] = embedding_input
                metadata.append(dict(section_path=chunk.section_path, source_locator=chunk.source_locator,
                                     start_char=chunk.start_char, end_char=chunk.end_char,
                                     embedding_prefix=prefix, embedding_key=key))
            reused = self.store.find_embeddings_by_key(chat_id, actor_user_id, list(inputs))
            missing = {key: value for key, value in inputs.items() if key not in reused}
            estimated_tokens = sum(max(1, math.ceil(len(text) / 4)) for text in missing.values())
            if missing:
                self._ensure_budget(pending.document.project_space_id, estimated_tokens)
            results = self.client.embed(list(missing.values())) if missing else []
            vectors = [vector for result in results for vector in result.vectors]
            for key, vector in zip(missing, vectors, strict=True):
                if len(vector) != self.settings.embedding_dimensions or not all(math.isfinite(x) for x in vector):
                    raise RuntimeError("document embedding has an invalid shape or value")
                reused[key] = encode_vector(vector)
            for result in results:
                self._record_usage(pending.document.project_space_id, result)
            self.store.replace_chunks(
                chat_id=chat_id, actor_user_id=actor_user_id,
                document_id=pending.document.id, document_version_id=pending.version.id,
                chunks=[
                    (chunk.text, chunk.content_sha256, reused[meta["embedding_key"]],
                     max(1, math.ceil(len(chunk.text) / 4)))
                    for chunk, meta in zip(chunks, metadata, strict=True)
                ],
                chunk_metadata=metadata,
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
        settings: AliBailianSettings, *, cost_per_1000_tokens_cny: float = 0.00025,
        retrieval: RetrievalSettings | None = None, query_expander=None,
    ) -> None:
        self.store, self.client, self.settings = store, embedding_client, settings
        self.cost_per_1000_tokens_cny = cost_per_1000_tokens_cny
        self.retrieval = retrieval or RetrievalSettings.from_env()
        self.query_expander = query_expander
        aliases_path = kb_option("KB_QUERY_EXPANSIONS_FILE")
        if self.retrieval.expansion_enabled and query_expander is None and aliases_path:
            aliases = json.loads(Path(aliases_path).read_text(encoding="utf-8"))
            if not isinstance(aliases, dict) or any(
                not isinstance(key, str) or not isinstance(values, list)
                or any(not isinstance(value, str) for value in values) for key, values in aliases.items()
            ):
                raise ValueError("query expansions file must map queries to lists of aliases")
            self.query_expander = lambda query: aliases.get(query, [])

    def search(self, chat_id: str, user_id: str, query: str, *, limit: int | None = None) -> list[SearchHit]:
        space = self.store.require_member(chat_id, user_id)
        return self._search(
            space.id,
            user_id,
            query,
            limit=limit,
            material=self.store.list_search_material(chat_id, user_id),
        )

    def search_space(
        self, project_space_id: str, user_id: str, query: str, *, limit: int | None = None
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
        limit: int | None,
        material,
    ) -> list[SearchHit]:
        limit = self.retrieval.result_limit if limit is None else limit
        if not query.strip() or len(query) > 4000 or not 1 <= limit <= 20:
            raise ValueError("query (1..4000 characters) and limit (1..20) are required")
        if not material:
            return []
        queries = [query.strip()]
        if self.retrieval.expansion_enabled and self.retrieval.max_expansions and self.query_expander is not None:
            try:
                expanded = self.query_expander(query)
                if not isinstance(expanded, (list, tuple)):
                    raise ValueError("query expander must return a list")
                for value in expanded:
                    if isinstance(value, str) and value.strip() and len(value) <= 4000 and value.strip() not in queries:
                        queries.append(value.strip())
                    if len(queries) >= 1 + self.retrieval.max_expansions:
                        break
            except Exception as exc:
                warnings.warn(f"query expansion unavailable; using original query: {type(exc).__name__}", UserWarning)
                queries = [query.strip()]
        candidate_limit = max(limit, self.retrieval.candidate_limit)
        # Both branches see exactly the same authorized current-version snapshot.
        # Only vector work performs I/O; it overlaps the local lexical scoring.
        if self.retrieval.mode == "hybrid":
            with ThreadPoolExecutor(max_workers=2) as pool:
                vector_future = pool.submit(self._vector_candidates, project_space_id, queries, material)
                keyword_future = pool.submit(keyword_candidates, queries, material)
                vectors, keywords = vector_future.result(), keyword_future.result()
        elif self.retrieval.mode == "keyword":
            vectors, keywords = [], keyword_candidates(queries, material)
        else:
            vectors, keywords = self._vector_candidates(project_space_id, queries, material), []
        vectors, keywords = vectors[:candidate_limit], keywords[:candidate_limit]
        vector_ranks = {hit.chunk_id: rank for rank, hit in enumerate(vectors, 1)}
        keyword_ranks = {hit.chunk_id: rank for rank, hit in enumerate(keywords, 1)}
        vector_scores = {hit.chunk_id: hit.score for hit in vectors}
        keyword_scores = {hit.chunk_id: hit.score for hit in keywords}
        hits = {hit.chunk_id: hit for hit in vectors + keywords}
        if self.retrieval.mode == "hybrid":
            scores = reciprocal_rank_fusion([vectors, keywords], self.retrieval.rrf_constant)
        else:
            scores = {key: hit.score for key, hit in hits.items()}
        ordered = sorted(hits.values(), key=lambda h: (-scores[h.chunk_id], hit_order(h)))[:limit]
        return [replace(hit, score=scores[hit.chunk_id], rank=rank,
                        keyword_rank=keyword_ranks.get(hit.chunk_id), vector_rank=vector_ranks.get(hit.chunk_id),
                        keyword_score=keyword_scores.get(hit.chunk_id), vector_score=vector_scores.get(hit.chunk_id))
                for rank, hit in enumerate(ordered, 1)]

    def _vector_candidates(self, project_space_id, queries, material):
        estimate = sum(max(1, math.ceil(len(query) / 4)) for query in queries)
        used_tokens, used_cost = self.store.daily_embedding_usage(project_space_id)
        if (
            used_tokens + estimate > self.settings.embedding_daily_token_budget
            or used_cost + estimate / 1000 * self.cost_per_1000_tokens_cny
            > self.settings.embedding_daily_cost_budget_cny
        ):
            raise EmbeddingBudgetExceeded("project daily embedding budget is exhausted")
        results = self.client.embed(queries)
        query_vectors = [vector for result in results for vector in result.vectors]
        if len(query_vectors) != len(queries) or any(
            len(vector) != self.settings.embedding_dimensions or not all(math.isfinite(x) for x in vector)
            for vector in query_vectors
        ):
            raise RuntimeError("query embedding returned an unexpected batch shape")
        for result in results:
            self.store.record_embedding_usage(
                project_space_id, result.model, result.input_tokens,
                result.input_tokens / 1000 * self.cost_per_1000_tokens_cny, result.request_id,
            )
        hits = []
        for chunk, document in material:
            expected_key = sha256_text(json.dumps([
                self.settings.embedding_base_url, self.settings.embedding_model,
                self.settings.embedding_dimensions, chunk.embedding_prefix + chunk.text,
            ], ensure_ascii=False))
            if chunk.embedding_key and chunk.embedding_key != expected_key:
                continue
            vector = decode_vector(chunk.embedding or b"")
            if len(vector) != self.settings.embedding_dimensions or not all(math.isfinite(x) for x in vector):
                continue
            score = max(cosine_similarity(query_vector, vector) for query_vector in query_vectors)
            if score >= self.retrieval.min_vector_score:
                hits.append(make_hit(chunk, document, score))
        return sorted(hits, key=lambda h: (-h.score, hit_order(h)))


def make_hit(chunk, document, score):
    return SearchHit(chunk.id, document.id, document.title, document.source,
                     document.source_ref, chunk.ordinal, chunk.text, score,
                     chunk.section_path, chunk.source_locator, chunk.document_version_id, document.source_url)


def hit_order(hit):
    return hit.source.value, hit.source_ref, hit.ordinal, hit.chunk_id


def tokenize(text: str) -> list[str]:
    """Keep full model names; use CJK bigrams without an external dictionary."""
    tokens = []
    for match in re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+", text.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", match):
            tokens.extend(match[i:i + 2] for i in range(max(1, len(match) - 1)))
        else:
            tokens.append(match)
    return tokens


def keyword_candidates(queries, material):
    """BM25 over the already authorized snapshot (k1=1.2, b=0.75)."""
    bags = [Counter(tokenize(document.title + " " + " / ".join(chunk.section_path) + " " + chunk.text))
            for chunk, document in material]
    lengths = [sum(bag.values()) for bag in bags]
    average = sum(lengths) / len(lengths) if lengths else 1
    frequencies = Counter(term for bag in bags for term in bag)
    query_terms = [set(tokenize(query)) for query in queries]
    hits = []
    for (chunk, document), bag, length in zip(material, bags, lengths, strict=True):
        scores = [sum(
            math.log(1 + (len(bags) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            * bag[term] * 2.2 / (bag[term] + 1.2 * (0.25 + 0.75 * length / (average or 1)))
            for term in terms if bag[term]
        ) for terms in query_terms]
        score = max(scores, default=0)
        if score > 0:
            hits.append(make_hit(chunk, document, score))
    return sorted(hits, key=lambda h: (-h.score, hit_order(h)))


def reciprocal_rank_fusion(rankings, constant: int = 60) -> dict[str, float]:
    if constant < 1:
        raise ValueError("RRF constant must be positive")
    scores = {}
    for ranking in rankings:
        seen = set()
        for rank, hit in enumerate(ranking, 1):
            if hit.chunk_id not in seen:
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + 1 / (constant + rank)
                seen.add(hit.chunk_id)
    return scores


def encode_vector(vector: tuple[float, ...] | list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_vector(value: bytes) -> tuple[float, ...]:
    if not value or len(value) % 4:
        return ()
    return struct.unpack(f"<{len(value) // 4}f", value)


def cosine_similarity(left, right) -> float:
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0
