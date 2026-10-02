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
class ParsedDocument:
    name: str
    text: str
    content_sha256: str
    mime_type: str | None


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    text: str
    content_sha256: str


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
        if suffix in {".md", ".markdown", ".txt"}:
            text = self._decode_text(data)
        elif suffix == ".docx":
            text = self._extract_docx(data)
        else:
            text = self._extract_pdf(data)
        normalized = normalize_text(text)
        if not normalized:
            raise ValueError("document contains no extractable text")
        return ParsedDocument(
            name=name,
            text=normalized,
            content_sha256=sha256_text(normalized),
            mime_type=mime_type or mimetypes.guess_type(name)[0],
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
    def _extract_docx(data: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("DOCX is missing word/document.xml")
                root = ElementTree.fromstring(archive.read("word/document.xml"))
        except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError("invalid DOCX document") from exc
        paragraphs: list[str] = []
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        for paragraph in root.iter(f"{namespace}p"):
            text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
            if text.strip():
                paragraphs.append(text)
        return "\n\n".join(paragraphs)

    @staticmethod
    def _extract_pdf(data: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF parsing requires the pypdf dependency") from exc
        try:
            return "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        except Exception as exc:  # pypdf exposes several parser-specific exceptions
            raise ValueError("invalid or unreadable PDF document") from exc


class TextChunker:
    def __init__(self, *, max_chars: int = 1200, overlap_chars: int = 120) -> None:
        if max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars:
            raise ValueError("invalid chunk size or overlap")
        self.max_chars, self.overlap_chars = max_chars, overlap_chars

    def split(self, text: str) -> list[TextChunk]:
        normalized = normalize_text(text)
        chunks: list[TextChunk] = []
        start = 0
        while start < len(normalized):
            target_end = min(start + self.max_chars, len(normalized))
            end = target_end
            if target_end < len(normalized):
                boundary = max(
                    normalized.rfind("\n\n", start, target_end),
                    normalized.rfind("。", start, target_end),
                    normalized.rfind(". ", start, target_end),
                )
                if boundary > start + self.max_chars // 2:
                    end = boundary + (1 if normalized[boundary] == "。" else 0)
            chunk_text = normalized[start:end].strip()
            if chunk_text:
                chunks.append(TextChunk(len(chunks), chunk_text, sha256_text(chunk_text)))
            if end >= len(normalized):
                break
            next_start = max(start + 1, end - self.overlap_chars)
            start = next_start
        return chunks


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
