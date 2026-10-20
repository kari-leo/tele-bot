"""Safe parsing, normalization and deterministic chunking for KB sources."""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from tele_bot.tools.workspace_policy import WorkspacePolicy


SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}
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


@dataclass(frozen=True)
class ParsedDocument:
    name: str
    text: str
    content_sha256: str
    mime_type: str | None
    sections: tuple[DocumentSection, ...] = ()


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
    def __init__(self, *, max_bytes: int = 30 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes

    def parse_file(
        self,
        raw_path: str,
        *,
        policy: WorkspacePolicy,
        bound_directory: str,
    ) -> ParsedDocument:
        path = policy.resolve_existing(raw_path)
        bound = policy.resolve_existing(bound_directory)
        if not path.is_file() or not bound.is_dir():
            raise ValueError("import source must be a file inside a bound directory")
        try:
            path.relative_to(bound)
        except ValueError as exc:
            raise ValueError("import source is outside the explicitly bound directory") from exc
        return self.parse_bytes(path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0])

    def parse_bytes(self, name: str, data: bytes, mime_type: str | None = None) -> ParsedDocument:
        if len(data) > self.max_bytes:
            raise ValueError(f"document exceeds {self.max_bytes} bytes")
        suffix = Path(name).suffix.lower()
        if suffix in IMAGE_EXTENSIONS or (mime_type or "").startswith("image/"):
            raise ValueError("image metadata may be archived, but OCR is not supported")
        if suffix in REJECTED_EXTENSIONS or suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"unsupported or unsafe document type: {suffix or 'unknown'}")
        sections = ()
        if suffix in {".md", ".markdown", ".txt"}:
            text = self._decode_text(data)
        elif suffix == ".docx":
            text, sections = self._extract_docx_sections(data)
        else:
            text, sections = self._extract_pdf_sections(data)
        normalized = normalize_text(text)
        if not normalized:
            raise ValueError("document contains no extractable text")
        return ParsedDocument(
            name=name,
            text=normalized,
            content_sha256=sha256_text(normalized),
            mime_type=mime_type or mimetypes.guess_type(name)[0],
            sections=sections,
        )

    @staticmethod
    def _decode_text(data: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("text document encoding is not supported")

    @staticmethod
    def _extract_docx_sections(data: bytes) -> tuple[str, tuple[DocumentSection, ...]]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("DOCX is missing word/document.xml")
                root = ElementTree.fromstring(archive.read("word/document.xml"))
        except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError("invalid DOCX document") from exc
        paragraphs: list[str] = []
        sections = []
        headings: list[str] = []
        offset = 0
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        body = root.find(f"{namespace}body")
        for index, element in enumerate(body if body is not None else root, 1):
            if element.tag == f"{namespace}tbl":
                rows = ["\t".join("".join(n.text or "" for n in cell.iter(f"{namespace}t"))
                                  for cell in row.findall(f"{namespace}tc"))
                        for row in element.findall(f"{namespace}tr")]
                text = normalize_text("\n".join(rows))
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
                if paragraphs:
                    offset += 2
                sections.append(DocumentSection(offset, offset + len(text), tuple(headings), f"docx:block:{index}"))
                paragraphs.append(text)
                offset += len(text)
        return "\n\n".join(paragraphs), tuple(sections)

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
            marker = re.match(r"^(`{3,}|~{3,})", stripped)
            heading = None if fence else re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$", stripped)
            chapter = None if fence else re.match(r"^(?:第[一二三四五六七八九十百0-9]+[章节篇]|\d+(?:\.\d+)*[.、 ]\s*\S).{0,70}$", stripped)
            setext = (re.fullmatch(r"(={3,}|-{3,})\s*", lines[index + 1].strip())
                      if not fence and stripped and index + 1 < len(lines) else None)
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
    match = re.fullmatch(r"/(docx|docs|wiki)/([A-Za-z0-9_-]+)", parsed.path.rstrip("/"))
    if not match:
        raise ValueError("Feishu document URL has an unsupported shape")
    return match.group(1), match.group(2)
