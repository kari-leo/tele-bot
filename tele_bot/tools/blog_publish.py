"""
BlogPublishTool — publishes a markdown article to windborne-blog's posts dir.

Safety rules (enforced, not advisory):
- posts_dir verified writable at init via real sentinel write/read/delete
- slug validated: [a-z0-9-] only, 1–80 chars, no leading '-'
- output path constrained to posts_dir (no traversal, no absolute paths)
- no overwrite: raises BlogPublishError if slug already exists
- frontmatter required: title, published (YYYY-MM-DD), description
- git push is opt-in and requires a separate confirmation token
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from threading import RLock
from typing_extensions import NotRequired, TypedDict

from tele_bot.tools.git_push import GitPushError, GitPushTool
from tele_bot.tools.file_system import FileSystemTool

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,79}$")
_FM_FIELD_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_REQUIRED_FM = {"title", "published", "description"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_CONTENT_CHARS = 400_000


class BlogPublishError(Exception):
    pass


class BlogSourceDocument(TypedDict):
    source_path: str
    slug: str
    title: NotRequired[str]
    description: NotRequired[str]
    order: NotRequired[int]
    published: NotRequired[str]


@dataclass
class BlogPublishTool:
    posts_dir: Path
    git_push_tool: GitPushTool | None = None
    _publish_lock: RLock = field(default_factory=RLock, init=False, repr=False)

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

    def publish_from_file(
        self, slug: str, source_path: str, *, title: str = "",
        description: str = "", published: str = "", project: str = "",
        order: int = 0, filesystem_tool: FileSystemTool | None = None,
    ) -> str:
        """Publish an allowed UTF-8 source without sending its body through the LLM.

        Metadata replaces existing frontmatter; the Markdown body is verbatim.
        A project document is attached to an existing project in the same site.
        """
        with self._publish_lock:
            return self._publish_from_file(
                slug, source_path, title=title, description=description, published=published,
                project=project, order=order, filesystem_tool=filesystem_tool)

    def _publish_from_file(self, slug, source_path, *, title, description, published,
                           project, order, filesystem_tool) -> str:
        self._validate_slug(slug)
        source = (filesystem_tool or FileSystemTool()).resolve_file(source_path)
        if source.stat().st_size > MAX_CONTENT_CHARS * 4:
            raise BlogPublishError("source file is too large")
        body = source.read_bytes().decode("utf-8-sig")
        body = self._markdown_body(body)
        if not body.strip():
            raise BlogPublishError("source body is empty")
        title = title.strip() or source.stem
        def quote(value: str) -> str:
            return json.dumps(value, ensure_ascii=False)
        fields = [f"title: {quote(title)}", f"description: {quote(description)}"]
        content_root = self.posts_dir.resolve().parent
        if project:
            self._validate_slug(project)
            project_file = (content_root / "projects" / f"{project}.md").resolve()
            if not project_file.is_relative_to(content_root / "projects") or not project_file.is_file():
                raise BlogPublishError(f"project does not exist: {project}")
            destination = (content_root / "projectDocuments" / project).resolve()
            if not destination.is_relative_to(content_root):
                raise BlogPublishError("project document destination is outside the content directory")
            fields += [f"project: {quote(project)}", f"order: {int(order)}"]
        else:
            published = published or date.today().isoformat()
            try:
                date.fromisoformat(published)
            except ValueError as exc:
                raise BlogPublishError("published must be a valid YYYY-MM-DD date") from exc
            if not _DATE_RE.fullmatch(published):
                raise BlogPublishError("published must be YYYY-MM-DD")
            if not description.strip():
                raise BlogPublishError("description must not be empty for a post")
            fields.append(f"published: {published}")
            destination = self.posts_dir.resolve()
        content = "---\n" + "\n".join(fields) + "\n---\n" + body
        if len(content) > MAX_CONTENT_CHARS:
            raise BlogPublishError(f"content exceeds {MAX_CONTENT_CHARS} chars")
        destination.mkdir(parents=True, exist_ok=True)
        out_path = (destination / f"{slug}.md").resolve()
        if not out_path.is_relative_to(destination):
            raise BlogPublishError("destination escapes the publishing directory")
        # Reuse an already-published body even if the model invents another slug.
        # This also works after restart and for files written by older versions.
        candidates = sorted(destination.glob("*.md"))
        if out_path.exists():
            candidates = [out_path, *(path for path in candidates if path.resolve() != out_path)]
        for path in candidates:
            resolved = path.resolve()
            if not resolved.is_relative_to(destination) or not resolved.is_file():
                continue
            if resolved.stat().st_size > MAX_CONTENT_CHARS * 4:
                continue
            try:
                existing_body = self._markdown_body(resolved.read_bytes().decode("utf-8-sig"))
            except UnicodeDecodeError:
                continue
            if existing_body == body:
                return str(resolved)
        try:
            with out_path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
        except FileExistsError as exc:
            raise BlogPublishError(f"slug already exists: {out_path}") from exc
        return str(out_path)

    @staticmethod
    def _markdown_body(text: str) -> str:
        frontmatter = re.match(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
        return text[frontmatter.end():] if frontmatter else text

    def publish_files(self, documents: list[BlogSourceDocument], *, project: str = "",
                      filesystem_tool: FileSystemTool | None = None) -> dict:
        """Prepare a complete batch, never relying on historical publication paths."""
        receipt = {"status": "failed", "requested_count": len(documents),
                   "written_count": 0, "pushed_count": 0, "files": []}
        fs = filesystem_tool or FileSystemTool()
        try:
            if not 1 <= len(documents) <= 20:
                raise BlogPublishError("batch requires 1–20 source documents")
            # Validate all source locations before any document is written.
            for document in documents:
                self._validate_slug(document["slug"])
                fs.resolve_file(document["source_path"])
            if len({document["slug"] for document in documents}) != len(documents):
                raise BlogPublishError("each batch document must have a unique slug")
            with self._publish_lock:
                for document in documents:
                    path = self.publish_from_file(
                        document["slug"], document["source_path"], project=project,
                        title=document.get("title", ""), description=document.get("description", ""),
                        order=document.get("order", 0), published=document.get("published", ""),
                        filesystem_tool=fs)
                    receipt["files"].append({"source_path": document["source_path"],
                                             "path": path, "status": "written"})
                    receipt["written_count"] += 1
            if len({item["path"] for item in receipt["files"]}) != len(documents):
                raise BlogPublishError("batch contains identical bodies that resolve to the same document")
            receipt["status"] = "written"
        except (BlogPublishError, ValueError, OSError, KeyError) as exc:
            receipt["error"] = str(exc)
        return receipt

    def push_files(self, *, chat_id: str, file_paths: list[str], commit_message: str = "",
                   remote: str = "origin", branch: str = "") -> str:
        content_root = self.posts_dir.resolve().parent
        roots = (self.posts_dir.resolve(), content_root / "projectDocuments")
        paths = [Path(path).resolve() for path in file_paths]
        if any(not any(path.is_relative_to(root) for root in roots) or path.suffix.lower() != ".md"
               for path in paths):
            raise BlogPublishError("only existing blog posts or projectDocuments Markdown files can be pushed")
        try:
            return self._git_push().push_files(
                chat_id=chat_id, file_paths=[str(path) for path in paths],
                commit_message=commit_message or None, remote=remote or None, branch=branch or None)
        except GitPushError as exc:
            raise GitPushError(f"{exc}\n博客文件：{', '.join(map(str, paths))}") from exc

    def request_push(
        self,
        *,
        chat_id: str,
        file_path: str,
        commit_message: str | None = None,
        remote: str | None = None,
        branch: str | None = None,
        confirmation_tool: str = "blog_publish",
    ) -> str:
        return self._git_push().request_push(
            chat_id=chat_id,
            file_path=file_path,
            commit_message=commit_message,
            remote=remote,
            branch=branch,
            confirmation_tool=confirmation_tool,
        )

    def confirm_push(self, *, chat_id: str, confirm_token: str) -> str:
        return self._git_push().confirm_push(
            chat_id=chat_id,
            confirm_token=confirm_token,
        )

    def _git_push(self) -> GitPushTool:
        with self._publish_lock:
            if self.git_push_tool is None:
                self.git_push_tool = GitPushTool(repo_root=self.posts_dir.resolve().parent)
            return self.git_push_tool

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

        for field_name in ("title", "description"):
            if not fm_values.get(field_name):
                raise BlogPublishError(f"frontmatter field '{field_name}' must not be empty")

        published = fm_values.get("published", "")
        if not published:
            raise BlogPublishError("frontmatter field 'published' must not be empty")
        if not _DATE_RE.match(published):
            raise BlogPublishError(
                f"frontmatter 'published' must be YYYY-MM-DD, got {published!r}"
            )

    def _safe_path(self, slug: str) -> Path:
        resolved = (self.posts_dir / f"{slug}.md").resolve()
        if not resolved.is_relative_to(self.posts_dir.resolve()):
            raise BlogPublishError(f"path traversal detected for slug {slug!r}")
        return resolved
