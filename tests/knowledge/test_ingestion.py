"""Local discovery, format parsing, table semantics and deterministic chunking."""

from __future__ import annotations

import io
import re
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tele_bot.knowledge.ingestion import (
    DocumentParser,
    TextChunker,
    parse_feishu_document_url,
)
from tele_bot.knowledge.sources import KnowledgeSourceResolver
from tele_bot.knowledge.tables import TableLimits, TableReadOptions
from tele_bot.tools.workspace_policy import WorkspacePolicy
from tests.knowledge.support import (
    assert_chunk_locations,
    docx_bytes,
    tc,
    workbook_bytes,
)


class KnowledgeIngestionTests(unittest.TestCase):
    def test_text_normalization_hash_and_incremental_chunks_are_stable(self) -> None:
        parser = DocumentParser()
        first = parser.parse_bytes("a.md", b"# A\r\n\r\nhello\r\n")
        second = parser.parse_bytes("a.md", b"# A\n\nhello\n")
        self.assertEqual(first.content_sha256, second.content_sha256)
        chunks = TextChunker(max_chars=8, overlap_chars=2).split("first. second. third")
        self.assertGreater(len(chunks), 1)
        self.assertEqual([chunk.ordinal for chunk in chunks], list(range(len(chunks))))

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
                allowed_roots=(root,),
                workspace_root=workspace,
                quarantine_root=root / "quarantine",
            )
            with self.assertRaisesRegex(ValueError, "outside"):
                parser.parse_file(
                    str(outside), policy=policy, bound_directory=str(bound)
                )

    def test_feishu_document_url_is_strictly_validated(self) -> None:
        self.assertEqual(
            parse_feishu_document_url("https://acme.feishu.cn/docx/Abc_123"),
            ("docx", "Abc_123"),
        )
        with self.assertRaises(ValueError):
            parse_feishu_document_url("https://evil.test/docx/Abc_123")


class StructureTests(unittest.TestCase):
    def assert_offsets(self, parsed, chunks):
        return assert_chunk_locations(self, parsed, chunks)

    def test_markdown_hierarchy_natural_blocks_and_fenced_heading(self):
        text = "# Root\n\nIntro paragraph.\n\n## Child\n\n```python\n# code, not a heading\nprint('x')\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        parsed = DocumentParser().parse_bytes("a.md", text.encode())
        chunks = TextChunker(max_chars=100, overlap_chars=0).split_document(parsed)
        self.assertEqual(chunks[0].section_path, ("Root",))
        self.assertEqual(chunks[-1].section_path, ("Root", "Child"))
        self.assertIn("# code, not a heading", "\n".join(c.text for c in chunks))
        self.assert_offsets(parsed, chunks)

    def test_oversized_code_tables_plain_text_have_bounded_lossless_fallback(self):
        for text in (
            "字" * 203,
            "```\n" + "x" * 203 + "\n```",
            "|A|B|\n" * 100,
            "First sentence. Second sentence. Third sentence. " * 20,
        ):
            parsed = DocumentParser().parse_bytes("a.txt", text.encode())
            chunks = TextChunker(max_chars=30, overlap_chars=4).split_document(parsed)
            self.assertTrue(all(0 < len(c.text) <= 30 for c in chunks))
            self.assert_offsets(parsed, chunks)
            coverage = set()
            for chunk in chunks:
                coverage.update(range(chunk.start_char, chunk.end_char))
            self.assertTrue(
                all(
                    index in coverage
                    for index, char in enumerate(parsed.text)
                    if not char.isspace()
                )
            )

    def test_docx_heading_styles_and_table_row_boundaries(self):
        xml = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Root</w:t></w:r></w:p>'
            '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Child</w:t></w:r></w:p>'
            "<w:p><w:r><w:t>Facts</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", xml)
        parsed = DocumentParser().parse_bytes("a.docx", buffer.getvalue())
        self.assertIn("Facts", parsed.text)
        chunks = TextChunker().split_document(parsed)
        self.assertIn("字段：A；B", chunks[-1].text)
        self.assertIn("[无数据记录]", chunks[-1].text)
        self.assertEqual(chunks[-1].section_path, ("Root", "Child"))
        self.assertIn("docx:block:4", chunks[-1].source_locator)
        self.assert_offsets(parsed, chunks)

    def test_pdf_page_locator_and_unstructured_page_fallback(self):
        pages = [
            SimpleNamespace(extract_text=lambda: "第1章 概览\n\nFacts"),
            SimpleNamespace(extract_text=lambda: ""),
            SimpleNamespace(
                extract_text=lambda: "An unstructured page.\n\nNext paragraph."
            ),
        ]
        with patch.dict(
            "sys.modules",
            {
                "pypdf": SimpleNamespace(
                    PdfReader=lambda stream: SimpleNamespace(pages=pages)
                )
            },
        ):
            parsed = DocumentParser().parse_bytes("a.pdf", b"placeholder")
        chunks = TextChunker().split_document(parsed)
        self.assertIn("pdf:page:1", chunks[0].source_locator)
        self.assertEqual(chunks[0].section_path, ("第1章 概览",))
        self.assertIn("pdf:page:3", chunks[-1].source_locator)
        self.assertEqual(chunks[-1].section_path, ("第1章 概览",))
        self.assert_offsets(parsed, chunks)

    def test_setext_heading_and_chinese_sentence_fallback(self):
        parsed = DocumentParser().parse_bytes(
            "a.md",
            "Root\n====\n\nChild\n-----\n\n这是第一句话。这是第二句话。".encode(),
        )
        chunks = TextChunker(max_chars=20, overlap_chars=0).split_document(parsed)
        self.assertEqual(chunks[-1].section_path, ("Root", "Child"))
        self.assert_offsets(parsed, chunks)


class TableParserTests(unittest.TestCase):
    def assert_offsets(self, parsed, budget=1200):
        chunks = TextChunker(max_chars=budget, overlap_chars=0).split_document(parsed)
        return assert_chunk_locations(self, parsed, chunks, budget)

    def test_csv_quotes_newlines_zero_duplicate_and_blank_headers(self):
        parsed = DocumentParser().parse_bytes(
            "sample.csv", '名称,值,值,\r\n甲,0,,"备注一\n备注二"\r\n乙,1,2,尾'.encode()
        )
        table = parsed.tables[0]
        self.assertEqual(
            [c.name for c in table.columns], ["名称", "值", "值 [列3]", "列4 [列4]"]
        )
        self.assertEqual([r.position for r in table.rows], [2, 3])
        self.assertEqual(table.rows[0].cells[3].value, "备注一\n备注二")
        self.assertIn("值：0", parsed.text)
        self.assertIn("值 [列3]：[空值]", parsed.text)
        self.assertIn("record:2", parsed.sections[0].locator)
        self.assert_offsets(parsed)

    def test_csv_encodings_delimiters_headerless_and_ragged_rows(self):
        for encoding in ("utf-8-sig", "gb18030", "utf-16"):
            parsed = DocumentParser().parse_bytes(
                "a.csv",
                "甲;0\n乙;1;尾".encode(encoding),
                table_options=TableReadOptions(header_rows=0, delimiter=";"),
            )
            self.assertEqual(len(parsed.tables[0].rows), 2)
            self.assertEqual(len(parsed.tables[0].columns), 3)
            self.assertIn("列2 [列2]：0", parsed.text)
            self.assertEqual(parsed.tables[0].rows[0].cells[-1].value, None)
        with self.assertRaisesRegex(ValueError, "CSV 解析失败"):
            DocumentParser().parse_bytes("a.csv", b'A,B\n"unterminated')

    def test_equivalent_csv_and_xlsx_share_field_meaning(self):
        raw = workbook_bytes(
            lambda w: [
                w.active.append(row) for row in (("项目", "数量"), ("甲", 0), ("乙", 2))
            ]
        )
        csv = (
            DocumentParser()
            .parse_bytes("a.csv", "项目,数量\n甲,0\n乙,2".encode())
            .tables[0]
        )
        xlsx = DocumentParser().parse_bytes("a.xlsx", raw).tables[0]
        self.assertEqual(csv.columns, xlsx.columns)
        self.assertEqual(
            [[c.value for c in row.cells] for row in csv.rows],
            [[c.value for c in row.cells] for row in xlsx.rows],
        )

    def test_xlsx_multiple_sheets_regions_and_explicit_selection(self):
        def fill(w):
            ws = w.active
            ws.title = "项目"
            for row in (("名称", "数量"), ("甲", 0), (), ("参数", "值"), ("乙", 2)):
                ws.append(row)
            w.create_sheet("其他").append(["唯一列"])

        raw = workbook_bytes(fill)
        parsed = DocumentParser().parse_bytes("book.xlsx", raw)
        self.assertEqual(len(parsed.tables), 3)
        self.assertEqual([t.region for t in parsed.tables], ["A1:B2", "A4:B5", "A1:A1"])
        selected = DocumentParser().parse_bytes(
            "book.xlsx",
            raw,
            table_options=TableReadOptions(sheets=("项目",), cell_range="A4:B5"),
        )
        self.assertEqual(len(selected.tables), 1)
        self.assertIn("乙", selected.text)
        self.assertNotIn("甲", selected.text)
        self.assert_offsets(parsed)
        with self.assertRaisesRegex(ValueError, "工作表不存在"):
            DocumentParser().parse_bytes(
                "a.xlsx", raw, table_options=TableReadOptions(sheets=("不存在",))
            )

    def test_xlsx_multilevel_headers_real_merges_and_no_blank_fill(self):
        def fill(w):
            ws = w.active
            for row in (
                ("项目", "金额（万元）", None),
                (None, "预算", "实际"),
                ("甲", 0, None),
                (None, 2, 1),
            ):
                ws.append(row)
            ws.merge_cells("A1:A2")
            ws.merge_cells("B1:C1")
            ws.merge_cells("A3:A4")

        raw = workbook_bytes(fill)
        parsed = DocumentParser().parse_bytes(
            "a.xlsx", raw, table_options=TableReadOptions(header_rows=2)
        )
        table = parsed.tables[0]
        self.assertEqual(
            [c.name for c in table.columns],
            ["项目", "金额（万元） / 预算", "金额（万元） / 实际"],
        )
        self.assertEqual(table.rows[1].cells[0].value, "甲")
        self.assertEqual(table.rows[1].cells[0].merged_from, (3, 1))
        self.assertIsNone(table.rows[0].cells[2].value)
        self.assertIn("预算：0", parsed.text)
        with self.assertRaisesRegex(ValueError, "切断合并"):
            DocumentParser().parse_bytes(
                "a.xlsx", raw, table_options=TableReadOptions(cell_range="B1:B4")
            )

    def test_explicit_range_preserves_completely_blank_columns_and_rows(self):
        def fill(w):
            w.active.append(["名称", None, "数量"])
            w.active.append(["甲", None, 0])

        parsed = DocumentParser().parse_bytes(
            "a.xlsx",
            workbook_bytes(fill),
            table_options=TableReadOptions(cell_range="A1:C3"),
        )
        table = parsed.tables[0]
        self.assertEqual([c.position for c in table.columns], [1, 2, 3])
        self.assertEqual([r.position for r in table.rows], [2, 3])
        self.assertIn("列2 [列2]：[空值]", parsed.text)

    def test_xlsx_formulas_dates_formats_and_cached_zero(self):
        def fill(w):
            ws = w.active
            ws.append(["公式", "比例", "日期", "金额"])
            ws.append(["=1-1", 0.25, datetime(2026, 10, 20), 3])
            ws["B2"].number_format = "0%"
            ws["D2"].number_format = '"￥"0.00'

        raw = workbook_bytes(fill)
        parsed = DocumentParser().parse_bytes("a.xlsx", raw)
        self.assertIn("缺失缓存，未重算", parsed.text)
        self.assertIn("0.25 [源格式：0%]", parsed.text)
        self.assertIn("2026-10-20T00:00:00", parsed.text)
        self.assertIn('源格式："￥"0.00', parsed.text)
        # Insert a source-provided cached zero, never recalculate it ourselves.
        output = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(raw)) as source,
            zipfile.ZipFile(output, "w") as target,
        ):
            for item in source.infolist():
                value = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    value, replacements = re.subn(
                        rb"(<f>1-1</f>)<v(?:\s*/>|></v>)", rb"\1<v>0</v>", value
                    )
                    self.assertEqual(replacements, 1)
                target.writestr(item, value)
        cached = DocumentParser().parse_bytes("a.xlsx", output.getvalue())
        self.assertIn("缓存：0", cached.text)
        self.assertNotIn("缺失缓存", cached.text)

    def test_bounded_row_groups_repeat_fields_and_long_cells_are_lossless(self):
        budget = 100
        text = "名称,备注\n" + "\n".join(f"item{i},note{i}" for i in range(12))
        parsed = DocumentParser(table_chunk_chars=budget).parse_bytes(
            "a.csv", text.encode()
        )
        chunks = self.assert_offsets(parsed, budget)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertIn("表格：a.csv", chunk.text)
            self.assertIn("名称：", chunk.text)
            self.assertIn("备注：", chunk.text)
        value = "很长备注" * 100
        parsed = DocumentParser(table_chunk_chars=budget).parse_bytes(
            "a.csv", f"名称,备注\n甲,{value}".encode()
        )
        chunks = self.assert_offsets(parsed, budget)
        fragments = [c.text.split("备注：", 1)[1] for c in chunks if "备注：" in c.text]
        self.assertEqual("".join(fragments), value)
        self.assertTrue(
            all(
                "record:2" in c.source_locator and "表格：a.csv" in c.text
                for c in chunks
            )
        )

    def test_markdown_boundaries_escaped_pipes_fences_and_physical_lines(self):
        text = "# 标题\n\n\n\n正文\n\n| 名称 | 备注 |\n|---|---|\n| 甲 | a\\|b |\n\n## 后章\n\n```md\n| A | B |\n|---|---|\n| 1 | 2 |\n```\n尾段"
        parsed = DocumentParser().parse_bytes("a.md", text.encode())
        self.assertEqual(len(parsed.tables), 1)
        self.assertEqual(parsed.tables[0].rows[0].cells[1].value, "a|b")
        self.assertEqual(parsed.tables[0].rows[0].position, 9)
        chunks = self.assert_offsets(parsed)
        self.assertIn("| 1 | 2 |", "\n".join(c.text for c in chunks))
        self.assertEqual(chunks[-1].section_path, ("标题", "后章"))
        self.assertIn("尾段", chunks[-1].text)

    def test_markdown_indented_code_and_heading_after_table_are_not_rows(self):
        text = "# Root\n\n    | A | B |\n    |---|---|\n    | 1 | 2 |\n\n|A|B|\n|---|---|\n|x|y|\n## Next | chapter\n\nTail"
        parsed = DocumentParser().parse_bytes("a.md", text.encode())
        self.assertEqual(len(parsed.tables), 1)
        self.assertEqual(len(parsed.tables[0].rows), 1)
        chunks = self.assert_offsets(parsed)
        self.assertEqual(chunks[-1].section_path, ("Root", "Next | chapter"))
        self.assertIn("| 1 | 2 |", parsed.text)

    def test_docx_merged_headers_vertical_cells_and_paragraph_boundaries(self):
        xml = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>章</w:t></w:r></w:p>'
        xml += (
            "<w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr>"
            + tc("项目", '<w:vMerge w:val="restart"/>')
            + tc("金额", '<w:gridSpan w:val="2"/>')
            + "</w:tr>"
        )
        xml += (
            "<w:tr><w:trPr><w:tblHeader/></w:trPr>"
            + tc("", "<w:vMerge/>")
            + tc("预算")
            + tc("实际")
            + "</w:tr>"
        )
        xml += "<w:tr>" + tc("甲") + tc("0") + tc("") + "</w:tr></w:tbl>"
        xml += "<w:p><w:r><w:t>尾段</w:t></w:r></w:p>"
        parsed = DocumentParser().parse_bytes("a.docx", docx_bytes(xml))
        self.assertEqual(
            [c.name for c in parsed.tables[0].columns],
            ["项目", "金额 / 预算", "金额 / 实际"],
        )
        self.assertIn("预算：0", parsed.text)
        chunks = self.assert_offsets(parsed)
        self.assertEqual(chunks[-1].text, "尾段")
        self.assertEqual(chunks[-1].section_path, ("章",))

    def test_limits_reject_before_index_without_truncation(self):
        for limits in (
            replace(TableLimits(), max_rows=1),
            replace(TableLimits(), max_columns=1),
            replace(TableLimits(), max_cells=2),
            replace(TableLimits(), max_cell_chars=2),
            replace(TableLimits(), max_output_chars=5),
        ):
            with self.assertRaisesRegex(ValueError, "超限"):
                DocumentParser(table_limits=limits).parse_bytes(
                    "a.csv", b"Name,Value\nalpha,1"
                )
        raw = workbook_bytes(lambda w: w.active.append(["name"]))
        with self.assertRaisesRegex(ValueError, "解压后字节数"):
            DocumentParser(
                table_limits=replace(TableLimits(), max_unpacked_bytes=20)
            ).parse_bytes("a.xlsx", raw)
        for extension in ("xls", "xlsm", "png"):
            with self.assertRaises(ValueError):
                DocumentParser().parse_bytes("a." + extension, raw)

    def test_office_entity_and_huge_merge_are_rejected(self):
        xml = '<!DOCTYPE d [<!ENTITY x "text">]><w:p>&x;</w:p>'
        with self.assertRaisesRegex(ValueError, "entities/DOCTYPE"):
            DocumentParser().parse_bytes("a.docx", docx_bytes(xml))
        raw = workbook_bytes(lambda w: w.active.append(["header"]))
        output = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(raw)) as source,
            zipfile.ZipFile(output, "w") as target,
        ):
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data = data.replace(
                        b"</worksheet>",
                        b'<mergeCells><mergeCell ref="A1:XFD1048576"/></mergeCells></worksheet>',
                    )
                target.writestr(item, data)
        with self.assertRaisesRegex(ValueError, "超限"):
            DocumentParser().parse_bytes("a.xlsx", output.getvalue())


class KnowledgeSourceResolverTests(unittest.TestCase):
    def test_resolves_ordered_fuzzy_directory_and_collects_supported_files(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "files_data"
            target = workspace / "Job" / "八股"
            target.mkdir(parents=True)
            (target / "a.md").write_text("a", encoding="utf-8")
            (target / "b.txt").write_text("b", encoding="utf-8")
            (target / "image.png").write_bytes(b"png")
            policy = WorkspacePolicy(
                allowed_roots=(root,),
                workspace_root=workspace,
                quarantine_root=root / "quarantine",
            )

            selection = KnowledgeSourceResolver(policy).resolve(
                "将files_data目录下的job目录下的八股目录里的文档全部导入",
                bound_directory=str(workspace),
            )

            self.assertEqual(selection.directory, target.resolve())
            self.assertEqual([path.name for path in selection.files], ["a.md", "b.txt"])

    def test_direct_path_cannot_escape_bound_directory(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            bound = workspace / "bound"
            outside = workspace / "outside"
            bound.mkdir(parents=True)
            outside.mkdir()
            (outside / "secret.md").write_text("secret", encoding="utf-8")
            policy = WorkspacePolicy(
                allowed_roots=(root,),
                workspace_root=workspace,
                quarantine_root=root / "quarantine",
            )
            with self.assertRaisesRegex(ValueError, "绑定目录之外"):
                KnowledgeSourceResolver(policy).resolve(
                    str(outside), bound_directory=str(bound)
                )
