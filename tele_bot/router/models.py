from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentMode(str, Enum):
    CHAT = "chat"
    MARKDOWN = "markdown"
    REASONING = "reasoning"


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    content: str


@dataclass
class ConversationState:
    chat_id: str
    mode: AgentMode = AgentMode.CHAT
    turns: list[ConversationTurn] = field(default_factory=list)
    last_tool_result_summary: str | None = None
    report_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RouterDecision:
    mode: AgentMode
    model: str
    allow_markdown: bool
    save_markdown: bool
    allow_tools: bool
    search_required: bool
    reason: str