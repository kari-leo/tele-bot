"""Permission-preserving MCP facade over the project knowledge base."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from tele_bot.knowledge.search import KnowledgeSearch
from tele_bot.persistence.kb_store import KnowledgeStore


@dataclass(frozen=True)
class KnowledgeMCPService:
    store: KnowledgeStore
    search_engine: KnowledgeSearch
    user_id: str

    def list_spaces(self) -> dict:
        spaces = self.store.list_accessible_spaces(self.user_id)
        return {
            "spaces": [
                {
                    "id": space.id,
                    "name": space.name,
                    "created_by": space.created_by,
                    "created_at": space.created_at,
                }
                for space in spaces
            ]
        }

    def search(self, project_space_id: str, query: str, limit: int = 5) -> dict:
        limit = max(1, min(limit, 20))
        hits = self.search_engine.search_space(
            project_space_id, self.user_id, query, limit=limit
        )
        return {
            "project_space_id": project_space_id,
            "query": query,
            "hits": [
                {
                    "document_id": hit.document_id,
                    "title": hit.title,
                    "source": hit.source.value,
                    "source_ref": hit.source_ref,
                    "chunk_ordinal": hit.ordinal,
                    "text": hit.text,
                    "score": hit.score,
                }
                for hit in hits
            ],
        }

    def list_documents(self, project_space_id: str) -> dict:
        documents = self.store.list_documents_for_space(
            project_space_id, self.user_id
        )
        return {
            "project_space_id": project_space_id,
            "documents": [asdict(document) for document in documents],
        }

    def budget(self, project_space_id: str) -> dict:
        self.store.require_space_member(project_space_id, self.user_id)
        tokens, cost = self.store.daily_embedding_usage(project_space_id)
        return {
            "project_space_id": project_space_id,
            "tokens_today": tokens,
            "estimated_cost_cny_today": cost,
        }
