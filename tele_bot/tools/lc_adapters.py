"""
LangChain @tool wrappers for existing tools.

These adapters expose project tools as LangChain tools that can be bound to
ChatOpenAI via llm.bind_tools().
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from tele_bot.llm.opencli_gateway import OpenCLIGateway
from tele_bot.tools.adviser import AdviserTool
from tele_bot.tools.blog_publish import BlogPublishTool
from tele_bot.tools.domain_hotspot import DomainHotspotTool, to_json as _hotspot_to_json
from tele_bot.tools.file_system import FileSystemTool
from tele_bot.tools.knowledge_tool import KnowledgeTool
from tele_bot.tools.opencli_search import OpenCLISearchTool
from tele_bot.tools.shell_sandbox import ShellSandboxTool
from tele_bot.tools.write_report import WriteReportTool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WINDBORNE_POSTS = Path("/home/agiuser/workspace/trial/windborne-blog/src/content/posts")
# Directories the LLM is allowed to write restored knowledge docs into.
# Intentionally excludes _REPO_ROOT itself — LLM must not write to source dirs.
_KNOWLEDGE_ALLOWED_ROOTS: tuple[Path, ...] = (
    _REPO_ROOT / "reports",
    Path("/tmp"),
)


def _validate_knowledge_output_path(output_path: str | None) -> str | None:
    """Raise ValueError if output_path escapes allowed directories."""
    if not output_path:
        return None
    resolved = Path(output_path).resolve()
    for root in _KNOWLEDGE_ALLOWED_ROOTS:
        try:
            resolved.relative_to(root)
            return output_path
        except ValueError:
            continue
    allowed = ", ".join(str(r) for r in _KNOWLEDGE_ALLOWED_ROOTS)
    raise ValueError(
        f"output_path {output_path!r} is outside allowed directories: {allowed}"
    )


def _truncate_json(obj: Any, max_chars: int = 8000) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f'... <truncated, total {len(text)} chars>'


def build_spike_tools(
    *,
    filesystem_tool: FileSystemTool | None = None,
    shell_tool: ShellSandboxTool | None = None,
    adviser_tool: AdviserTool | None = None,
    include_adviser: bool = False,
) -> list:
    """
    Return the minimal tool set used in Phase 0-Spike.

    Always includes:
      - filesystem_list_dir
      - filesystem_read_file
      - shell_execute

    Optionally (set include_adviser=True):
      - ask_adviser  — high-latency second-opinion tool (OpenCLI ChatGPT)

    The adviser is OFF by default in spike tests because its 1-5 minute latency
    would dominate test runtime; turn it on for real Telegram sessions.
    """
    fs = filesystem_tool or FileSystemTool()
    sh = shell_tool or ShellSandboxTool()

    @tool
    def filesystem_list_dir(path: str, depth: int = 1) -> str:
        """List entries under a directory inside allowed roots.

        Args:
            path: absolute or home-relative directory path (e.g. "/tmp").
            depth: 1 lists only the directory; 2 also lists one level deeper.

        Returns a JSON string with {path, depth, entries: [{path, type}]}.
        """
        return _truncate_json(fs.list_dir(path, depth=depth))

    @tool
    def filesystem_read_file(path: str, max_lines: int = 200) -> str:
        """Read a text file inside allowed roots.

        Args:
            path: absolute or home-relative file path.
            max_lines: read at most this many lines from the start of the file.

        Returns a JSON string with {path, line_count, truncated, lines}.
        """
        return _truncate_json(fs.read_file(path, max_lines=max_lines))

    @tool
    def shell_execute(command: str) -> str:
        """Run a shell command inside a strict sandbox.

        Only whitelisted commands are allowed: ls, cat, head, tail, grep, find, wc, du, df.
        Forbidden tokens include rm, sudo, pipes, redirects, etc.

        Args:
            command: the shell command to execute, e.g. "ls /tmp".

        Returns a JSON string with {command, returncode, stdout, stderr, truncated}.
        """
        return _truncate_json(sh.execute_shell(command))

    tools: list = [filesystem_list_dir, filesystem_read_file, shell_execute]

    if include_adviser:
        adv = adviser_tool or AdviserTool()

        @tool
        def ask_adviser(question: str, context: str = "") -> str:
            """Consult the adviser (OpenCLI ChatGPT) for a second opinion.

            EXPENSIVE: each call takes 1-5 minutes. Use sparingly — only at key
            decision points (design choices, code review before edits, sanity
            checks on non-reversible actions, choosing between two tools).
            Do NOT use for routine lookups; you already have other tools for that.

            The adviser is opinionated and concise (1-5 sentences). It may
            disagree with your plan — weigh its advice but you decide whether
            to follow it.

            Args:
                question: what you want the adviser to weigh in on.
                context: relevant code/data/prior reasoning the adviser needs
                    to give useful advice. Include enough to be actionable.

            Returns the adviser's plain-text reply. If the adviser is
            unavailable, returns a degraded-mode notice you should not surface
            to the user.
            """
            return adv.consult(question, context=context)

        tools.append(ask_adviser)

    return tools


def build_core_tools(
    *,
    filesystem_tool: FileSystemTool | None = None,
    shell_tool: ShellSandboxTool | None = None,
    search_tool: OpenCLISearchTool | None = None,
    knowledge_tool: KnowledgeTool | None = None,
    write_report_tool: WriteReportTool | None = None,
    blog_publish_tool: BlogPublishTool | None = None,
    domain_hotspot_tool: DomainHotspotTool | None = None,
    adviser_tool: AdviserTool | None = None,
    include_adviser: bool = False,
    include_blog_publish: bool = False,
    include_domain_hotspot: bool = False,
) -> list:
    """
    Return the FULL Phase 0-Core tool set covering all 7 legacy workflows
    plus optional adviser, blog_publish, domain_hotspot.

    Adds to build_spike_tools:
      - opencli_search           — W4 search
      - knowledge_restore        — W5 restore-cheap
      - write_report             — W2 / W4 save-to-md

    Optional (Phase 2A):
      - blog_publish             — publish markdown to windborne-blog posts dir

    Optional (Phase 3):
      - domain_hotspot           — fetch curated AI / robotics RSS feeds

    Reasoning model switching (W3) remains in the executor's responsibility,
    not a tool — system prompt + model choice at LLM construction.
    """
    base = build_spike_tools(
        filesystem_tool=filesystem_tool,
        shell_tool=shell_tool,
        adviser_tool=adviser_tool,
        include_adviser=include_adviser,
    )

    search = search_tool or OpenCLISearchTool()
    knowledge = knowledge_tool or KnowledgeTool(gateway=OpenCLIGateway())
    writer = write_report_tool or WriteReportTool()

    @tool
    def opencli_search(query: str) -> str:
        """Search the web via OpenCLI's DuckDuckGo bridge.

        Use this for time-sensitive lookups (latest versions, recent news,
        documentation links). Returns the top 5 results.

        Args:
            query: search terms; pass user's intent verbatim or paraphrased.

        Returns JSON {query, engine, result_count, results: [{rank, title, url, snippet}]}.
        """
        return _truncate_json(search.execute(query))

    @tool
    def knowledge_restore(source: str, output_path: str | None = None) -> str:
        """Restore a distilled note into a full markdown document.

        Use when the user provides a compressed/outline-style note and wants
        the expanded long-form version. Uses OpenCLI ChatGPT for restoration
        (high-quality long-form generation).

        Args:
            source: either the distilled note text directly, OR a path to a
                file containing it.
            output_path: optional explicit output path; defaults next to source
                or in the repo root for inline text.

        Returns the file path of the restored markdown document.
        """
        return knowledge.restore_knowledge(
            source, output_path=_validate_knowledge_output_path(output_path)
        )

    @tool
    def write_report(content: str, title: str = "", filename: str = "") -> str:
        """Save a finished markdown report to the project's reports/ directory.

        Use this AFTER you have produced the full markdown content yourself
        (or via opencli_search + your own summarisation). Do NOT use it for
        scratch notes — only when the user wants a persistent document.

        Args:
            content: the full markdown body to write.
            title: optional human-readable title; used to derive a filename
                if `filename` is not given.
            filename: optional explicit base filename (with or without .md);
                must be slug-shaped (a-z, 0-9, dashes/underscores).

        Returns the absolute path where the report was written. Never
        overwrites: if the slug exists, a numeric suffix is appended.
        """
        return writer.write(
            content,
            title=title or None,
            filename=filename or None,
        )

    return (
        base
        + [opencli_search, knowledge_restore, write_report]
        + ([_make_blog_publish_tool(blog_publish_tool)] if include_blog_publish else [])
        + ([_make_domain_hotspot_tool(domain_hotspot_tool)] if include_domain_hotspot else [])
    )


def _make_blog_publish_tool(publisher: BlogPublishTool | None) -> Any:
    pub = publisher or BlogPublishTool(posts_dir=_WINDBORNE_POSTS)

    @tool
    def blog_publish(slug: str, content: str) -> str:
        """Publish a markdown article to the windborne blog.

        Writes `content` as `<slug>.md` in the blog's posts directory.
        Fails if the slug already exists (no overwrite).
        No git operations are performed — committing and pushing is handled
        separately by the user.

        Args:
            slug: URL-safe identifier for the post, e.g. "my-first-post".
                  Only [a-z0-9-], max 80 chars.
            content: full markdown string, must include YAML frontmatter with
                     title, published (YYYY-MM-DD), and description fields.

        Returns:
            Absolute path of the created file.
        """
        return pub.publish(slug=slug, content=content)

    return blog_publish


def _make_domain_hotspot_tool(hotspot: DomainHotspotTool | None) -> Any:
    fetcher = hotspot or DomainHotspotTool()

    @tool
    def domain_hotspot(domain: str, limit: int = 10) -> str:
        """Fetch curated hotspot articles for a domain.

        Use this when the user asks about today's AI hotspots, AI news,
        robotics news, robotics industry updates, what's trending in AI
        or robotics, recent robotics papers (arXiv cs.RO), or popular
        robotics open-source projects on GitHub.

        Args:
            domain: "ai" — single curated zh-CN AI source (AI HOT).
                    "robotics" — 8 parallel sources merged: arXiv cs.RO,
                    Robohub, Robotics & Automation News, TechXplore,
                    RoboticsTomorrow, qbitai, zhidx, GitHub trending
                    robotics repos.
            limit: max items total across all sources; clamped to 1..30.
                Default 10. (For robotics, items are merged from all
                sources then sorted by published date desc and truncated.)

        Returns:
            JSON string.
            For "ai" (single source): {domain, source, fetched_at, items: [...]}
              with items having {title, link, summary, published, categories}.
            For "robotics" (multi-source): {domain, fetched_at,
              sources_succeeded, sources_failed, items: [...], errors: [...]}
              where each item additionally has a "source" key naming the
              originating feed (e.g. "robohub", "arxiv_cs_ro",
              "github_robotics"). errors[] lists failed sources without
              raising.
            On unknown domain raises ValueError. Per-source HTTP/parse
            failures never raise — they appear in errors[].
        """
        return _hotspot_to_json(fetcher.fetch(domain=domain, limit=limit))

    return domain_hotspot
