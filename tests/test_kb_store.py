import unittest

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
            chat_id="chat-a", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="reports/a.md", title="A", content_sha256="doc-hash"
        )
        self.assertTrue(changed)
        same_doc, same_version, changed = self.store.upsert_document_version(
            chat_id="chat-a", actor_user_id="admin", source=DocumentSource.REPORT,
            source_ref="reports/a.md", title="A", content_sha256="doc-hash"
        )
        self.assertFalse(changed)
        self.assertEqual(same_doc.id, doc.id)
        self.assertEqual(same_version.id, version.id)

        chunks = self.store.replace_chunks(
            chat_id="chat-a", actor_user_id="admin", document_id=doc.id,
            document_version_id=version.id,
            chunks=[("one", "h1", b"v1", 1), ("one repeated", "h1", b"v1", 1)]
        )
        self.assertEqual(len(chunks), 1)
        self.store.add_member("chat-a", "admin", "member")
        self.assertEqual(self.store.list_chunks("chat-a", "member")[0].text, "one")

    def test_chunk_limit_and_job_state_machine(self) -> None:
        doc, version, _ = self.store.upsert_document_version(
            chat_id="chat-a", actor_user_id="admin", source=DocumentSource.FEISHU_UPLOAD,
            source_ref="file-key", title="file", content_sha256="doc-hash"
        )
        with self.assertRaisesRegex(ValueError, "chunk limit"):
            self.store.replace_chunks(
                chat_id="chat-a", actor_user_id="admin", document_id=doc.id,
                document_version_id=version.id,
                chunks=[("1", "1", None, 1), ("2", "2", None, 1), ("3", "3", None, 1)]
            )
        job = self.store.create_index_job("chat-a", "admin", doc.id)
        running = self.store.transition_index_job(job.id, IndexJobStatus.RUNNING)
        self.assertEqual(running.attempts, 1)
        failed = self.store.transition_index_job(job.id, IndexJobStatus.FAILED, error="boom")
        self.assertEqual(failed.error, "boom")
        self.assertEqual(
            self.store.transition_index_job(job.id, IndexJobStatus.QUEUED).status,
            IndexJobStatus.QUEUED,
        )


if __name__ == "__main__":
    unittest.main()
