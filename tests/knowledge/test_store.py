"""Knowledge persistence, schema migrations, membership and configuration contracts."""

from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tele_bot.config.feishu import FeishuSettings
from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.config.runtime import RuntimeSettings
from tele_bot.knowledge.models import DocumentSource, IndexJobStatus, MemberRole
from tele_bot.persistence.kb_store import KnowledgeStore


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = KnowledgeStore(":memory:", max_chunks_per_space=2)
        self.space = self.store.create_space("chat-a", "Alpha", "admin")

    def tearDown(self) -> None:
        self.store.close()

    def test_creator_is_admin_and_cross_chat_or_user_is_rejected(self) -> None:
        self.assertEqual(self.store.require_member("chat-a", "admin").id, self.space.id)
        with self.assertRaises(PermissionError):
            self.store.require_member("chat-a", "stranger")
        with self.assertRaises(PermissionError):
            self.store.require_member("chat-b", "admin")

        self.store.add_member("chat-a", "admin", "member", MemberRole.MEMBER)
        with self.assertRaises(PermissionError):
            self.store.bind_directory("chat-a", "member", "D:/docs")

    def test_multiple_spaces_can_be_created_from_one_chat(self) -> None:
        second = self.store.create_space("chat-a", "Beta", "admin")
        self.assertNotEqual(second.id, self.space.id)
        self.assertEqual(self.store.require_member("chat-a", "admin").id, second.id)
        accessible = self.store.list_accessible_spaces("admin")
        self.assertEqual({space.name for space in accessible}, {"Alpha", "Beta"})

    def test_document_hash_dedup_and_current_chunks(self) -> None:
        doc, version, changed = self.store.upsert_document_version(
            chat_id="chat-a",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="reports/a.md",
            title="A",
            content_sha256="doc-hash",
        )
        self.assertTrue(changed)
        same_doc, same_version, changed = self.store.upsert_document_version(
            chat_id="chat-a",
            actor_user_id="admin",
            source=DocumentSource.REPORT,
            source_ref="reports/a.md",
            title="A",
            content_sha256="doc-hash",
        )
        self.assertFalse(changed)
        self.assertEqual(same_doc.id, doc.id)
        self.assertEqual(same_version.id, version.id)

        chunks = self.store.replace_chunks(
            chat_id="chat-a",
            actor_user_id="admin",
            document_id=doc.id,
            document_version_id=version.id,
            chunks=[("one", "h1", b"v1", 1), ("one repeated", "h1", b"v1", 1)],
        )
        self.assertEqual(len(chunks), 1)
        self.store.add_member("chat-a", "admin", "member")
        self.assertEqual(self.store.list_chunks("chat-a", "member")[0].text, "one")

    def test_chunk_limit_and_job_state_machine(self) -> None:
        doc, version, _ = self.store.upsert_document_version(
            chat_id="chat-a",
            actor_user_id="admin",
            source=DocumentSource.FEISHU_UPLOAD,
            source_ref="file-key",
            title="file",
            content_sha256="doc-hash",
        )
        with self.assertRaisesRegex(ValueError, "chunk limit"):
            self.store.replace_chunks(
                chat_id="chat-a",
                actor_user_id="admin",
                document_id=doc.id,
                document_version_id=version.id,
                chunks=[("1", "1", None, 1), ("2", "2", None, 1), ("3", "3", None, 1)],
            )
        job = self.store.create_index_job("chat-a", "admin", doc.id)
        running = self.store.transition_index_job(job.id, IndexJobStatus.RUNNING)
        self.assertEqual(running.attempts, 1)
        failed = self.store.transition_index_job(
            job.id, IndexJobStatus.FAILED, error="boom"
        )
        self.assertEqual(failed.error, "boom")
        self.assertEqual(
            self.store.transition_index_job(job.id, IndexJobStatus.QUEUED).status,
            IndexJobStatus.QUEUED,
        )

    def test_v3_migration_preserves_existing_content_and_vectors(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.sqlite"
            store = KnowledgeStore(path)
            store.create_space("chat", "Team", "admin")
            doc, version, _ = store.upsert_document_version(
                chat_id="chat",
                actor_user_id="admin",
                source=DocumentSource.REPORT,
                source_ref="a",
                title="A",
                content_sha256="hash",
            )
            store.replace_chunks(
                chat_id="chat",
                actor_user_id="admin",
                document_id=doc.id,
                document_version_id=version.id,
                chunks=[("old", "old-hash", b"1234", 1)],
            )
            store.close()
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE old_chunks AS SELECT id,document_id,document_version_id,project_space_id,
                    ordinal,text,content_sha256,embedding,token_count FROM knowledge_chunks;
                DROP TABLE knowledge_chunks;
                ALTER TABLE old_chunks RENAME TO knowledge_chunks;
                PRAGMA user_version=3;
            """)
            conn.close()
            store = KnowledgeStore(path)
            try:
                chunk = store.list_chunks("chat", "admin")[0]
                self.assertEqual(
                    (chunk.text, chunk.embedding, chunk.section_path),
                    ("old", b"1234", ()),
                )
                self.assertEqual(
                    store._conn.execute("PRAGMA user_version").fetchone()[0], 5
                )
            finally:
                store.close()


class KnowledgeConfigurationTests(unittest.TestCase):
    def test_embedding_defaults_are_safe_and_independent(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("tele_bot.config.llm.settings._load_local_env", return_value={}),
        ):
            settings = AliBailianSettings.from_env()

        self.assertEqual(settings.embedding_model, "text-embedding-v4")
        self.assertEqual(settings.embedding_dimensions, 1024)
        self.assertIsNone(settings.embedding_api_key)
        with self.assertRaisesRegex(RuntimeError, "EMBEDDING_API_KEY"):
            settings.require_embedding_credentials()

    def test_invalid_embedding_dimension_fails_during_load(self) -> None:
        with (
            patch.dict(
                os.environ, {"ALIBAILIAN_EMBEDDING_DIMENSIONS": "0"}, clear=True
            ),
            patch("tele_bot.config.llm.settings._load_local_env", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "DIMENSIONS"):
                AliBailianSettings.from_env()

    def test_runtime_knowledge_defaults_and_validation(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = RuntimeSettings.from_env()
        self.assertEqual(settings.kb_sqlite_path, "data/knowledge.sqlite")
        self.assertEqual(settings.kb_max_chunks_per_space, 10_000)

        with patch.dict(os.environ, {"KB_MAX_CHUNKS_PER_SPACE": "-1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "KB_MAX_CHUNKS_PER_SPACE"):
                RuntimeSettings.from_env()

    def test_feishu_settings_loads_kb_creators_from_feishu_config(self) -> None:
        file_values = {
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": "secret",
            "KB_CREATOR_USER_IDS": " user_1,ou_2,, user_1 ",
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(FeishuSettings, "_load_env_file", return_value=file_values),
        ):
            settings = FeishuSettings.from_env()

        self.assertEqual(settings.kb_creator_user_ids, ("user_1", "ou_2", "user_1"))
