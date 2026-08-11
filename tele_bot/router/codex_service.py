"""Message-level orchestration for explicit Codex commands."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from secrets import token_urlsafe
from threading import Lock
from pathlib import Path

from tele_bot.models import IncomingMessage
from tele_bot.router.codex_commands import CodexCommand, parse_codex_command
from tele_bot.tools.codex_runner import CodexResult, CodexRunner
from tele_bot.tools.workspace_resolver import WorkspaceResolver


@dataclass(frozen=True)
class PendingCodexRun:
    token: str
    request: str
    prompt: str
    workspace: str | None
    plan_output: str


@dataclass(frozen=True)
class PendingApplyRequest:
    token: str
    task: str
    prompt: str
    workspace: str
    context_used: bool
    operation: str
    target: str
    matched_files: tuple[str, ...]


@dataclass
class CodexConversationContext:
    workspace: str | None = None
    last_request: str = ""
    last_result: str = ""


class CodexCommandService:
    """Execute Codex only after an explicit command and matching confirmation."""

    def __init__(self, runner: CodexRunner, resolver: WorkspaceResolver | None = None) -> None:
        self.runner = runner
        self.resolver = resolver or self._build_resolver(runner)
        self._pending: dict[tuple[str, str], PendingCodexRun] = {}
        self._pending_apply: dict[tuple[str, str], PendingApplyRequest] = {}
        self._contexts: dict[str, CodexConversationContext] = {}
        self._lock = Lock()

    def handle(self, message: IncomingMessage) -> str | None:
        command = parse_codex_command(message.text)
        if command is None:
            return None
        if command.mode == "apply":
            return self._apply(message.chat_id, command.prompt, command.workspace)
        return self._inspect_or_plan(message.chat_id, command)

    def handle_confirmation(self, message: IncomingMessage) -> str | None:
        """Handle plain-text confirmation only when this chat has a pending request."""
        decision = message.text.strip().lower()
        if decision not in {"确认", "确认执行", "yes", "y", "取消", "cancel", "no", "n"}:
            return None
        with self._lock:
            has_pending = any(chat_id == message.chat_id for chat_id, _ in self._pending_apply)
        if not has_pending:
            return None
        if decision in {"取消", "cancel", "no", "n"}:
            with self._lock:
                self._pending_apply = {
                    key: value
                    for key, value in self._pending_apply.items()
                    if key[0] != message.chat_id
                }
            return "已取消 Codex apply 请求，未修改文件。"
        with self._lock:
            matches = [
                (key, value)
                for key, value in self._pending_apply.items()
                if key[0] == message.chat_id
            ]
            if len(matches) != 1:
                return "当前会话有多个待确认请求，请使用对应的 /codex apply <令牌>。"
            (chat_id, token), request = matches[0]
            del self._pending_apply[(chat_id, token)]
        return self._execute_apply_request(message.chat_id, request)

    def create_apply_request(
        self,
        *,
        chat_id: str,
        task: str,
        workspace_hint: str = "",
        use_context: bool = False,
    ) -> str:
        """Create a preview only; no Codex process is started."""
        task = task.strip()
        if not task:
            raise ValueError("Codex apply task is required")
        workspace, resolution_error = self._resolve_workspace(chat_id, task, workspace_hint or None)
        if resolution_error:
            return resolution_error
        workspace = workspace or self._default_workspace()
        if not workspace:
            return "无法确定 Codex 工作区，请补充目录。"
        prompt = self._prompt_with_context(chat_id, task) if use_context else task
        target = self._extract_target(task)
        matched_files = self._matched_files(workspace, target)
        token = token_urlsafe(12)
        request = PendingApplyRequest(
            token=token,
            task=task,
            prompt=prompt,
            workspace=workspace,
            context_used=use_context,
            operation="delete" if any(word in task.lower() for word in ("删除", "删掉", "remove", "delete")) else "modify",
            target=target,
            matched_files=matched_files,
        )
        with self._lock:
            self._pending_apply[(chat_id, token)] = request
        command = {
            "mode": "apply",
            "workspace": workspace,
            "task": task,
            "context_used": use_context,
            "sandbox": "workspace-write",
            "operation": request.operation,
            "target": request.target or None,
            "matched_files": list(request.matched_files),
            "confirmation_token": token,
        }
        return (
            "即将执行 Codex apply（尚未执行）：\n"
            f"```json\n{json.dumps(command, ensure_ascii=False, indent=2)}\n```\n\n"
            f"确认执行请回复：确认\n也可发送：/codex apply {token}\n"
            "取消请回复：取消"
        )

    def handle_tool(
        self,
        *,
        chat_id: str,
        user_id: str,
        mode: str,
        task: str,
        workspace_hint: str = "",
        use_context: bool = False,
    ) -> str:
        """Run the read-only/plan bridge exposed to the ordinary Agent."""
        if mode not in {"inspect", "plan"}:
            raise ValueError("Codex Agent bridge only supports inspect and plan")
        request = task.strip()
        if use_context:
            request = f"继续刚才的 Codex 工作：{request}"
        if workspace_hint.strip():
            request = f"在 {workspace_hint.strip()} {request}"
        response = self.handle(
            IncomingMessage(
                channel="feishu",
                user_id=user_id,
                chat_id=chat_id,
                text=f"/codex {mode} {request}",
            )
        )
        return response or "Codex 未返回结果。"

    def _inspect_or_plan(self, chat_id: str, command: CodexCommand) -> str:
        workspace, resolution_error = self._resolve_workspace(chat_id, command.prompt, command.workspace)
        if resolution_error:
            return resolution_error
        prompt = self._prompt_with_context(chat_id, command.prompt)
        try:
            result = self.runner.run(command.mode, prompt, workspace)
        except (RuntimeError, ValueError) as exc:
            return f"Codex 执行失败：{exc}"
        text = self._format_result(result)
        self._remember(chat_id, command.prompt, text, result.workspace)
        if command.mode == "plan" and result.returncode == 0:
            token = token_urlsafe(12)
            with self._lock:
                self._pending[(chat_id, token)] = PendingCodexRun(
                    token=token,
                    request=command.prompt,
                    prompt=prompt,
                    workspace=result.workspace,
                    plan_output=text,
                )
            text += f"\n\n确认执行请发送：/codex apply {token}"
        return text

    def _apply(self, chat_id: str, token: str, direct_workspace: str | None = None) -> str:
        with self._lock:
            pending = self._pending.pop((chat_id, token), None)
            pending_apply = self._pending_apply.pop((chat_id, token), None)
        if pending_apply is not None:
            return self._execute_apply_request(chat_id, pending_apply)
        if pending is not None:
            prompt = (
                "请执行已确认的修改计划。不要只描述方案；请直接在目标工作区完成修改，"
                "并在完成后检查变更结果。\n\n"
                f"原始任务与上下文：\n{pending.prompt}\n\n"
                f"只读 plan 输出：\n{pending.plan_output}"
            )
            workspace = pending.workspace
        elif self._looks_like_confirmation_token(token):
            return "Codex 确认令牌无效、已使用或不属于当前会话。请先发送 /codex plan <任务>。"
        else:
            prompt = token
            workspace, resolution_error = self._resolve_workspace(chat_id, prompt, direct_workspace)
            if resolution_error:
                return resolution_error
            prompt = self._prompt_with_context(chat_id, prompt)
        try:
            result = self.runner.run("apply", prompt, workspace)
        except (RuntimeError, ValueError) as exc:
            return f"Codex 执行失败：{exc}"
        text = self._format_result(result)
        self._remember(
            chat_id,
            pending.request if pending is not None else token,
            text,
            result.workspace,
        )
        return text

    def _execute_apply_request(self, chat_id: str, request: PendingApplyRequest) -> str:
        current_matches = self._matched_files(request.workspace, request.target)
        if request.target and current_matches != request.matched_files:
            return "待确认文件列表已发生变化，请重新创建 Codex apply 请求。"
        try:
            result = self.runner.run("apply", request.prompt, request.workspace)
        except (RuntimeError, ValueError) as exc:
            return f"Codex 执行失败：{exc}"
        text = self._format_result(result)
        self._remember(chat_id, request.task, text, result.workspace)
        return text

    @staticmethod
    def _extract_target(task: str) -> str:
        matches = re.findall(r"(?<![\w])[^\s/\\:：,，。；;]+[*?][^\s/\\:：,，。；;]*", task)
        return matches[-1].replace("\\*", "*") if matches else ""

    @staticmethod
    def _matched_files(workspace: str, target: str) -> tuple[str, ...]:
        if not target:
            return ()
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            return ()
        matches = sorted(
            str(path.relative_to(root))
            for path in root.glob(target)
            if path.is_file()
        )
        return tuple(matches)

    def _default_workspace(self) -> str | None:
        default_workspace = getattr(self.runner, "default_workspace", None)
        if default_workspace is not None:
            return str(default_workspace.expanduser().resolve())
        policy = getattr(self.runner, "policy", None)
        workspace = getattr(policy, "workspace_root", None)
        return str(workspace) if workspace is not None else None

    def _context_for(self, chat_id: str) -> CodexConversationContext:
        with self._lock:
            return self._contexts.setdefault(chat_id, CodexConversationContext())

    def _prompt_with_context(self, chat_id: str, prompt: str) -> str:
        context = self._context_for(chat_id)
        continuation_terms = ("刚才", "上次", "之前", "继续", "这个修改", "上述")
        if not any(term in prompt for term in continuation_terms):
            return prompt
        return (
            "这是同一个飞书会话中的后续 Codex 请求。请结合前情理解‘这个、该修改、"
            "上面的岗位方向’等指代，不要要求用户重复已经提供的内容。\n\n"
            f"默认工作区：{context.workspace or '未指定'}\n"
            f"上一条 Codex 请求：{context.last_request}\n"
            f"上一条 Codex 摘要：\n{context.last_result[:1000]}\n\n"
            f"当前请求：\n{prompt}"
        )

    def _remember(
        self,
        chat_id: str,
        request: str,
        result: str,
        workspace: str,
    ) -> None:
        with self._lock:
            self._contexts[chat_id] = CodexConversationContext(
                workspace=workspace,
                last_request=request,
                last_result=result[-6000:],
            )

    @staticmethod
    def _build_resolver(runner: CodexRunner) -> WorkspaceResolver | None:
        policy = getattr(runner, "policy", None)
        if policy is None:
            return None
        default_workspace = getattr(runner, "default_workspace", None)
        if default_workspace is not None:
            search_root = default_workspace.expanduser().resolve()
            if search_root.is_dir():
                return WorkspaceResolver(policy, search_roots=(search_root,))
        return WorkspaceResolver(policy)

    def _resolve_workspace(
        self, chat_id: str, prompt: str, explicit_workspace: str | None
    ) -> tuple[str | None, str | None]:
        if explicit_workspace:
            return explicit_workspace, None
        context_workspace = self._context_for(chat_id).workspace
        if self.resolver is None:
            return context_workspace, None
        fuzzy_markers = ("这个", "那个", "项目", "目录", "工作区", "skill")
        if not any(marker in prompt.lower() for marker in fuzzy_markers):
            return context_workspace, None
        candidates = self.resolver.find(prompt)
        if len(candidates) == 1:
            return str(candidates[0].path), None
        if not candidates:
            return None, "没有在允许的目录中找到匹配的 Codex 工作区，请补充项目目录。"
        choices = "\n".join(f"- {item.path}（{item.reason}）" for item in candidates)
        return None, f"找到多个可能的工作区，请明确选择后再执行：\n{choices}"

    @staticmethod
    def _looks_like_confirmation_token(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{16,}", value))

    @staticmethod
    def _format_result(result: CodexResult) -> str:
        parts = [
            (
                f"Codex {result.mode} 完成，目录：{result.workspace}"
                if result.returncode == 0
                else f"Codex {result.mode} 执行失败，目录：{result.workspace}"
            ),
            f"退出码：{result.returncode}",
        ]
        if result.stdout.strip():
            parts.append(f"输出：\n{result.stdout.strip()}")
        if result.returncode != 0 and result.stderr.strip():
            parts.append(f"错误输出：\n{result.stderr.strip()}")
        return "\n".join(parts)