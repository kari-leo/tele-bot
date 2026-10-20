"""Safe parsing, normalization and deterministic chunking for KB sources."""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from tele_bot.tools.workspace_policy import WorkspacePolicy
from tele_bot.knowledge.tables import (
    StructuredTable, TableCell, TableLimits, TableMerge, TableReadOptions, TableRow,
    build_table, render_table,
)
from tele_bot.knowledge.table_readers import checked_archive, read_csv, read_xlsx, safe_xml


SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx", ".xlsx", ".csv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
REJECTED_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".dll", ".com", ".bat", ".cmd", ".ps1"
}


@dataclass(frozen=True)
class DocumentSection:
    start: int
    end: int
    path: tuple[str, ...] = ()
    locator: str = ""
    kind: str = "text"


@dataclass(frozen=True)
class ParsedDocument:
    name: str
    text: str
    content_sha256: str
    mime_type: str | None
    sections: tuple[DocumentSection, ...] = ()
    tables: tuple[StructuredTable, ...] = ()
    warnings: tuple[str, ...] = ()
    parser_profile: str = ""
    input_sha256: str = ""
    missing_content: tuple[str, ...] = ()


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    text: str
    content_sha256: str
    section_path: tuple[str, ...] = ()
    source_locator: str = ""
    start_char: int | None = None
    end_char: int | None = None


class DocumentParser:
    def __init__(self, *, max_bytes: int = 30 * 1024 * 1024,
                 table_limits: TableLimits | None = None, table_chunk_chars: int = 1200) -> None:
        self.max_bytes = max_bytes
        self.table_limits = table_limits or TableLimits.from_env()
        self.table_chunk_chars = table_chunk_chars
        if max_bytes <= 0 or table_chunk_chars <= 0:
            raise ValueError("parser size limits must be positive")

    def parse_file(
        self,
        raw_path: str,
        *,
        policy: WorkspacePolicy,
        bound_directory: str,
        table_options: TableReadOptions | None = None,
    ) -> ParsedDocument:
        path = policy.resolve_existing(raw_path)
        bound = policy.resolve_existing(bound_directory)
        if not path.is_file() or not bound.is_dir():
            raise ValueError("import source must be a file inside a bound directory")
        try:
            path.relative_to(bound)
        except ValueError as exc:
            raise ValueError("import source is outside the explicitly bound directory") from exc
        if path.stat().st_size > self.max_bytes:
            raise ValueError(f"document exceeds {self.max_bytes} bytes")
        with path.open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        return self.parse_bytes(path.name, data, mimetypes.guess_type(path.name)[0], table_options=table_options)

    def parse_bytes(self, name: str, data: bytes, mime_type: str | None = None,
                    *, table_options: TableReadOptions | None = None) -> ParsedDocument:
        if len(data) > self.max_bytes:
            raise ValueError(f"document exceeds {self.max_bytes} bytes")
        suffix = Path(name).suffix.lower()
        if suffix in IMAGE_EXTENSIONS or (mime_type or "").startswith("image/"):
            raise ValueError("image metadata may be archived, but OCR is not supported")
        if suffix in REJECTED_EXTENSIONS or suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"unsupported or unsafe document type: {suffix or 'unknown'}")
        sections, tables, warnings = (), (), ()
        options = table_options or TableReadOptions()
        if suffix not in {".xlsx", ".csv"} and (options.sheets or options.cell_range or options.delimiter or options.encoding):
            raise ValueError("sheets/cell_range 仅支持 XLSX，delimiter/encoding 仅支持 CSV")
        if suffix == ".xlsx" and (options.delimiter or options.encoding):
            raise ValueError("delimiter/encoding 仅支持 CSV")
        if suffix in {".md", ".markdown", ".txt", ".pdf"} and options.header_rows != 1:
            raise ValueError("header_rows 仅支持 XLSX/CSV/DOCX；Markdown 表头由分隔行确定")
        if suffix == ".csv" or suffix == ".xlsx":
            tables = (read_csv if suffix == ".csv" else read_xlsx)(name, data, options, self.table_limits)
            text, sections = self._assemble_tables(tables)
        elif suffix in {".md", ".markdown"}:
            text, sections, tables = self._extract_markdown(name, self._decode_text(data))
        elif suffix == ".txt":
            text = self._decode_text(data)
        elif suffix == ".docx":
            text, sections, tables = self._extract_docx_sections(data, name, options)
        else:
            text, sections = self._extract_pdf_sections(data)
            warnings = ("PDF 仅提取文本和页码；未恢复复杂表格结构，不支持扫描件 OCR。",)
        normalized = normalize_text(text)
        if not normalized:
            raise ValueError("document contains no extractable text")
        self.table_limits.check(len(normalized), self.table_limits.max_output_chars, "总输出字符数")
        if tables:
            warnings += (f"表头按明确规则读取（header_rows={options.header_rows}；Markdown 使用分隔行），请核对预览字段；"
                         "检索片段不能证明整表统计或全量筛选。",)
        missing_content = []
        for table in tables:
            missing = [c for row in table.rows for c in row.cells if c.formula is not None and c.value is None]
            if missing:
                missing_content.append(
                    f"{table.title}：{len(missing)} 个公式缺失缓存，源行 {min(c.row for c in missing)}–{max(c.row for c in missing)}，"
                    f"源列 {min(c.column for c in missing)}–{max(c.column for c in missing)}（已保留公式，未重算）")
            missing_headers = [c.position for c in table.columns if any("缺失缓存，未重算" in p for p in c.header_path)]
            if missing_headers:
                missing_content.append(f"{table.title}：表头源行 {table.header_rows}，列 {missing_headers} 的公式缺失缓存")
        return ParsedDocument(
            name=name,
            text=normalized,
            content_sha256=sha256_text(normalized),
            mime_type=mime_type or mimetypes.guess_type(name)[0],
            sections=sections,
            tables=tables,
            warnings=warnings,
            parser_profile=json.dumps({"renderer": "table-v1", "options": asdict(options),
                                       "table_chunk_chars": self.table_chunk_chars}, sort_keys=True),
            input_sha256=hashlib.sha256(data).hexdigest(),
            missing_content=tuple(missing_content),
        )

    def _assemble_tables(self, tables):
        pieces = [(part.text, part.path, part.locator, "table") for table in tables
                  for part in render_table(table, max_chars=self.table_chunk_chars, limits=self.table_limits)]
        return self._assemble(pieces)

    def _assemble(self, pieces):
        texts, sections, offset = [], [], 0
        for text, path, locator, kind in pieces:
            text = normalize_text(text)
            if not text:
                continue
            if texts:
                offset += 2
            self.table_limits.check(offset + len(text), self.table_limits.max_output_chars, "总输出字符数")
            sections.append(DocumentSection(offset, offset + len(text), tuple(path), locator, kind))
            texts.append(text)
            offset += len(text)
        return "\n\n".join(texts), tuple(sections)

    def _extract_markdown(self, name, text):
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        pieces, tables, prose, headings = [], [], [], []
        fence, index = None, 0

        def flush():
            if prose:
                pieces.append(("\n".join(prose), tuple(headings), "markdown", "text"))
                prose.clear()

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            indented_code = line.startswith(("    ", "\t"))
            marker = None if indented_code else re.match(r"^(`{3,}|~{3,})", stripped)
            if marker:
                if fence is None:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = None
                prose.append(line)
                index += 1
                continue
            if not fence and not indented_code:
                heading = re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$", stripped)
                setext = index + 1 < len(lines) and re.fullmatch(r"(={3,}|-{3,})", lines[index + 1].strip())
                if heading or (stripped and setext):
                    flush()
                    level = len(heading[1]) if heading else (1 if lines[index+1].strip()[0] == "=" else 2)
                    headings = headings[:level-1] + [heading[2] if heading else stripped]
                if index + 1 < len(lines) and "|" in line:
                    headers = self._markdown_cells(line)
                    separators = self._markdown_cells(lines[index+1])
                    if headers and len(headers) == len(separators) and all(re.fullmatch(r":?-{3,}:?", c) for c in separators):
                        flush()
                        first_line = index + 1
                        raw_rows = [headers]
                        index += 2
                        while (index < len(lines) and lines[index].strip() and "|" in lines[index]
                               and not re.match(r"^\s*(?:#{1,6}\s|`{3,}|~{3,})", lines[index])):
                            raw_rows.append(self._markdown_cells(lines[index]))
                            index += 1
                        width = max(map(len, raw_rows))
                        self.table_limits.check(width, self.table_limits.max_columns, "Markdown 列数")
                        self.table_limits.check(len(raw_rows), self.table_limits.max_rows, "Markdown 行数")
                        self.table_limits.check(width * len(raw_rows), self.table_limits.max_cells, "Markdown 单元格数")
                        rows = []
                        for ordinal, values in enumerate(raw_rows):
                            source_line = first_line if ordinal == 0 else first_line + ordinal + 1
                            for value in values:
                                self.table_limits.check(len(value), self.table_limits.max_cell_chars, "单元格字符数")
                            rows.append(TableRow(source_line, tuple(TableCell(source_line, c+1, values[c] or None if c < len(values) else None)
                                                                    for c in range(width))))
                        table = build_table(source="markdown", identity=f"table:{len(tables)+1}",
                                            title=f"{name} / 表格{len(tables)+1}", rows=tuple(rows),
                                            region=f"lines:{first_line}-{index}", section_path=tuple(headings))
                        tables.append(table)
                        pieces.extend((p.text, p.path, p.locator, "table") for p in
                                      render_table(table, max_chars=self.table_chunk_chars, limits=self.table_limits))
                        continue
            prose.append(line)
            index += 1
        flush()
        if not tables:
            # Existing heading/code parsing remains unchanged for non-table Markdown.
            return normalize_text(text), (), ()
        text, sections = self._assemble(pieces)
        return text, sections, tuple(tables)

    @staticmethod
    def _markdown_cells(line):
        # Escaped pipes are literal cell content, including inside inline code.
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|") and not line.endswith("\\|"):
            line = line[:-1]
        cells, current, escaped = [], [], False
        for char in line:
            if escaped:
                current.extend((char,) if char == "|" else ("\\", char))
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "|":
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if escaped:
            current.append("\\")
        return cells + ["".join(current).strip()]

    @staticmethod
    def _decode_text(data: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("text document encoding is not supported")

    def _extract_docx_sections(self, data: bytes, name: str, options: TableReadOptions):
        try:
            with checked_archive(data, self.table_limits) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("DOCX is missing word/document.xml")
                root = safe_xml(archive.read("word/document.xml"))
        except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError("invalid DOCX document") from exc
        pieces, tables = [], []
        headings: list[str] = []
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        body = root.find(f"{namespace}body")
        for index, element in enumerate(body if body is not None else root, 1):
            if element.tag == f"{namespace}tbl":
                if element.find(f".//{namespace}tbl") is not None:
                    raise ValueError("DOCX 嵌套表格暂不支持，请拆分为普通表格后导入")
                table = self._docx_table(element, namespace, name, index, tuple(headings), options)
                tables.append(table)
                pieces.extend((p.text, p.path, p.locator, "table") for p in
                              render_table(table, max_chars=self.table_chunk_chars, limits=self.table_limits))
                continue
            else:
                text = normalize_text("".join(n.text or "" for n in element.iter(f"{namespace}t")))
                style = element.find(f"{namespace}pPr/{namespace}pStyle")
                outline = element.find(f"{namespace}pPr/{namespace}outlineLvl")
                style_name = style.get(f"{namespace}val", "") if style is not None else ""
                match = re.search(r"(?:heading|标题)\s*([1-6])", style_name, re.I)
                level = int(match[1]) if match else None
                if outline is not None:
                    raw_level = outline.get(f"{namespace}val", "")
                    if raw_level.isdigit() and int(raw_level) < 6:
                        level = int(raw_level) + 1
                if level and text:
                    headings = headings[:level - 1] + [text]
            if text:
                pieces.append((text, tuple(headings), f"docx:block:{index}", "text"))
        text, sections = self._assemble(pieces)
        return text, sections, tuple(tables)

    def _docx_table(self, element, ns, name, block, path, options):
        rows, merges, active = [], [], {}
        xml_rows = element.findall(f"{ns}tr")
        self.table_limits.check(len(xml_rows), self.table_limits.max_rows, "DOCX 表格行数")
        header_count = 0
        for r, row in enumerate(xml_rows, 1):
            header = row.find(f"{ns}trPr/{ns}tblHeader")
            if (r == header_count + 1 and header is not None
                    and header.get(f"{ns}val", "true").lower() not in {"false", "0", "off"}):
                header_count += 1
            before = row.find(f"{ns}trPr/{ns}gridBefore")
            c = 1 + (int(before.get(f"{ns}val", "0")) if before is not None else 0)
            cells, next_active = [], {}
            for cell in row.findall(f"{ns}tc"):
                span = cell.find(f"{ns}tcPr/{ns}gridSpan")
                width = int(span.get(f"{ns}val", "1")) if span is not None else 1
                self.table_limits.check(c + width - 1, self.table_limits.max_columns, "DOCX 列数")
                if width < 1:
                    raise ValueError("invalid DOCX gridSpan")
                value = "\n".join("".join(n.text or "" for n in p.iter(f"{ns}t")) for p in cell.findall(f"{ns}p"))
                self.table_limits.check(len(value), self.table_limits.max_cell_chars, "单元格字符数")
                cells.extend(TableCell(r, col, value or None if col == c else None) for col in range(c, c+width))
                vertical = cell.find(f"{ns}tcPr/{ns}vMerge")
                if vertical is not None and vertical.get(f"{ns}val", "continue") == "continue":
                    if (c, width) not in active:
                        raise ValueError("DOCX 合并结构缺少起始单元格")
                    merge_index = active[c, width]
                    old = merges[merge_index]
                    merges[merge_index] = TableMerge(old.first_row, c, r, c+width-1)
                    next_active[c, width] = merge_index
                elif width > 1 or vertical is not None:
                    merges.append(TableMerge(r, c, r, c+width-1))
                    if vertical is not None:
                        next_active[c, width] = len(merges)-1
                c += width
            active = next_active
            rows.append(TableRow(r, tuple(cells)))
        self.table_limits.check(sum(len(row.cells) for row in rows), self.table_limits.max_cells, "DOCX 单元格数")
        return build_table(source="docx", identity=f"docx:block:{block}", title=f"{name} / 表格{block}",
                           rows=tuple(rows), header_rows=header_count or options.header_rows,
                           merges=tuple(merges), region=f"block:{block}", section_path=path)

    @staticmethod
    def _extract_pdf_sections(data: bytes) -> tuple[str, tuple[DocumentSection, ...]]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF parsing requires the pypdf dependency") from exc
        try:
            pages, sections, offset = [], [], 0
            for number, page in enumerate(PdfReader(io.BytesIO(data)).pages, 1):
                text = normalize_text(page.extract_text() or "")
                if not text:
                    continue
                if pages:
                    offset += 2
                sections.append(DocumentSection(offset, offset + len(text), (), f"pdf:page:{number}"))
                pages.append(text)
                offset += len(text)
            return "\n\n".join(pages), tuple(sections)
        except Exception as exc:  # pypdf exposes several parser-specific exceptions
            raise ValueError("invalid or unreadable PDF document") from exc


class TextChunker:
    def __init__(self, *, max_chars: int = 1200, overlap_chars: int = 120) -> None:
        if max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("invalid chunk size or overlap")
        self.max_chars, self.overlap_chars = max_chars, overlap_chars

    def split_document(self, parsed: ParsedDocument) -> list[TextChunk]:
        return self.split(parsed.text, sections=parsed.sections)

    def split(self, text: str, *, sections: tuple[DocumentSection, ...] = ()) -> list[TextChunk]:
        """Pack paragraphs within sections; split oversized code/tables without losing text.

        Offsets are half-open character ranges in normalized extracted text.
        PDF headings use explicit Markdown/numbered/Chinese chapter lines only;
        unrecognizable visual headings fall back to page + paragraph boundaries.
        """
        normalized = normalize_text(text)
        chunks: list[TextChunk] = []
        ranges = sections or (DocumentSection(0, len(normalized)),)
        pdf_path = ()
        for section in ranges:
            if section.kind == "table":
                body = normalized[section.start:section.end]
                if len(body) > self.max_chars:
                    raise ValueError("表格片段超过分块预算，请以相同或更小的 table_chunk_chars 重新解析")
                chunks.append(TextChunk(len(chunks), body, sha256_text(body), section.path,
                                        f"{section.locator}:chars:{section.start}-{section.end}",
                                        section.start, section.end))
                continue
            if section.locator.startswith("pdf:page:") and not section.path:
                section = DocumentSection(section.start, section.end, pdf_path, section.locator)
            blocks = self._blocks(normalized, section)
            if section.locator.startswith("pdf:page:") and blocks:
                pdf_path = blocks[-1][2]
            packed = []
            for start, end, path in blocks:
                if packed and packed[-1][2] == path and end - packed[-1][0] <= self.max_chars:
                    packed[-1] = (packed[-1][0], end, path)
                else:
                    packed.append((start, end, path))
            for start, stop, path in packed:
                while start < stop:
                    end = min(start + self.max_chars, stop)
                    if end < stop:
                        # Prefer complete lines/sentences; the hard split is bounded.
                        boundaries = [m.end() for m in re.finditer(r"\n|[。！？]|[.!?](?:\s|$)", normalized[start:end])]
                        if boundaries and boundaries[-1] > self.max_chars // 2:
                            end = start + boundaries[-1]
                    left = start
                    while left < end and normalized[left].isspace():
                        left += 1
                    right = end
                    while right > left and normalized[right - 1].isspace():
                        right -= 1
                    body = normalized[left:right]
                    if body:
                        locator = f"{section.locator or 'text'}:chars:{left}-{right}"
                        chunks.append(TextChunk(len(chunks), body, sha256_text(body), path, locator, left, right))
                    if end == stop:
                        break
                    start = max(start + 1, end - self.overlap_chars)
        return chunks

    @staticmethod
    def _blocks(text: str, section: DocumentSection) -> list[tuple[int, int, tuple[str, ...]]]:
        blocks = []
        path = list(section.path)
        start = None
        offset = section.start
        fence = None
        lines = text[section.start:section.end].splitlines(keepends=True)
        for index, line in enumerate(lines):
            stripped = line.strip()
            indented_code = line.startswith(("    ", "\t"))
            marker = None if indented_code else re.match(r"^(`{3,}|~{3,})", stripped)
            heading = None if fence or indented_code else re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$", stripped)
            chapter = None if fence or indented_code else re.match(r"^(?:第[一二三四五六七八九十百0-9]+[章节篇]|\d+(?:\.\d+)*[.、 ]\s*\S).{0,70}$", stripped)
            setext = (re.fullmatch(r"(={3,}|-{3,})\s*", lines[index + 1].strip())
                      if not fence and not indented_code and stripped and index + 1 < len(lines) else None)
            if heading or chapter or setext:
                if start is not None:
                    blocks.append((start, offset, tuple(path)))
                level = len(heading[1]) if heading else (2 if setext and setext[1][0] == "-" else 1)
                title = heading[2] if heading else stripped
                path = path[:level - 1] + [title]
                start = offset
            elif not stripped and not fence:
                if start is not None:
                    blocks.append((start, offset, tuple(path)))
                    start = None
            elif start is None:
                start = offset
            if marker:
                if fence is None:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = None
            offset += len(line)
        if start is not None:
            blocks.append((start, section.end, tuple(path)))
        return blocks


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_feishu_document_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "feishu.cn" or host.endswith(".feishu.cn")
        or host == "larksuite.com" or host.endswith(".larksuite.com")
    ):
        raise ValueError("only HTTPS Feishu/Lark document URLs are supported")
    match = re.fullmatch(r"/(docx|docs|wiki|sheets|base)/([A-Za-z0-9_-]+)", parsed.path.rstrip("/"))
    if not match:
        raise ValueError("Feishu document URL has an unsupported shape")
    return match.group(1), match.group(2)
