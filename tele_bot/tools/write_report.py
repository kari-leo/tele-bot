"""
WriteReportTool — saves a markdown report to the project's reports/ dir.

Decoupled from any LLM client: takes a finished text + a filename and writes
it. This is the W2 / W4 write-out path in the new ReAct executor.

Safety:
- reports/ is the only allowed write location (no traversal escape)
- filename slug-validated; cannot start with `.` or `/`
- max content size 200 KB
- never overwrites: if `<slug>.md` exists, append `-N` suffix
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_CONTENT_CHARS = 200_000
DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[2] / "reports"
_SLUG_RE = re.compile(r"[^a-z0-9\-_]+")
_VALID_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,80}$")


@dataclass
class WriteReportTool:
    reports_dir: Path = DEFAULT_REPORT_DIR

    def __post_init__(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def write(self, content: str, title: str | None = None,
              filename: str | None = None) -> str:
        if not content or not content.strip():
            raise ValueError("content is required")
        if len(content) > MAX_CONTENT_CHARS:
            raise ValueError(
                f"content exceeds {MAX_CONTENT_CHARS} chars (got {len(content)})"
            )

        slug = self._resolve_slug(filename, title)
        out_path = self._next_available_path(slug)
        out_path.write_text(content.strip() + "\n", encoding="utf-8")
        return str(out_path)

    def _resolve_slug(self, filename: str | None, title: str | None) -> str:
        if filename:
            if "/" in filename or filename.startswith("."):
                raise ValueError(f"invalid filename: {filename!r}")
            base = filename[:-3] if filename.lower().endswith(".md") else filename
            slug = base.lower()
            slug = _SLUG_RE.sub("-", slug).strip("-_")
            if not _VALID_SLUG_RE.match(slug):
                raise ValueError(f"invalid filename: {filename!r}")
            return slug

        if title:
            slug = _SLUG_RE.sub("-", title.lower()).strip("-_")
            if _VALID_SLUG_RE.match(slug):
                return slug
            # Title is e.g. all-Chinese; fall through to timestamp default

        return f"report-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    def _next_available_path(self, slug: str) -> Path:
        candidate = self.reports_dir / f"{slug}.md"
        if not candidate.exists():
            return candidate
        n = 2
        while True:
            candidate = self.reports_dir / f"{slug}-{n}.md"
            if not candidate.exists():
                return candidate
            n += 1
            if n > 999:
                raise RuntimeError(f"too many existing reports with slug {slug}")
