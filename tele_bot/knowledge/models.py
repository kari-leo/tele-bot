"""Stable, persistence-neutral contracts for the project knowledge base."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MemberRole(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"


class DocumentSource(str, Enum):
    REPORT = "report"
    BOUND_DIRECTORY = "bound_directory"
    FEISHU_UPLOAD = "feishu_upload"
    FEISHU_DOCUMENT = "feishu_document"


class DocumentStatus(str, Enum):
    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"
    METADATA_ONLY = "metadata_only"
    DELETED = "deleted"


class IndexJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED_BUDGET = "paused_budget"


@dataclass(frozen=True)
class ProjectSpace:
    id: str
    chat_id: str
    name: str
    created_by: str
    created_at: str
    allowed_directory: str | None = None


@dataclass(frozen=True)
class ProjectMember:
    project_space_id: str
    user_id: str
    role: MemberRole
    created_at: str


@dataclass(frozen=True)
class KnowledgeDocument:
    id: str
    project_space_id: str
    source: DocumentSource
    source_ref: str
    title: str
    status: DocumentStatus
    created_by: str
    created_at: str
    updated_at: str
    source_url: str | None = None


@dataclass(frozen=True)
class DocumentVersion:
    id: str
    document_id: str
    version: int
    content_sha256: str
    source_version: str | None
    created_at: str


@dataclass(frozen=True)
class KnowledgeChunk:
    id: str
    document_id: str
    document_version_id: str
    project_space_id: str
    ordinal: int
    text: str
    content_sha256: str
    embedding: bytes | None = None
    token_count: int | None = None


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    document_id: str
    title: str
    source: DocumentSource
    source_ref: str
    ordinal: int
    text: str
    score: float


@dataclass(frozen=True)
class IndexJob:
    id: str
    project_space_id: str
    document_id: str
    status: IndexJobStatus
    attempts: int
    created_at: str
    updated_at: str
    error: str | None = None


@dataclass(frozen=True)
class EmbeddingUsage:
    id: str
    project_space_id: str
    model: str
    input_tokens: int
    estimated_cost_cny: float
    created_at: str
    request_id: str | None = None
