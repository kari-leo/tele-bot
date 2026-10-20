import io
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from tele_bot.knowledge.ingestion import DocumentParser, TextChunker
from tele_bot.knowledge.models import DocumentSource
from tele_bot.persistence.kb_store import KnowledgeStore


class StructureTests(unittest.TestCase):
    def assert_offsets(self, parsed, chunks):
        for chunk in chunks:
            self.assertEqual(parsed.text[chunk.start_char:chunk.end_char], chunk.text)
            self.assertTrue(chunk.source_locator)

    def test_markdown_hierarchy_natural_blocks_and_fenced_heading(self):
        text = "# Root\n\nIntro paragraph.\n\n## Child\n\n```python\n# code, not a heading\nprint('x')\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        parsed = DocumentParser().parse_bytes("a.md", text.encode())
        chunks = TextChunker(max_chars=100, overlap_chars=0).split_document(parsed)
        self.assertEqual(chunks[0].section_path, ("Root",))
        self.assertEqual(chunks[-1].section_path, ("Root", "Child"))
        self.assertIn("# code, not a heading", "\n".join(c.text for c in chunks))
        self.assert_offsets(parsed, chunks)

    def test_oversized_code_tables_plain_text_have_bounded_lossless_fallback(self):
        for text in ("字" * 203, "```\n" + "x" * 203 + "\n```", "|A|B|\n" * 100,
                     "First sentence. Second sentence. Third sentence. " * 20):
            parsed = DocumentParser().parse_bytes("a.txt", text.encode())
            chunks = TextChunker(max_chars=30, overlap_chars=4).split_document(parsed)
            self.assertTrue(all(0 < len(c.text) <= 30 for c in chunks))
            self.assert_offsets(parsed, chunks)
            coverage = set()
            for chunk in chunks:
                coverage.update(range(chunk.start_char, chunk.end_char))
            self.assertTrue(all(index in coverage for index, char in enumerate(parsed.text) if not char.isspace()))

    def test_docx_heading_styles_and_table_row_boundaries(self):
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
               '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Root</w:t></w:r></w:p>'
               '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Child</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>Facts</w:t></w:r></w:p>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>'
               '<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
               '</w:body></w:document>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", xml)
        parsed = DocumentParser().parse_bytes("a.docx", buffer.getvalue())
        chunks = TextChunker().split_document(parsed)
        self.assertEqual(chunks[-1].text, "A\tB")
        self.assertEqual(chunks[-1].section_path, ("Root", "Child"))
        self.assertIn("docx:block:4", chunks[-1].source_locator)
        self.assert_offsets(parsed, chunks)

    def test_pdf_page_locator_and_unstructured_page_fallback(self):
        pages = [SimpleNamespace(extract_text=lambda: "第1章 概览\n\nFacts"),
                 SimpleNamespace(extract_text=lambda: ""),
                 SimpleNamespace(extract_text=lambda: "An unstructured page.\n\nNext paragraph.")]
        with patch.dict("sys.modules", {"pypdf": SimpleNamespace(PdfReader=lambda stream: SimpleNamespace(pages=pages))}):
            parsed = DocumentParser().parse_bytes("a.pdf", b"placeholder")
        chunks = TextChunker().split_document(parsed)
        self.assertIn("pdf:page:1", chunks[0].source_locator)
        self.assertEqual(chunks[0].section_path, ("第1章 概览",))
        self.assertIn("pdf:page:3", chunks[-1].source_locator)
        self.assertEqual(chunks[-1].section_path, ("第1章 概览",))
        self.assert_offsets(parsed, chunks)

    def test_setext_heading_and_chinese_sentence_fallback(self):
        parsed = DocumentParser().parse_bytes("a.md", "Root\n====\n\nChild\n-----\n\n这是第一句话。这是第二句话。".encode())
        chunks = TextChunker(max_chars=20, overlap_chars=0).split_document(parsed)
        self.assertEqual(chunks[-1].section_path, ("Root", "Child"))
        self.assert_offsets(parsed, chunks)

    def test_v3_migration_preserves_existing_content_and_vectors(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.sqlite"
            store = KnowledgeStore(path)
            store.create_space("chat", "Team", "admin")
            doc, version, _ = store.upsert_document_version(chat_id="chat", actor_user_id="admin",
                source=DocumentSource.REPORT, source_ref="a", title="A", content_sha256="hash")
            store.replace_chunks(chat_id="chat", actor_user_id="admin", document_id=doc.id,
                                 document_version_id=version.id, chunks=[("old", "old-hash", b"1234", 1)])
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
                self.assertEqual((chunk.text, chunk.embedding, chunk.section_path), ("old", b"1234", ()))
                self.assertEqual(store._conn.execute("PRAGMA user_version").fetchone()[0], 4)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
