"""
LangChain @tool wrappers for existing tools.

These adapters expose project tools as LangChain tools that can be bound to
ChatOpenAI via llm.bind_tools().
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import InjectedState
from mcp import Client

try:
    from langchain_core.runnables import ensure_config
except ImportError:  # Lightweight test stubs expose only RunnableConfig.
    def ensure_config() -> RunnableConfig:
        return {}  # type: ignore[return-value]

from tele_bot.llm.opencli_gateway import OpenCLIGateway
from tele_bot.tools.adviser import AdviserTool
from tele_bot.tools.blog_publish import BlogPublishTool, BlogSourceDocument
from tele_bot.tools.domain_hotspot import DomainHotspotTool, to_json as _hotspot_to_json
from tele_bot.tools.file_system import FileSystemTool
from tele_bot.tools.git_push import GitPushError, GitPushTool
from tele_bot.tools.knowledge_tool import KnowledgeTool
from tele_bot.tools.bailian_web import BailianWebSearchTool, build_web_tools
from tele_bot.tools.shell_sandbox import ShellSandboxTool
from tele_bot.tools.write_report import WriteReportTool
from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.mcp.schedule import create_schedule_server
from tele_bot.models import IncomingMessage
from tele_bot.router.codex_service import CodexCommandService
from tele_bot.router.codex_history_service import CodexHistoryService
from tele_bot.tools.codex_history import CURRENT_CODEX_HISTORY_MESSAGE, CodexHistoryError
from tele_bot.scheduled.service import ScheduleService
from tele_bot.scheduled.proposals import ScheduleRequest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WINDBORNE_POSTS = Path(
    os.environ.get(
        "WINDBORNE_POSTS_DIR",
        str(_REPO_ROOT.parent / "windborne-blog" / "src" / "content" / "posts"),
    )
)
# Directories the LLM is allowed to write restored knowledge docs into.
# Intentionally excludes _REPO_ROOT itself — LLM must not write to source dirs.
_KNOWLEDGE_ALLOWED_ROOTS: tuple[Path, ...] = (
    _REPO_ROOT / "reports",
    Path("/tmp"),
)
CURRENT_CHAT_ID: ContextVar[str | None] = ContextVar("current_chat_id", default=None)
CURRENT_SCHEDULE_ALLOWED: ContextVar[bool] = ContextVar("current_schedule_allowed", default=False)
CURRENT_SCHEDULE_MESSAGE: ContextVar[IncomingMessage | None] = ContextVar(
    "current_schedule_message", default=None
)


def _chat_id_from_config(config: RunnableConfig | None) -> str:
    if config is None:
        config = ensure_config()
    if config:
        configurable = config.get("configurable") if isinstance(config, dict) else None
        if isinstance(configurable, dict):
            chat_id = configurable.get("chat_id") or configurable.get("thread_id")
            if chat_id:
                return str(chat_id)
    context_chat_id = CURRENT_CHAT_ID.get()
    if context_chat_id:
        return context_chat_id
    raise ValueError("chat_id is missing from the current message context")


def _blog_push_authorized(config: RunnableConfig | None) -> bool:
    """Use the actual current user message, never model-supplied tool arguments."""
    actual = config or ensure_config()
    configurable = actual.get("configurable", {})
    text = configurable.get("message_text", "")
    if not text and CURRENT_SCHEDULE_MESSAGE.get() is not None:
        text = CURRENT_SCHEDULE_MESSAGE.get().text
    if not isinstance(text, str):
        return False
    if re.search(r"不要|无需|不需要|暂不|别推|别发布|只写入|仅写入|为什么|为何|怎么回事|解释|看看|检查|没有直接推|do not|don't|without|\b(?:explain|why|what|how)\b", text, re.I):
        return False
    actions = r"推出去|推送|推到|直接推|上线|发布|\bpush\b|\bpublish\b|\bdeploy\b"
    command = r"请|帮我|替我|将|把|直接|立即|现在|继续|重新|执行|重试|再|\bplease\b"
    return bool(re.search(rf"(?:{command}).*(?:{actions})", text, re.I | re.S)
                or re.match(rf"\s*(?:{actions})", text, re.I))


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
    """Bound tool output without cutting JSON syntax or losing a file cursor.

    File pages retain whole lines. A single oversized line is returned intact
    so the reader can always advance, even if it exceeds the usual budget.
    Other oversized results return a JSON preview with short scalar metadata.
    """
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    if isinstance(obj, dict) and "next_start_line" in obj and obj.get("lines"):
        lines = obj["lines"]

        def page_text(count: int) -> str:
            page = {**obj, "lines": lines[:count]}
            if count < len(lines):
                page.update(truncated=True, next_start_line=lines[count]["line"])
            return json.dumps(page, ensure_ascii=False, default=str)

        # Search for the largest complete page that fits the output budget.
        low, high = 1, len(lines)
        while low < high:
            mid = (low + high + 1) // 2
            if len(page_text(mid)) <= max_chars:
                low = mid
            else:
                high = mid - 1
        return page_text(low)

    preview = {key: value for key, value in obj.items()
               if value is None or isinstance(value, (bool, int, float))
               or isinstance(value, str) and len(value) < 200} if isinstance(obj, dict) else {}
    preview.update(truncated=True, total_chars=len(text), preview="")
    if len(json.dumps(preview, ensure_ascii=False)) > max_chars:
        preview = {"truncated": True, "total_chars": len(text), "preview": ""}
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        preview["preview"] = text[:mid]
        if len(json.dumps(preview, ensure_ascii=False)) <= max_chars:
            low = mid
        else:
            high = mid - 1
    preview["preview"] = text[:low]
    return json.dumps(preview, ensure_ascii=False)


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
    would dominate test runtime; turn it on for real sessions.
    """
    fs = filesystem_tool or FileSystemTool()
    sh = shell_tool or ShellSandboxTool()

    @tool
    def filesystem_list_dir(path: str, depth: int = 1) -> str:
        """List entries under a directory inside allowed roots.

        Args:
            path: absolute path or path relative to the bot runtime root.
            depth: 1 lists only the directory; 2 also lists one level deeper.

        Returns a JSON string with {path, depth, entries: [{path, type}]}.
        """
        return _truncate_json(fs.list_dir(path, depth=depth))

    @tool
    def filesystem_read_file(path: str, max_lines: int = 200, start_line: int = 1) -> str:
        """Read a page of a text file inside allowed roots.

        When truncated is true, continue with start_line=next_start_line.
        Increasing max_lines cannot bypass the output budget; never repeatedly
        read from line 1 to obtain the remainder of a long document.

        Args:
            path: absolute path or path relative to the bot runtime root.
            max_lines: read at most this many lines per page (maximum 500).
            start_line: first line to read, numbered from 1.

        Returns JSON {path, line_count, start_line, truncated, next_start_line,
        lines}. next_start_line is null at EOF. A single oversized line may
        exceed the usual output budget to preserve its complete content.
        """
        return _truncate_json(fs.read_file(path, max_lines=max_lines, start_line=start_line))

    @tool
    def shell_execute(command: str) -> str:
        """Run a shell command inside a strict sandbox.

        Only whitelisted read-only commands are allowed: ls/dir, cat/type,
        head, tail, grep, find, wc, du, df.
        Forbidden tokens include rm, sudo, pipes, redirects, etc.
        On Windows, dir supports /s (recursive), /b (full paths), /ad
        (directories only), and one quoted path with a final-component wildcard.

        Args:
            command: the shell command to execute, e.g. "ls" or "ls data".

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
    search_tool: BailianWebSearchTool | None = None,
    knowledge_tool: KnowledgeTool | None = None,
    write_report_tool: WriteReportTool | None = None,
    blog_publish_tool: BlogPublishTool | None = None,
    domain_hotspot_tool: DomainHotspotTool | None = None,
    adviser_tool: AdviserTool | None = None,
    include_adviser: bool = False,
    include_blog_publish: bool = False,
    include_domain_hotspot: bool = False,
    feishu_adapter: FeishuWebhookAdapter | None = None,
    codex_service: CodexCommandService | None = None,
    codex_history_service: CodexHistoryService | None = None,
    schedule_service: ScheduleService | None = None,
    knowledge_search=None,
    skill_loader=None,
    bitable_service=None,
) -> list:
    """
    Return the FULL Phase 0-Core tool set covering all 7 legacy workflows
    plus optional adviser, blog_publish, domain_hotspot.

    Adds to build_spike_tools:
      - web_search / web_extractor — Bailian search and URL reading
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

    knowledge = knowledge_tool or KnowledgeTool(gateway=OpenCLIGateway())
    writer = write_report_tool or WriteReportTool()
    git_push = GitPushTool(repo_root=_REPO_ROOT)

    def _chat_id_from_config(config: RunnableConfig | None) -> str:
        if config is None:
            config = ensure_config()
        if config:
            configurable = config.get("configurable") if isinstance(config, dict) else None
            if isinstance(configurable, dict):
                chat_id = configurable.get("chat_id") or configurable.get("thread_id")
                if chat_id:
                    return str(chat_id)
        context_chat_id = CURRENT_CHAT_ID.get()
        if context_chat_id:
            return context_chat_id
        raise ValueError("chat_id is missing from the current message context")

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
    def write_report(
        content: str,
        title: str = "",
        filename: str = "",
        git_push_enabled: bool = False,
        push_confirm_token: str = "",
        commit_message: str = "",
        remote: str = "origin",
        branch: str = "",
        config: RunnableConfig | None = None,
    ) -> str:
        """Save a finished markdown report to the project's reports/ directory.

        Use this AFTER you have produced the full markdown content yourself
        (or via web_search / web_extractor + your own summarisation). Do NOT use it for
        scratch notes — only when the user wants a persistent document.

        Args:
            content: the full markdown body to write.
            title: optional human-readable title; used to derive a filename
                if `filename` is not given.
            filename: optional explicit base filename (with or without .md);
                must be slug-shaped (a-z, 0-9, dashes/underscores).
            git_push_enabled: when true, enables optional git add/commit/push.
                Push is always two-step and requires confirmation token.
            push_confirm_token: confirmation token returned by a previous call.
            commit_message: optional commit message for push flow.
            remote: git remote for push flow. defaults to "origin".
            branch: git branch for push flow. defaults to current branch.

        Returns the absolute path where the report was written. Never
        overwrites: if the slug exists, a numeric suffix is appended.
        """
        if push_confirm_token.strip():
            if not git_push_enabled:
                raise ValueError(
                    "git_push_enabled must be true when push_confirm_token is provided"
                )
            chat_id = _chat_id_from_config(config)
            push_result = git_push.confirm_push(
                chat_id=chat_id,
                confirm_token=push_confirm_token.strip(),
            )
            return push_result

        out_path = writer.write(
            content,
            title=title or None,
            filename=filename or None,
        )
        if not git_push_enabled:
            return out_path

        chat_id = _chat_id_from_config(config)

        request_result = git_push.request_push(
            chat_id=chat_id,
            file_path=out_path,
            commit_message=commit_message or None,
            remote=remote or None,
            branch=branch or None,
        )
        return request_result

    tools = base + build_web_tools(search_tool) + [knowledge_restore, write_report]

    from tele_bot.bitable.bootstrap import build_bitable_service
    from tele_bot.tools.bitable import build_bitable_tools
    base_service = bitable_service if bitable_service is not None else build_bitable_service()
    tools.extend(build_bitable_tools(base_service))

    if knowledge_search is not None:
        from tele_bot.tools.knowledge_search import build_knowledge_search_tool
        tools.append(build_knowledge_search_tool(knowledge_search))
    if skill_loader is not None:
        from tele_bot.skills.loader import build_skill_tool
        tools.append(build_skill_tool(skill_loader))

    if schedule_service is not None:
        @tool
        def schedule_manage(
            request: ScheduleRequest | None = None,
            context_messages: Annotated[list, InjectedState("tool_context_messages")] = None,
        ) -> str:
            """Call the schedule MCP tool for the current user's message.

            Use for future reminders or execution, scheduled Codex/skill work,
            scheduling queries or changes, time corrections, and schedule
            confirmation or authorization replies. Conversation context is
            injected automatically, including this turn's earlier tool results.
            For resolved references or task requirements, request may contain
            operation (create/update/delete/list/get/history/clarify), task_id,
            tasks (content, at, rule, mode, scope, missed_policy). Derive these
            from the user's intent and available context; clarify ambiguous IDs.
            create/update needs a nonempty tasks array with content and at in
            each item. If uncertain or incomplete, omit request: the scheduling
            parser resolves the authenticated text and conversation for you.
            Identity and confirmation always use the original user message.
            Never claim scheduling is unavailable before calling this tool.
            Do not call Codex immediately when the user requested a future run.
            """
            if not CURRENT_SCHEDULE_ALLOWED.get():
                return json.dumps({"handled": True, "text":
                    "此入口未校验调用者身份，暂不开放定时任务管理；请从飞书发送。"},
                    ensure_ascii=False)
            message = CURRENT_SCHEDULE_MESSAGE.get()
            if message is None:
                raise RuntimeError("当前消息上下文缺失，无法调用定时 MCP 工具。")

            async def call_mcp() -> str:
                async with Client(create_schedule_server(
                    schedule_service, message, context_messages=context_messages,
                )) as client:
                    result = await client.call_tool("schedule_manage", {"request": request})
                    if result.is_error or not result.content:
                        raise RuntimeError("定时 MCP 工具调用失败。")
                    return str(result.content[0].text)

            return asyncio.run(call_mcp())

        tools.append(schedule_manage)

    if codex_history_service is not None:
        @tool
        def codex_history_lookup(search: str = "", thread_id: str = "", cursor: str = "", archived: bool = False) -> str:
            """Find native Codex conversation IDs, titles, workspaces and saved models.

            Use for Codex history, locating a named conversation, or preparing
            continuation in a specified session. Reads metadata through the
            native App Server, without scanning .codex or changing file roots.
            search filters titles; thread_id reads one known UUID; cursor pages
            results. Never invent IDs or choose among ambiguous matches.
            For scheduled continuation pass the selected ID in schedule_manage
            tasks[].scope.codex_thread_id and its cwd in scope.directories.
            For immediate continuation show /codex resume <ID> <task>.
            This tool never starts a turn. Only authenticated admin private
            chats can read history; model-supplied identity cannot grant access.
            """
            try:
                result = codex_history_service.lookup(
                    CURRENT_CODEX_HISTORY_MESSAGE.get(), search=search,
                    thread_id=thread_id, cursor=cursor or None, archived=archived,
                )
            except CodexHistoryError as exc:
                result = {"error": str(exc)}
            return json.dumps(result, ensure_ascii=False)

        tools.append(codex_history_lookup)

    if codex_service is not None:
        @tool
        def codex_inspect(
            task: str,
            workspace_hint: str = "",
            use_context: bool = False,
            config: RunnableConfig | None = None,
        ) -> str:
            """Ask Codex to inspect a project without changing files.

            Use only when the user explicitly asks for Codex or a code/project
            inspection. workspace_hint may be a path or a fuzzy project clue.
            Never use this tool to apply changes.
            """
            chat_id = _chat_id_from_config(config)
            user_id = str((config or {}).get("configurable", {}).get("user_id", "agent"))
            return codex_service.handle_tool(
                chat_id=chat_id,
                user_id=user_id,
                mode="inspect",
                task=task,
                workspace_hint=workspace_hint,
                use_context=use_context,
            )

        @tool
        def codex_plan(
            task: str,
            workspace_hint: str = "",
            use_context: bool = False,
            config: RunnableConfig | None = None,
        ) -> str:
            """Ask Codex for a read-only modification plan.

            Use only when the user explicitly asks for a Codex plan. A later
            file-changing apply still requires the user's explicit token.
            """
            chat_id = _chat_id_from_config(config)
            user_id = str((config or {}).get("configurable", {}).get("user_id", "agent"))
            return codex_service.handle_tool(
                chat_id=chat_id,
                user_id=user_id,
                mode="plan",
                task=task,
                workspace_hint=workspace_hint,
                use_context=use_context,
            )

        tools.extend([codex_inspect, codex_plan])

        @tool
        def codex_apply_request(
            task: str,
            workspace_hint: str = "",
            use_context: bool = False,
            config: RunnableConfig | None = None,
        ) -> str:
            """Prepare a Codex file-changing request for user confirmation.

            This tool never changes files and never starts Codex apply. Use it
            when the user explicitly asks to modify or delete files through
            Codex. The returned structured command must be shown to the user;
            execution requires the user to reply with confirmation.
            """
            chat_id = _chat_id_from_config(config)
            user_id = str((config or ensure_config()).get("configurable", {}).get("user_id", "agent"))
            return codex_service.create_apply_request(
                chat_id=chat_id,
                task=task,
                workspace_hint=workspace_hint,
                use_context=use_context,
                user_id=user_id,
            )

        tools.append(codex_apply_request)

    if feishu_adapter is not None:
        @tool
        def feishu_send_file(path: str, config: RunnableConfig | None = None) -> str:
            """Send a local file to the current Feishu chat.

            The path must point to a file inside the bot's configured allowed
            filesystem roots. Use this after creating or locating the file.
            """
            target = (filesystem_tool or FileSystemTool()).resolve_file(path)
            chat_id = _chat_id_from_config(config)
            feishu_adapter.send_file(chat_id, target)
            return f"文件已发送：{target.name}"

        tools.append(feishu_send_file)

    publishing_tools = []
    if include_blog_publish:
        publisher = blog_publish_tool or BlogPublishTool(posts_dir=_WINDBORNE_POSTS)
        publishing_tools = [_make_blog_publish_tool(publisher),
                            _make_blog_publish_from_file_tool(publisher, filesystem_tool),
                            _make_blog_publish_files_tool(publisher, filesystem_tool),
                            _make_blog_push_tool(publisher)]
    return (
        tools
        + publishing_tools
        + ([_make_domain_hotspot_tool(domain_hotspot_tool)] if include_domain_hotspot else [])
    )


def _make_blog_publish_tool(publisher: BlogPublishTool | None) -> Any:
    pub = publisher or BlogPublishTool(posts_dir=_WINDBORNE_POSTS)

    @tool
    def blog_publish(
        slug: str,
        content: str,
        git_push_enabled: bool = False,
        push_confirm_token: str = "",
        commit_message: str = "",
        remote: str = "origin",
        branch: str = "",
        config: RunnableConfig | None = None,
    ) -> str:
        """Publish a markdown article to the windborne blog.

        Writes `content` as `<slug>.md` in the blog's posts directory.
        Fails if the slug already exists (no overwrite). Set git_push_enabled
        when the user requests publishing/pushing: an explicit current user
        instruction authorizes direct commit/push. Otherwise return a pending
        confirmation token. Confirming a token never writes the article again.
        Use blog_push for existing files and retries; never invent new slugs
        to retry a failed push.

        Args:
            slug: URL-safe identifier for the post, e.g. "my-first-post".
                  Only [a-z0-9-], max 80 chars.
            content: full markdown string, must include YAML frontmatter with
                     title, published (YYYY-MM-DD), and description fields.

        Returns:
            Absolute path, or a push confirmation/result message.
        """
        if push_confirm_token.strip():
            if not git_push_enabled:
                raise ValueError(
                    "git_push_enabled must be true when push_confirm_token is provided"
                )
            chat_id = _chat_id_from_config(config)
            return pub.confirm_push(
                chat_id=chat_id,
                confirm_token=push_confirm_token.strip(),
            )

        out_path = pub.publish(slug=slug, content=content)
        if not git_push_enabled:
            return out_path
        chat_id = _chat_id_from_config(config)
        if _blog_push_authorized(config):
            return pub.push_files(chat_id=chat_id, file_paths=[out_path],
                                  commit_message=commit_message, remote=remote, branch=branch)
        return pub.request_push(
            chat_id=chat_id,
            file_path=out_path,
            commit_message=commit_message or None,
            remote=remote or None,
            branch=branch or None,
        )

    return blog_publish


def _make_blog_publish_from_file_tool(pub: BlogPublishTool, filesystem_tool) -> Any:
    @tool
    def blog_publish_from_file(
        slug: str, source_path: str, title: str = "", description: str = "",
        published: str = "", project: str = "", order: int = 0,
        git_push_enabled: bool = False, push_confirm_token: str = "",
        commit_message: str = "", remote: str = "origin", branch: str = "",
        config: RunnableConfig | None = None,
    ) -> str:
        """Publish a complete UTF-8 Markdown source file, preserving its body verbatim.

        Prefer this tool for publishing original documents: pass source_path,
        never copy their bodies into tool arguments. Paths must be inside the
        configured filesystem roots. Generated metadata replaces any existing
        frontmatter. Leave project empty for a post (description required);
        set project to an existing project slug, e.g. telebot, to create a
        projectDocuments/<project>/<slug>.md portfolio document. order controls
        its position. Identical bodies reuse the existing document even with
        a new slug; existing files are never overwritten. Set git_push_enabled
        when the user requests publishing/pushing. An explicit current user
        instruction authorizes direct commit/push; otherwise return a pending
        confirmation token. Token confirmation never reads/writes the source.
        For existing files or a failed push, use blog_push; do not change slugs.
        """
        if push_confirm_token.strip():
            if not git_push_enabled:
                raise ValueError("git_push_enabled must be true when push_confirm_token is provided")
            return pub.confirm_push(chat_id=_chat_id_from_config(config),
                                    confirm_token=push_confirm_token.strip())
        out_path = pub.publish_from_file(
            slug=slug, source_path=source_path, title=title, description=description,
            published=published, project=project, order=order, filesystem_tool=filesystem_tool)
        if not git_push_enabled:
            return out_path
        if _blog_push_authorized(config):
            return pub.push_files(chat_id=_chat_id_from_config(config), file_paths=[out_path],
                                  commit_message=commit_message, remote=remote, branch=branch)
        return pub.request_push(chat_id=_chat_id_from_config(config), file_path=out_path,
                                commit_message=commit_message or None,
                                remote=remote or None, branch=branch or None,
                                confirmation_tool="blog_publish_from_file")

    return blog_publish_from_file


def _make_blog_push_tool(pub: BlogPublishTool) -> Any:
    @tool
    def blog_push(file_paths: list[str], commit_message: str = "", remote: str = "origin",
                  branch: str = "", push_confirm_token: str = "",
                  config: RunnableConfig | None = None) -> str:
        """Commit and push existing blog Markdown files without creating any documents.

        Prefer one batch of all returned paths after writing multiple documents.
        Use this for network retries, already-written documents, and existing
        pending tokens. Requires an explicit current user push/publish request
        or a valid chat-bound confirmation token. On network failure retry the
        same paths at most once, then report failure and stop; never republish
        or change slugs. A successful local write is not a successful push.
        """
        chat_id = _chat_id_from_config(config)
        if push_confirm_token.strip():
            return pub.confirm_push(chat_id=chat_id, confirm_token=push_confirm_token.strip())
        if not _blog_push_authorized(config):
            raise ValueError("当前消息未明确要求推送博客；请先取得用户的推送指令或确认已有令牌。")
        return pub.push_files(chat_id=chat_id, file_paths=file_paths, commit_message=commit_message,
                              remote=remote, branch=branch)

    return blog_push


def _make_blog_publish_files_tool(pub: BlogPublishTool, filesystem_tool) -> Any:
    @tool
    def blog_publish_files(documents: list[BlogSourceDocument], project: str = "",
                           git_push_enabled: bool = False, commit_message: str = "",
                           remote: str = "origin", branch: str = "",
                           config: RunnableConfig | None = None) -> str:
        """Publish ALL requested original source documents as one complete batch.

        Prefer this tool when the user asks to publish multiple documents.
        Supply every requested source_path and slug in documents, even when old
        logs claim some were published. It checks actual source files, preserves
        their bodies, reuses existing identical documents, and pushes all paths
        in one commit when git_push_enabled=true and the current user authorizes
        publishing. project attaches every document to the portfolio project.
        Returns requested_count, written_count, pushed_count and per-file paths.
        A failed write prevents push of an incomplete set. Only status=pushed
        confirms full batch publication. On push_failed use blog_push with the
        returned paths once, never change slugs; report any remaining failure.
        """
        receipt = pub.publish_files(documents, project=project, filesystem_tool=filesystem_tool)
        if receipt["status"] == "written" and git_push_enabled:
            if not _blog_push_authorized(config):
                receipt.update(status="awaiting_authorization",
                               error="文件已写入；当前消息未明确授权推送。请取得推送指令后用 blog_push 推送这些路径。")
            else:
                paths = [item["path"] for item in receipt["files"]]
                try:
                    result = pub.push_files(chat_id=_chat_id_from_config(config), file_paths=paths,
                                            commit_message=commit_message, remote=remote, branch=branch)
                    receipt.update(status="pushed", pushed_count=len(paths), push_result=result)
                    for item in receipt["files"]:
                        item["status"] = "pushed"
                except GitPushError as exc:
                    receipt.update(status="push_failed", error=str(exc))
        return json.dumps(receipt, ensure_ascii=False)

    return blog_publish_files


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
