"""SQLite persistence for project-scoped knowledge with mandatory membership checks."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator, Sequence
from uuid import uuid4
from datetime import datetime, timezone

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
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeStore:
    """Owns KB schema and enforces chat + user authorization before every read."""

    def __init__(self, path: str | Path, *, max_chunks_per_space: int = 10_000) -> None:
        if max_chunks_per_space <= 0:
            raise ValueError("max_chunks_per_space must be greater than zero")
        path_text = str(path)
        if path_text != ":memory:":
            Path(path_text).parent.mkdir(parents=True, exist_ok=True)
        self.max_chunks_per_space = max_chunks_per_space
        self._lock = RLock()
        self._conn = sqlite3.connect(path_text, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        if path_text != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise RuntimeError(f"knowledge DB schema {version} is newer than supported schema 3")
            if version == 0:
                self._conn.executescript(
                """
                CREATE TABLE project_spaces (
                    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    created_by TEXT NOT NULL, allowed_directory TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE project_members (
                    project_space_id TEXT NOT NULL REFERENCES project_spaces(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','member')),
                    created_at TEXT NOT NULL, PRIMARY KEY(project_space_id, user_id)
                );
                CREATE TABLE knowledge_documents (
                    id TEXT PRIMARY KEY,
                    project_space_id TEXT NOT NULL REFERENCES project_spaces(id) ON DELETE CASCADE,
                    source TEXT NOT NULL, source_ref TEXT NOT NULL, source_url TEXT,
                    title TEXT NOT NULL, status TEXT NOT NULL, created_by TEXT NOT NULL,
                    current_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(project_space_id, source, source_ref)
                );
                CREATE TABLE document_versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL, content_sha256 TEXT NOT NULL, source_version TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, version), UNIQUE(document_id, content_sha256)
                );
                CREATE TABLE knowledge_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    document_version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
                    project_space_id TEXT NOT NULL REFERENCES project_spaces(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, text TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    embedding BLOB, token_count INTEGER,
                    UNIQUE(document_version_id, ordinal), UNIQUE(document_version_id, content_sha256)
                );
                CREATE TABLE embedding_usage (
                    id TEXT PRIMARY KEY,
                    project_space_id TEXT NOT NULL REFERENCES project_spaces(id) ON DELETE CASCADE,
                    model TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                    estimated_cost_cny REAL NOT NULL, request_id TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE index_jobs (
                    id TEXT PRIMARY KEY,
                    project_space_id TEXT NOT NULL REFERENCES project_spaces(id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX idx_members_user ON project_members(user_id, project_space_id);
                CREATE INDEX idx_documents_space_updated ON knowledge_documents(project_space_id, updated_at);
                CREATE INDEX idx_documents_status ON knowledge_documents(project_space_id, status);
                CREATE INDEX idx_versions_hash ON document_versions(document_id, content_sha256);
                CREATE INDEX idx_chunks_space ON knowledge_chunks(project_space_id, document_id);
                CREATE INDEX idx_chunks_hash ON knowledge_chunks(project_space_id, content_sha256);
                CREATE INDEX idx_usage_space_time ON embedding_usage(project_space_id, created_at);
                CREATE INDEX idx_jobs_space_status ON index_jobs(project_space_id, status, updated_at);
                PRAGMA user_version=1;
                """
                )
                version = 1
            if version == 1:
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS project_space_bindings (
                        chat_id TEXT PRIMARY KEY,
                        project_space_id TEXT NOT NULL REFERENCES project_spaces(id) ON DELETE CASCADE,
                        bound_by TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    INSERT OR IGNORE INTO project_space_bindings
                        (chat_id,project_space_id,bound_by,created_at)
                    SELECT chat_id,id,created_by,created_at FROM project_spaces;
                    CREATE INDEX IF NOT EXISTS idx_space_bindings_space
                        ON project_space_bindings(project_space_id, chat_id);
                    PRAGMA user_version=2;
                    """
                )
                version = 2
            if version == 2:
                columns = {
                    row[1] for row in self._conn.execute("PRAGMA table_info(project_spaces)")
                }
                if "origin_chat_id" not in columns:
                    self._conn.execute(
                        "ALTER TABLE project_spaces ADD COLUMN origin_chat_id TEXT"
                    )
                self._conn.executescript(
                    """
                    UPDATE project_spaces
                    SET origin_chat_id=COALESCE(origin_chat_id, chat_id);
                    UPDATE project_spaces SET chat_id='space:' || id
                    WHERE chat_id NOT LIKE 'space:%';
                    PRAGMA user_version=3;
                    """
                )
            self._conn.commit()

    def create_space(self, chat_id: str, name: str, creator_user_id: str) -> ProjectSpace:
        chat_id, name, creator_user_id = chat_id.strip(), name.strip(), creator_user_id.strip()
        if not chat_id or not name or not creator_user_id:
            raise ValueError("chat_id, name and creator_user_id are required")
        space_id, created_at = uuid4().hex, _now()
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO project_spaces
                   (id,chat_id,name,created_by,created_at,origin_chat_id)
                   VALUES(?,?,?,?,?,?)""",
                (space_id, f"space:{space_id}", name, creator_user_id, created_at, chat_id),
            )
            conn.execute(
                "INSERT INTO project_members(project_space_id,user_id,role,created_at) VALUES(?,?,?,?)",
                (space_id, creator_user_id, MemberRole.ADMIN.value, created_at),
            )
            conn.execute(
                """INSERT INTO project_space_bindings
                   (chat_id,project_space_id,bound_by,created_at) VALUES(?,?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                   project_space_id=excluded.project_space_id,
                   bound_by=excluded.bound_by,created_at=excluded.created_at""",
                (chat_id, space_id, creator_user_id, created_at),
            )
        return ProjectSpace(space_id, chat_id, name, creator_user_id, created_at)

    def require_member(self, chat_id: str, user_id: str, *, admin: bool = False) -> ProjectSpace:
        with self._lock:
            row = self._conn.execute(
                """SELECT s.*, m.role FROM project_space_bindings b
                   JOIN project_spaces s ON s.id=b.project_space_id
                   JOIN project_members m ON m.project_space_id=s.id
                   WHERE b.chat_id=? AND m.user_id=?""",
                (chat_id, user_id),
            ).fetchone()
        if row is None:
            raise PermissionError("当前会话未绑定可访问的项目空间，或当前用户不是该空间成员")
        if admin and row["role"] != MemberRole.ADMIN.value:
            raise PermissionError("此操作需要项目空间管理员权限")
        return self._space(row)

    def require_space_member(
        self, project_space_id: str, user_id: str, *, admin: bool = False
    ) -> ProjectSpace:
        """Authorize a user directly against a project space, without a chat binding."""
        with self._lock:
            row = self._conn.execute(
                """SELECT s.*,m.role FROM project_spaces s
                   JOIN project_members m ON m.project_space_id=s.id
                   WHERE s.id=? AND m.user_id=?""",
                (project_space_id, user_id),
            ).fetchone()
        if row is None:
            raise PermissionError("当前用户不是该项目空间成员")
        if admin and row["role"] != MemberRole.ADMIN.value:
            raise PermissionError("此操作需要项目空间管理员权限")
        return self._space(row)

    def list_accessible_spaces(self, user_id: str) -> list[ProjectSpace]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.* FROM project_spaces s
                   JOIN project_members m ON m.project_space_id=s.id
                   WHERE m.user_id=? ORDER BY s.created_at DESC""",
                (user_id,),
            ).fetchall()
        return [self._space(row) for row in rows]

    def bind_space(self, chat_id: str, user_id: str, identifier: str) -> ProjectSpace:
        normalized = identifier.strip()
        if not normalized:
            raise ValueError("项目空间 ID 或名称不能为空")
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT s.* FROM project_spaces s
                   JOIN project_members m ON m.project_space_id=s.id
                   WHERE m.user_id=? AND (s.id=? OR s.id LIKE ? OR s.name=?)
                   ORDER BY CASE WHEN s.id=? THEN 0 ELSE 1 END""",
                (user_id, normalized, f"{normalized}%", normalized, normalized),
            ).fetchall()
            unique = {row["id"]: row for row in rows}
            if not unique:
                raise PermissionError("找不到该项目空间，或当前用户没有该空间的读取权限")
            if len(unique) > 1:
                raise ValueError("项目空间名称或 ID 前缀不唯一，请使用完整空间 ID")
            row = next(iter(unique.values()))
            conn.execute(
                """INSERT INTO project_space_bindings(chat_id,project_space_id,bound_by,created_at)
                   VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                   project_space_id=excluded.project_space_id,
                   bound_by=excluded.bound_by,created_at=excluded.created_at""",
                (chat_id, row["id"], user_id, _now()),
            )
        return self._space(row)

    def add_member(
        self, chat_id: str, actor_user_id: str, user_id: str, role: MemberRole = MemberRole.MEMBER
    ) -> ProjectMember:
        space = self.require_member(chat_id, actor_user_id, admin=True)
        created_at = _now()
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO project_members(project_space_id,user_id,role,created_at)
                   VALUES(?,?,?,?) ON CONFLICT(project_space_id,user_id)
                   DO UPDATE SET role=excluded.role""",
                (space.id, user_id, role.value, created_at),
            )
        return ProjectMember(space.id, user_id, role, created_at)

    def bind_directory(self, chat_id: str, actor_user_id: str, directory: str | None) -> None:
        space = self.require_member(chat_id, actor_user_id, admin=True)
        with self._transaction() as conn:
            conn.execute(
                "UPDATE project_spaces SET allowed_directory=? WHERE id=?",
                (directory, space.id),
            )

    def list_documents(self, chat_id: str, user_id: str) -> list[KnowledgeDocument]:
        space = self.require_member(chat_id, user_id)
        return self.list_documents_for_space(space.id, user_id)

    def list_documents_for_space(
        self, project_space_id: str, user_id: str
    ) -> list[KnowledgeDocument]:
        space = self.require_space_member(project_space_id, user_id)
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM knowledge_documents WHERE project_space_id=?
                   AND status<>? ORDER BY updated_at DESC""",
                (space.id, DocumentStatus.DELETED.value),
            ).fetchall()
        return [self._document(row) for row in rows]

    def delete_document(self, chat_id: str, actor_user_id: str, document_id: str) -> None:
        space = self.require_member(chat_id, actor_user_id, admin=True)
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE knowledge_documents SET status=?,updated_at=?
                   WHERE id=? AND project_space_id=? AND status<>?""",
                (DocumentStatus.DELETED.value, _now(), document_id, space.id,
                 DocumentStatus.DELETED.value),
            )
            if cursor.rowcount != 1:
                raise KeyError(document_id)

    def get_space(self, chat_id: str, user_id: str) -> ProjectSpace:
        return self.require_member(chat_id, user_id)

    def upsert_document_version(
        self,
        *,
        chat_id: str,
        actor_user_id: str,
        source: DocumentSource,
        source_ref: str,
        title: str,
        content_sha256: str,
        source_version: str | None = None,
        source_url: str | None = None,
        require_admin: bool = True,
    ) -> tuple[KnowledgeDocument, DocumentVersion, bool]:
        space = self.require_member(chat_id, actor_user_id, admin=require_admin)
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_documents WHERE project_space_id=? AND source=? AND source_ref=?",
                (space.id, source.value, source_ref),
            ).fetchone()
            if row is None:
                document_id = uuid4().hex
                conn.execute(
                    """INSERT INTO knowledge_documents
                       (id,project_space_id,source,source_ref,source_url,title,status,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (document_id, space.id, source.value, source_ref, source_url, title,
                     DocumentStatus.PENDING.value, actor_user_id, now, now),
                )
                version_number = 1
            else:
                document_id = row["id"]
                existing_version = conn.execute(
                    "SELECT * FROM document_versions WHERE document_id=? AND content_sha256=?",
                    (document_id, content_sha256),
                ).fetchone()
                if existing_version is not None:
                    return self._document(row), self._version(existing_version), False
                version_number = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM document_versions WHERE document_id=?",
                    (document_id,),
                ).fetchone()[0]
                conn.execute(
                    """UPDATE knowledge_documents SET title=?,source_url=?,status=?,updated_at=?
                       WHERE id=?""",
                    (title, source_url, DocumentStatus.PENDING.value, now, document_id),
                )
            version_id = uuid4().hex
            conn.execute(
                """INSERT INTO document_versions
                   (id,document_id,version,content_sha256,source_version,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (version_id, document_id, version_number, content_sha256, source_version, now),
            )
            conn.execute(
                "UPDATE knowledge_documents SET current_version_id=? WHERE id=?",
                (version_id, document_id),
            )
            document_row = conn.execute(
                "SELECT * FROM knowledge_documents WHERE id=?", (document_id,)
            ).fetchone()
            version_row = conn.execute(
                "SELECT * FROM document_versions WHERE id=?", (version_id,)
            ).fetchone()
        return self._document(document_row), self._version(version_row), True

    def replace_chunks(
        self,
        *,
        chat_id: str,
        actor_user_id: str,
        document_id: str,
        document_version_id: str,
        chunks: Sequence[tuple[str, str, bytes | None, int | None]],
    ) -> list[KnowledgeChunk]:
        space = self.require_member(chat_id, actor_user_id, admin=True)
        with self._transaction() as conn:
            version = conn.execute(
                """SELECT v.id FROM document_versions v JOIN knowledge_documents d ON d.id=v.document_id
                   WHERE v.id=? AND d.id=? AND d.project_space_id=?""",
                (document_version_id, document_id, space.id),
            ).fetchone()
            if version is None:
                raise PermissionError("document version does not belong to this project space")
            other_count = conn.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE project_space_id=? AND document_id<>?",
                (space.id, document_id),
            ).fetchone()[0]
            if other_count + len(chunks) > self.max_chunks_per_space:
                raise ValueError(f"project space exceeds the {self.max_chunks_per_space} chunk limit")
            conn.execute(
                "DELETE FROM knowledge_chunks WHERE document_version_id=?", (document_version_id,)
            )
            created: list[KnowledgeChunk] = []
            seen_hashes: set[str] = set()
            for ordinal, (text, content_sha256, embedding, token_count) in enumerate(chunks):
                if content_sha256 in seen_hashes:
                    continue
                seen_hashes.add(content_sha256)
                chunk_id = uuid4().hex
                conn.execute(
                    """INSERT INTO knowledge_chunks
                       (id,document_id,document_version_id,project_space_id,ordinal,text,content_sha256,embedding,token_count)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, document_id, document_version_id, space.id, ordinal, text,
                     content_sha256, embedding, token_count),
                )
                created.append(KnowledgeChunk(chunk_id, document_id, document_version_id,
                    space.id, ordinal, text, content_sha256, embedding, token_count))
            conn.execute(
                "UPDATE knowledge_documents SET status=?,updated_at=? WHERE id=?",
                (DocumentStatus.INDEXED.value, _now(), document_id),
            )
        return created

    def list_chunks(self, chat_id: str, user_id: str) -> list[KnowledgeChunk]:
        space = self.require_member(chat_id, user_id)
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.* FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id
                   WHERE c.project_space_id=? AND d.current_version_id=c.document_version_id
                   AND d.status=? ORDER BY d.updated_at DESC,c.ordinal""",
                (space.id, DocumentStatus.INDEXED.value),
            ).fetchall()
        return [self._chunk(row) for row in rows]

    def list_search_material(
        self, chat_id: str, user_id: str
    ) -> list[tuple[KnowledgeChunk, KnowledgeDocument]]:
        space = self.require_member(chat_id, user_id)
        return self.list_search_material_for_space(space.id, user_id)

    def list_search_material_for_space(
        self, project_space_id: str, user_id: str
    ) -> list[tuple[KnowledgeChunk, KnowledgeDocument]]:
        space = self.require_space_member(project_space_id, user_id)
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.*,d.source,d.source_ref,d.source_url,d.title,d.status,d.created_by,
                          d.created_at AS document_created_at,d.updated_at
                   FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id
                   WHERE c.project_space_id=? AND d.current_version_id=c.document_version_id
                   AND d.status=? AND c.embedding IS NOT NULL""",
                (space.id, DocumentStatus.INDEXED.value),
            ).fetchall()
        material = []
        for row in rows:
            document = KnowledgeDocument(
                row["document_id"], row["project_space_id"], DocumentSource(row["source"]),
                row["source_ref"], row["title"], DocumentStatus(row["status"]), row["created_by"],
                row["document_created_at"], row["updated_at"], row["source_url"],
            )
            material.append((self._chunk(row), document))
        return material

    def find_chunk_embeddings(
        self, chat_id: str, actor_user_id: str, content_hashes: Sequence[str]
    ) -> dict[str, bytes]:
        space = self.require_member(chat_id, actor_user_id, admin=True)
        hashes = list(dict.fromkeys(content_hashes))
        if not hashes:
            return {}
        placeholders = ",".join("?" for _ in hashes)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT content_sha256,embedding FROM knowledge_chunks
                    WHERE project_space_id=? AND embedding IS NOT NULL
                    AND content_sha256 IN ({placeholders})""",
                (space.id, *hashes),
            ).fetchall()
        return {row["content_sha256"]: row["embedding"] for row in rows}

    def create_index_job(self, chat_id: str, actor_user_id: str, document_id: str) -> IndexJob:
        space = self.require_member(chat_id, actor_user_id, admin=True)
        now, job_id = _now(), uuid4().hex
        with self._transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM knowledge_documents WHERE id=? AND project_space_id=?",
                (document_id, space.id),
            ).fetchone()
            if not exists:
                raise PermissionError("document does not belong to this project space")
            conn.execute(
                "INSERT INTO index_jobs(id,project_space_id,document_id,status,attempts,created_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                (job_id, space.id, document_id, IndexJobStatus.QUEUED.value, now, now),
            )
        return IndexJob(job_id, space.id, document_id, IndexJobStatus.QUEUED, 0, now, now)

    def transition_index_job(
        self, job_id: str, status: IndexJobStatus, *, error: str | None = None
    ) -> IndexJob:
        allowed = {
            IndexJobStatus.QUEUED: {IndexJobStatus.RUNNING},
            IndexJobStatus.RUNNING: {
                IndexJobStatus.SUCCEEDED, IndexJobStatus.FAILED, IndexJobStatus.PAUSED_BUDGET
            },
            IndexJobStatus.FAILED: {IndexJobStatus.QUEUED},
            IndexJobStatus.PAUSED_BUDGET: {IndexJobStatus.QUEUED},
            IndexJobStatus.SUCCEEDED: set(),
        }
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM index_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = IndexJobStatus(row["status"])
            if status not in allowed[current]:
                raise ValueError(f"invalid index job transition: {current.value} -> {status.value}")
            attempts = row["attempts"] + (1 if status is IndexJobStatus.RUNNING else 0)
            updated_at = _now()
            conn.execute(
                "UPDATE index_jobs SET status=?,attempts=?,error=?,updated_at=? WHERE id=?",
                (status.value, attempts, error, updated_at, job_id),
            )
            updated = conn.execute("SELECT * FROM index_jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(updated)

    def record_embedding_usage(
        self, project_space_id: str, model: str, input_tokens: int,
        estimated_cost_cny: float, request_id: str | None = None
    ) -> EmbeddingUsage:
        if input_tokens < 0 or estimated_cost_cny < 0:
            raise ValueError("embedding usage cannot be negative")
        usage = EmbeddingUsage(uuid4().hex, project_space_id, model, input_tokens,
            estimated_cost_cny, _now(), request_id)
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO embedding_usage(id,project_space_id,model,input_tokens,estimated_cost_cny,request_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (usage.id, usage.project_space_id, usage.model, usage.input_tokens,
                 usage.estimated_cost_cny, usage.request_id, usage.created_at),
            )
        return usage

    def daily_embedding_usage(self, project_space_id: str) -> tuple[int, float]:
        day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(estimated_cost_cny),0)
                   FROM embedding_usage WHERE project_space_id=? AND created_at>=?""",
                (project_space_id, day_start),
            ).fetchone()
        return int(row[0]), float(row[1])

    @staticmethod
    def _space(row: sqlite3.Row) -> ProjectSpace:
        keys = row.keys()
        origin_chat_id = row["origin_chat_id"] if "origin_chat_id" in keys else row["chat_id"]
        return ProjectSpace(row["id"], origin_chat_id, row["name"], row["created_by"],
            row["created_at"], row["allowed_directory"])

    @staticmethod
    def _document(row: sqlite3.Row) -> KnowledgeDocument:
        return KnowledgeDocument(row["id"], row["project_space_id"], DocumentSource(row["source"]),
            row["source_ref"], row["title"], DocumentStatus(row["status"]), row["created_by"],
            row["created_at"], row["updated_at"], row["source_url"])

    @staticmethod
    def _version(row: sqlite3.Row) -> DocumentVersion:
        return DocumentVersion(row["id"], row["document_id"], row["version"],
            row["content_sha256"], row["source_version"], row["created_at"])

    @staticmethod
    def _chunk(row: sqlite3.Row) -> KnowledgeChunk:
        return KnowledgeChunk(row["id"], row["document_id"], row["document_version_id"],
            row["project_space_id"], row["ordinal"], row["text"], row["content_sha256"],
            row["embedding"], row["token_count"])

    @staticmethod
    def _job(row: sqlite3.Row) -> IndexJob:
        return IndexJob(row["id"], row["project_space_id"], row["document_id"],
            IndexJobStatus(row["status"]), row["attempts"], row["created_at"],
            row["updated_at"], row["error"])
