"""
ReactAgentExecutor — single-loop ReAct executor that replaces the Router +
7-static-graph path. Drop-in for ControlledAgentExecutor.handle().

Wires:
    IncomingMessage
        → conversation history loaded from state store
        → ReactGraph.invoke (LLM + tools loop, MAX_ITERATIONS bounded)
        → final AIMessage.content
        → filter_outbound_reply (strips ADVISER_DEGRADED marker + paraphrases)
        → AgentExecutionResult

Design choices:
- Same public surface as ControlledAgentExecutor — same handle() signature
  and same AgentExecutionResult fields. Allows side-by-side rollout via
  AgentCore configuration (Step C).
- `mode` and `model` fields are kept for backward compatibility but their
  semantics shift: `mode` is always "react"; `model` is the bound LLM's
  configured model id. report_path / tool_result_summary will be re-derived
  from trace in subsequent phases (Phase 1+ tools: write_report, knowledge_restore).
- chat_id / user_id are injected into RunnableConfig so future tools
  (GitTool's push approval) can read them via the @tool config parameter.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage

from tele_bot.agents.reply_filter import filter_outbound_reply
from tele_bot.models import IncomingMessage
from tele_bot.router import InMemoryConversationStateStore
from tele_bot.router.models import AgentMode


@dataclass(frozen=True)
class AgentExecutionResult:
    """Mirror of agents.executor.AgentExecutionResult — kept identical so the
    Telegram channel and tests can be reused across both executors."""

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
    ) -> None:
        """
        graph: a compiled LangGraph CompiledStateGraph (from build_react_graph()).
        model_name: model id for reporting in AgentExecutionResult.model.
        state_store: persistence; defaults to in-memory.
        """
        self.graph = graph
        self.model_name = model_name
        self.state_store = state_store or InMemoryConversationStateStore()

    def handle(self, message: IncomingMessage) -> AgentExecutionResult:
        self.state_store.set_mode(message.chat_id, AgentMode.CHAT)
        self.state_store.set_active_route(
            message.chat_id, skill_name="react", workflow_name="react_loop"
        )
        self.state_store.append_turn(
            message.chat_id, role="user", content=message.text
        )

        config = {
            "configurable": {
                "thread_id": message.chat_id,
                "chat_id": message.chat_id,
                "user_id": message.user_id,
            }
        }

        used_tool = False
        tool_result_summary: str | None = None
        try:
            result = self.graph.invoke(
                {"messages": [HumanMessage(content=message.text)], "iterations": 0},
                config=config,
            )
            messages = result.get("messages", [])
            final = self._final_text(messages)
            tool_calls_seen = self._count_tool_calls(messages)
            used_tool = tool_calls_seen > 0
            if used_tool:
                tool_result_summary = f"tools called: {tool_calls_seen}"
            reply_text = filter_outbound_reply(final) or "（无回复）"
        except Exception as exc:
            reply_text = f"处理失败：{exc}"

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
                return msg.content or ""
        if messages:
            return getattr(messages[-1], "content", "") or ""
        return ""

    @staticmethod
    def _count_tool_calls(messages: list) -> int:
        n = 0
        for msg in messages:
            if isinstance(msg, AIMessage):
                n += len(getattr(msg, "tool_calls", None) or [])
        return n
