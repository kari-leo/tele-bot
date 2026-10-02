import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.knowledge.ingestion import (
    DocumentParser,
    TextChunker,
    parse_feishu_document_url,
)
from tele_bot.tools.workspace_policy import WorkspacePolicy


class KnowledgeIngestionTests(unittest.TestCase):
    def test_text_normalization_hash_and_incremental_chunks_are_stable(self) -> None:
        parser = DocumentParser()
        first = parser.parse_bytes("a.md", b"# A\r\n\r\nhello\r\n")
        second = parser.parse_bytes("a.md", b"# A\n\nhello\n")
        self.assertEqual(first.content_sha256, second.content_sha256)
        chunks = TextChunker(max_chars=8, overlap_chars=2).split("first. second. third")
        self.assertGreater(len(chunks), 1)
        self.assertEqual([chunk.ordinal for chunk in chunks], list(range(len(chunks))))

    def test_docx_is_extracted_without_ocr_or_macros(self) -> None:
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>',
            )
        parsed = DocumentParser().parse_bytes("a.docx", content.getvalue())
        self.assertEqual(parsed.text, "Hello")

    def test_rejects_images_archives_and_paths_outside_bound_directory(self) -> None:
        parser = DocumentParser()
        for name in ("image.png", "archive.zip", "program.exe"):
            with self.assertRaises(ValueError):
                parser.parse_bytes(name, b"not relevant")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            bound = workspace / "bound"
            bound.mkdir(parents=True)
            outside = workspace / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            policy = WorkspacePolicy(
                allowed_roots=(root,), workspace_root=workspace,
                quarantine_root=root / "quarantine",
            )
            with self.assertRaisesRegex(ValueError, "outside"):
                parser.parse_file(str(outside), policy=policy, bound_directory=str(bound))

    def test_feishu_document_url_is_strictly_validated(self) -> None:
        self.assertEqual(
            parse_feishu_document_url("https://acme.feishu.cn/docx/Abc_123"),
            ("docx", "Abc_123"),
        )
        with self.assertRaises(ValueError):
            parse_feishu_document_url("https://evil.test/docx/Abc_123")


if __name__ == "__main__":
    unittest.main()
