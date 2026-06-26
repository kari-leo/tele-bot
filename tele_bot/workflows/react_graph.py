"""
Phase 0-Spike: Single ReAct loop graph.

Replaces the 7 static per-workflow LangGraph DAGs.
The LLM decides which tools to call; the graph loops until no more tool_calls
or MAX_ITERATIONS is reached (in which case a user-visible termination message
is appended).

Phase 4 (additive): optional `on_progress` callback receives a short
human-readable status string at each ReAct step. Default is None (no
callbacks), preserving Phase 0/1/3 behavior exactly.

Phase 6 (additive, D3): `PROGRESS_CONTEXTVAR` lets callers inject a
per-invocation progress callback without rebuilding the graph. Resolution
order inside `_emit`:
    1. explicit `on_progress` parameter (Phase 4 path; takes precedence)
    2. `PROGRESS_CONTEXTVAR.get()` (Phase 6 path)
    3. no-op
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Annotated, Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

MAX_ITERATIONS = 10

MAX_ITERATIONS_MESSAGE = (
    "⚠️ 已达到最大循环次数 ({max_iter} 轮)，任务未能完成。"
    "请将请求拆分为更小的步骤后重试。"
)

ProgressCallback = Callable[[str], None]

# Phase 6 D3: per-invocation progress callback injection.
# Callers (typically MessageService) set this before invoking the graph and
# reset it in a `finally` block. ContextVar gives automatic isolation across
# threads (ThreadPoolExecutor copies the active Context per task), so
# concurrent chats do not cross-talk.
PROGRESS_CONTEXTVAR: ContextVar[Optional[ProgressCallback]] = ContextVar(
    "phase6_progress", default=None
)


class ReactState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    iterations: int


def build_react_graph(llm_with_tools, tools: list, checkpointer=None,
                      system_prompt: str = "",
                      on_progress: Optional[ProgressCallback] = None):
    """
    Build the ReAct loop graph.

    llm_with_tools: result of ChatOpenAI(...).bind_tools(tools)
    tools: list of @tool-decorated functions
    checkpointer: LangGraph checkpointer. **Default remains MemorySaver.**
        Pass `build_sqlite_saver(path)` from `tele_bot.persistence` to enable
        cross-restart persistence (Phase 4, opt-in only).
    system_prompt: optional skill descriptions / instructions prepended as a
        SystemMessage before the conversation messages on every LLM call.
    on_progress: optional Phase 4 callback invoked with a short status string
        at each ReAct step. Failures inside the callback are caught and
        logged (the agent loop must not be broken by progress reporting).
        Default None: no callbacks unless `PROGRESS_CONTEXTVAR` is set
        (Phase 6 D3, see module docstring).
    """
    _system_msg: list[SystemMessage] = (
        [SystemMessage(content=system_prompt)] if system_prompt else []
    )

    def _emit(text: str) -> None:
        cb: Optional[ProgressCallback] = on_progress
        if cb is None:
            cb = PROGRESS_CONTEXTVAR.get()
        if cb is None:
            return
        try:
            cb(text)
        except Exception:  # noqa: BLE001
            # Progress reporting must never break the agent loop.
            import logging
            logging.getLogger(__name__).warning(
                "progress callback raised", exc_info=True,
            )

    def call_model(state: ReactState, config: RunnableConfig) -> dict:
        iter_no = state.get("iterations", 0)
        if iter_no == 0:
            _emit("正在思考...")
        else:
            _emit(f"正在思考... (第 {iter_no + 1} 轮)")
        response = llm_with_tools.invoke(_system_msg + state["messages"], config=config)
        if getattr(response, "tool_calls", None):
            tool_names = [tc.get("name", "?") for tc in response.tool_calls]
            _emit("调用工具: " + ", ".join(tool_names))
        return {
            "messages": [response],
            "iterations": iter_no + 1,
        }

    def emit_max_iterations(state: ReactState) -> dict:
        """Append a user-visible AIMessage when MAX_ITERATIONS is exceeded."""
        return {
            "messages": [
                AIMessage(content=MAX_ITERATIONS_MESSAGE.format(max_iter=MAX_ITERATIONS))
            ],
        }

    def should_continue(state: ReactState) -> str:
        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            return "end"
        if state.get("iterations", 0) >= MAX_ITERATIONS:
            return "max_iter"
        return "continue"

    tool_node = ToolNode(tools)

    graph = StateGraph(ReactState)
    graph.add_node("call_model", call_model)
    graph.add_node("call_tools", tool_node)
    graph.add_node("max_iter_exit", emit_max_iterations)

    graph.add_edge(START, "call_model")
    graph.add_conditional_edges(
        "call_model",
        should_continue,
        {
            "continue": "call_tools",
            "end": END,
            "max_iter": "max_iter_exit",
        },
    )
    graph.add_edge("call_tools", "call_model")
    graph.add_edge("max_iter_exit", END)

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return graph.compile(checkpointer=cp)
