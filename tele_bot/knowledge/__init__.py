"""Project-scoped knowledge-base domain and ingestion services."""

from tele_bot.knowledge.models import (
    DocumentSource,
    DocumentStatus,
    DocumentVersion,
    EmbeddingUsage,
    IndexJob,
    IndexJobStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    MemberRole,
    ProjectMember,
    ProjectSpace,
    SearchHit,
)

__all__ = [
    "DocumentSource",
    "DocumentStatus",
    "DocumentVersion",
    "EmbeddingUsage",
    "IndexJob",
    "IndexJobStatus",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "MemberRole",
    "ProjectMember",
    "ProjectSpace",
    "SearchHit",
]
