from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentMode(str, Enum):
    CHAT = "chat"
    MARKDOWN = "markdown"
    REASONING = "reasoning"


class Capability(str, Enum):
    SEARCH_WEB = "search_web"
    WRITE_REPORT = "write_report"
    RESTORE_KNOWLEDGE = "restore_knowledge"
    MULTI_STEP_PLANNING = "multi_step_planning"
    TELEGRAM_REPLY = "telegram_reply"
    READ_FILESYSTEM = "read_filesystem"
    SEARCH_FILESYSTEM = "search_filesystem"
    EXECUTE_SHELL_SANDBOX = "execute_shell_sandbox"


class WorkflowName(str, Enum):
    CHAT_REPLY = "chat_reply"
    MARKDOWN_REPLY = "markdown_reply"
    REASONING_REPLY = "reasoning_reply"
    SEARCH_REPORT = "search_report"
    RESTORE_CHEAP = "restore_cheap"
    FILESYSTEM_INSPECT = "filesystem_inspect"
    SHELL_INSPECT = "shell_inspect"


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    content: str


@dataclass
class ConversationState:
    chat_id: str
    mode: AgentMode = AgentMode.CHAT
    active_skill: str = "chat"
    active_workflow: str = WorkflowName.CHAT_REPLY.value
    turns: list[ConversationTurn] = field(default_factory=list)
    last_tool_result_summary: str | None = None
    report_paths: list[str] = field(default_factory=list)
    workflow_trace: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    default_workflow: WorkflowName
    mode: AgentMode
    capabilities: tuple[Capability, ...]
    allow_markdown: bool = False
    save_markdown: bool = False


@dataclass(frozen=True)
class RouterDecision:
    skill_name: str
    workflow_name: str
    mode: AgentMode
    model: str
    allow_markdown: bool
    save_markdown: bool
    required_capabilities: tuple[Capability, ...]
    reason: str

    @property
    def allow_tools(self) -> bool:
        return Capability.SEARCH_WEB in self.required_capabilities

    @property
    def search_required(self) -> bool:
        return Capability.SEARCH_WEB in self.required_capabilities