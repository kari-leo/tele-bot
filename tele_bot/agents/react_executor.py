"""
ReactAgentExecutor — single-loop ReAct executor with LangGraph.

Wires:
    IncomingMessage
        → conversation history loaded from state store
        → ReactGraph.invoke (LLM + tools loop, MAX_ITERATIONS bounded)
        → final AIMessage.content
        → filter_outbound_reply (strips ADVISER_DEGRADED marker + paraphrases)
        → AgentExecutionResult

Design choices:
- `mode` is always "react"; `model` is the bound LLM's configured model id.
- report_path / tool_result_summary will be re-derived from trace in
  subsequent phases (Phase 1+ tools: write_report, knowledge_restore).
- chat_id / user_id are injected into RunnableConfig so future tools
  (GitTool's push approval) can read them via the @tool config parameter.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tele_bot.agents.reply_filter import filter_outbound_reply
from tele_bot.agents.task_results import current_turn, incomplete_reply, message_text
from tele_bot.models import IncomingMessage
from tele_bot.router import InMemoryConversationStateStore
from tele_bot.router.models import AgentMode
from tele_bot.tools.lc_adapters import (
    CURRENT_CHAT_ID, CURRENT_SCHEDULE_ALLOWED, CURRENT_SCHEDULE_MESSAGE,
)
from tele_bot.tools.knowledge_search import CURRENT_KNOWLEDGE_MESSAGE


@dataclass(frozen=True)
class AgentExecutionResult:
    """Result returned by the ReAct executor."""

    reply_text: str
    mode: str
    model: str
    report_path: str | None = None
    tool_result_summary: str | None = None
    used_tool: bool = False


class ReactAgentExecutor:
    def __init__(
        self,
        *,
        graph,
        model_name: str,
        state_store: InMemoryConversationStateStore | None = None,
        knowledge_store=None,
    ) -> None:
        """
        graph: a compiled LangGraph CompiledStateGraph (from build_react_graph()).
        model_name: model id for reporting in AgentExecutionResult.model.
        state_store: persistence; defaults to in-memory.
        """
        self.graph = graph
        self.model_name = model_name
        self.state_store = state_store or InMemoryConversationStateStore()
        self.knowledge_store = knowledge_store

    def handle(self, message: IncomingMessage) -> AgentExecutionResult:
        self.state_store.set_mode(message.chat_id, AgentMode.CHAT)
        self.state_store.set_active_route(
            message.chat_id, skill_name="react", workflow_name="react_loop"
        )
        self.state_store.append_turn(
            message.chat_id, role="user", content=message.text
        )

        thread_id = message.chat_id
        if self.knowledge_store is not None:
            authenticated = CURRENT_KNOWLEDGE_MESSAGE.get()
            scope = "untrusted"
            if authenticated is not None:
                try:
                    scope = self.knowledge_store.require_member(message.chat_id, message.user_id).id
                except PermissionError:
                    scope = "no-membership"
            # Prevent old knowledge results/answers leaking via chat history after
            # rebinding a conversation or switching users (including the raw API).
            identity = json.dumps([message.chat_id, message.user_id, scope])
            thread_id = "scoped-" + hashlib.sha256(identity.encode()).hexdigest()
        config = {
            "configurable": {
                "thread_id": thread_id,
                "chat_id": message.chat_id,
                "user_id": message.user_id,
                "channel": message.channel,
                "message_text": message.text,
                "schedule_allowed": CURRENT_SCHEDULE_ALLOWED.get(),
            }
        }

        used_tool = False
        tool_result_summary: str | None = None
        chat_id_token = CURRENT_CHAT_ID.set(message.chat_id)
        schedule_message_token = CURRENT_SCHEDULE_MESSAGE.set(message)
        request = HumanMessage(content=message.text, id=str(uuid4()))
        messages = []
        try:
            result = self.graph.invoke(
                {"messages": [request], "iterations": 0},
                config=config,
            )
            messages = current_turn(result.get("messages", []), request.id)
            final = self._final_text(messages)
            schedule_reply = self._schedule_tool_reply(messages, message.text)
            if schedule_reply is not None:
                final = schedule_reply
            tool_calls_seen = self._count_tool_calls(messages)
            used_tool = tool_calls_seen > 0
            if used_tool:
                tool_result_summary = f"tools called: {tool_calls_seen}"
            reply_text = filter_outbound_reply(final) or incomplete_reply(messages)
        except Exception as exc:
            try:
                messages = current_turn(self.graph.get_state(config).values.get("messages", []), request.id)
            except Exception:
                pass
            reply_text = incomplete_reply(messages, reason=f"处理失败：{exc}")
            tool_calls_seen = self._count_tool_calls(messages)
            used_tool = tool_calls_seen > 0
            if used_tool:
                tool_result_summary = f"tools called: {tool_calls_seen}"
        finally:
            CURRENT_SCHEDULE_MESSAGE.reset(schedule_message_token)
            CURRENT_CHAT_ID.reset(chat_id_token)

        self.state_store.append_turn(
            message.chat_id, role="assistant", content=reply_text
        )
        return AgentExecutionResult(
            reply_text=reply_text,
            mode="react",
            model=self.model_name,
            report_path=None,
            tool_result_summary=tool_result_summary,
            used_tool=used_tool,
        )

    @staticmethod
    def _final_text(messages: list) -> str:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                return message_text(msg.content)
        return ""

    @staticmethod
    def _count_tool_calls(messages: list) -> int:
        n = 0
        for msg in messages:
            if isinstance(msg, AIMessage):
                n += len(getattr(msg, "tool_calls", None) or [])
        return n

    @staticmethod
    def _schedule_tool_reply(messages: list, request_text: str) -> str | None:
        """Keep confirmation tokens and scheduler errors exactly as MCP returned."""
        for item in reversed(messages):
            if isinstance(item, HumanMessage) and item.content == request_text:
                break
            if isinstance(item, ToolMessage) and item.name == "schedule_manage":
                try:
                    result = json.loads(item.content)
                except (TypeError, ValueError):
                    continue
                if result.get("handled"):
                    return str(result["text"])
        return None
