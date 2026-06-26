"""
BlogPublishTool — publishes a markdown article to windborne-blog's posts dir.

Safety rules (enforced, not advisory):
- posts_dir verified writable at init via real sentinel write/read/delete
- slug validated: [a-z0-9-] only, 1–80 chars, no leading '-'
- output path constrained to posts_dir (no traversal, no absolute paths)
- no overwrite: raises BlogPublishError if slug already exists
- frontmatter required: title, published (YYYY-MM-DD), description
- no git operations of any kind
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,79}$")
_FM_FIELD_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_REQUIRED_FM = {"title", "published", "description"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_CONTENT_CHARS = 400_000


class BlogPublishError(Exception):
    pass


@dataclass
class BlogPublishTool:
    posts_dir: Path

    def __post_init__(self) -> None:
        self.posts_dir = Path(self.posts_dir)
        self._verify_writable()

    def _verify_writable(self) -> None:
        if not self.posts_dir.exists():
            raise BlogPublishError(f"posts_dir does not exist: {self.posts_dir}")
        if not self.posts_dir.is_dir():
            raise BlogPublishError(f"posts_dir is not a directory: {self.posts_dir}")
        sentinel = self.posts_dir / f".sentinel-{uuid.uuid4().hex}.tmp"
        try:
            sentinel.write_text("ok", encoding="utf-8")
            if sentinel.read_text(encoding="utf-8") != "ok":
                raise BlogPublishError("sentinel read-back mismatch")
        except OSError as exc:
            raise BlogPublishError(
                f"posts_dir not writable: {self.posts_dir}: {exc}"
            ) from exc
        finally:
            sentinel.unlink(missing_ok=True)

    def publish(self, slug: str, content: str) -> str:
        """Write content as <slug>.md in posts_dir.

        Args:
            slug: URL-safe identifier, [a-z0-9-], max 80 chars.
            content: full markdown string including frontmatter.

        Returns:
            Absolute path of the created file.

        Raises:
            BlogPublishError: on validation failure or slug collision.
        """
        self._validate_slug(slug)
        self._validate_content(content)

        out_path = self._safe_path(slug)
        if out_path.exists():
            raise BlogPublishError(
                f"slug already exists: {out_path}; delete it first to republish"
            )

        out_path.write_text(content.strip() + "\n", encoding="utf-8")
        return str(out_path)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _validate_slug(self, slug: str) -> None:
        if not slug or not isinstance(slug, str):
            raise BlogPublishError("slug must be a non-empty string")
        if not _SLUG_RE.match(slug):
            raise BlogPublishError(
                f"invalid slug {slug!r}: only [a-z0-9-], 1–80 chars, no leading '-'"
            )

    def _validate_content(self, content: str) -> None:
        if not content or not content.strip():
            raise BlogPublishError("content is empty")
        if len(content) > MAX_CONTENT_CHARS:
            raise BlogPublishError(
                f"content exceeds {MAX_CONTENT_CHARS} chars (got {len(content)})"
            )
        m = _FM_FIELD_RE.match(content.strip())
        if not m:
            raise BlogPublishError(
                "content must begin with a YAML frontmatter block (--- ... ---)"
            )
        fm_block = m.group(1)
        fm_values: dict[str, str] = {}
        for line in fm_block.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fm_values[key.strip()] = val.strip()

        missing = _REQUIRED_FM - fm_values.keys()
        if missing:
            raise BlogPublishError(
                f"frontmatter missing required fields: {sorted(missing)}"
            )

        for field in ("title", "description"):
            if not fm_values.get(field):
                raise BlogPublishError(f"frontmatter field '{field}' must not be empty")

        published = fm_values.get("published", "")
        if not published:
            raise BlogPublishError("frontmatter field 'published' must not be empty")
        if not _DATE_RE.match(published):
            raise BlogPublishError(
                f"frontmatter 'published' must be YYYY-MM-DD, got {published!r}"
            )

    def _safe_path(self, slug: str) -> Path:
        resolved = (self.posts_dir / f"{slug}.md").resolve()
        if not str(resolved).startswith(str(self.posts_dir.resolve())):
            raise BlogPublishError(f"path traversal detected for slug {slug!r}")
        return resolved
