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
import json
from typing import Annotated, Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict
from tele_bot.workflows.context_manager import ContextManager
from tele_bot.agents.task_results import incomplete_reply, message_text, publication_receipts

MAX_ITERATIONS = 15
MAX_EMPTY_RETRIES = 1
RECOVERY_INSTRUCTION = (
    "请以当前用户请求为准，继续尚未完成的必要步骤，并返回非空回复。"
    "逐一核对当前请求要求的全部文档；历史成功记录不证明文件现在仍然存在。"
    "不要把仅处理一份说成全部完成，也不要重复已在本轮确认完成的操作。"
    "如果提供 blog_publish_files，多份源文档应作为完整批次提交，已有正文会复用。"
    "最后根据本轮工具结果明确说明已完成和未完成部分；无法继续时如实说明原因。"
)
EMPTY_RESPONSE_INSTRUCTION = "上一轮模型没有返回有效内容。" + RECOVERY_INSTRUCTION
INCOMPLETE_RESPONSE_INSTRUCTION = "本轮工具记录只确认部分文档已推送，不能结束为全部完成。" + RECOVERY_INSTRUCTION

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
    llm_messages: list[BaseMessage]
    tool_context_messages: list[BaseMessage]
    context_summary: str
    summary_until_message_id: str | None
    context_refs: list[dict]
    token_scale: float
    context_stats: dict
    empty_retries: int
    empty_recovery_pending: bool
    incomplete_retries: int


def build_react_graph(llm_with_tools, tools: list, checkpointer=None,
                      system_prompt: str = "",
                      on_progress: Optional[ProgressCallback] = None,
                      context_manager: ContextManager | None = None,
                      skill_loader=None):
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
    manager = context_manager or ContextManager.for_tools(tools)

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

    def prepare_context(state: ReactState) -> dict:
        # This node is checkpointed BEFORE the API request, including on timeout.
        new_request = state.get("iterations", 0) == 0
        recovering = state.get("empty_recovery_pending", False) and not new_request
        instruction = (INCOMPLETE_RESPONSE_INSTRUCTION if message_text(state["messages"][-1].content)
                       else EMPTY_RESPONSE_INSTRUCTION)
        prompt = system_prompt + ("\n\n" + instruction if recovering else "")
        messages = state["messages"]
        if skill_loader is not None:
            messages, prompt = skill_loader.prepare_request(messages, prompt)
        prepared = manager.prepare(
            messages, system_prompt=prompt,
            summary=state.get("context_summary", ""),
            cursor=state.get("summary_until_message_id"),
            refs=state.get("context_refs", []), token_scale=state.get("token_scale", 1.0),
            on_progress=_emit)
        if new_request:
            prepared.update(empty_retries=0, empty_recovery_pending=False, incomplete_retries=0)
        return prepared

    def call_model(state: ReactState, config: RunnableConfig) -> dict:
        iter_no = state.get("iterations", 0)
        if iter_no == 0:
            _emit("正在思考...")
        else:
            _emit(f"正在思考... (第 {iter_no + 1} 轮)")
        response = llm_with_tools.invoke(state["llm_messages"], config=config)
        usage = getattr(response, "usage_metadata", None) or {}
        metadata = getattr(response, "response_metadata", None) or {}
        observed = usage.get("input_tokens") or metadata.get("token_usage", {}).get("prompt_tokens")
        scale = state.get("token_scale", 1.0)
        if observed:
            scale = manager.calibrate(scale, state["context_stats"]["unscaled_tokens"], observed)
        if getattr(response, "tool_calls", None):
            tool_names = [tc.get("name", "?") for tc in response.tool_calls]
            _emit("调用工具: " + ", ".join(tool_names))
        return {
            "messages": [response],
            "iterations": iter_no + 1,
            "token_scale": scale,
            "llm_messages": [],
            "tool_context_messages": (state["llm_messages"] + [response]
                                      if any(tc.get("name") == "schedule_manage"
                                             for tc in getattr(response, "tool_calls", []) or []) else []),
            "empty_recovery_pending": False,
        }

    def retry_empty_response(state: ReactState) -> dict:
        _emit("模型未返回有效内容，正在恢复处理并重新生成回复...")
        return {"empty_retries": state.get("empty_retries", 0) + 1,
                "empty_recovery_pending": True}

    def emit_empty_response_exit(state: ReactState) -> dict:
        _emit("模型仍未返回有效内容，正在整理本轮执行结果...")
        return {"messages": [AIMessage(content=incomplete_reply(state["messages"]))]}

    def retry_incomplete_publication(state: ReactState) -> dict:
        receipts = publication_receipts(state["messages"])
        _emit(f"文档尚未全部推送（{len(receipts['pushed'])}/{receipts['expected']}），正在继续处理...")
        return {"incomplete_retries": state.get("incomplete_retries", 0) + 1,
                "empty_recovery_pending": True}

    def emit_incomplete_publication(state: ReactState) -> dict:
        return {"messages": [AIMessage(content=incomplete_reply(
            state["messages"], reason="工具记录未确认所有要求的文档已推送"))]}

    def emit_max_iterations(state: ReactState) -> dict:
        """Append a user-visible AIMessage when MAX_ITERATIONS is exceeded."""
        return {
            "messages": [
                AIMessage(content=MAX_ITERATIONS_MESSAGE.format(max_iter=MAX_ITERATIONS))
            ],
        }

    def should_continue(state: ReactState, config: RunnableConfig) -> str:
        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            if not message_text(last.content):
                if (state.get("empty_retries", 0) < MAX_EMPTY_RETRIES
                        and state.get("iterations", 0) < MAX_ITERATIONS):
                    return "retry_empty"
                return "empty_exit"
            receipts = publication_receipts(state["messages"])
            if (receipts["has_publication"] and receipts["expected"]
                    and len(receipts["pushed"]) < receipts["expected"]):
                from tele_bot.tools.lc_adapters import _blog_push_authorized
                if _blog_push_authorized(config):
                    if (state.get("incomplete_retries", 0) < 1 and state.get("iterations", 0) < MAX_ITERATIONS
                            and not receipts["last_failed"]):
                        return "retry_incomplete"
                    return "incomplete_exit"
            return "end"
        if state.get("iterations", 0) >= MAX_ITERATIONS:
            return "max_iter"
        return "continue"

    def schedule_reply(state: ReactState) -> str | None:
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and last.name == "schedule_manage" and last.status != "error":
            call = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
            if call is not None and len(call.tool_calls) == 1:
                try:
                    value = json.loads(last.content)
                    if value.get("handled") and isinstance(value.get("text"), str):
                        return value["text"]
                except (ValueError, TypeError, AttributeError):
                    pass
        return None

    def after_tools(state: ReactState) -> str:
        return "schedule_exit" if schedule_reply(state) is not None else "continue"

    def emit_schedule_reply(state: ReactState) -> dict:
        return {"messages": [AIMessage(content=schedule_reply(state))], "tool_context_messages": []}

    tool_node = ToolNode(tools)

    graph = StateGraph(ReactState)
    graph.add_node("prepare_context", prepare_context)
    graph.add_node("call_model", call_model)
    graph.add_node("call_tools", tool_node)
    graph.add_node("schedule_exit", emit_schedule_reply)
    graph.add_node("max_iter_exit", emit_max_iterations)
    graph.add_node("retry_empty_response", retry_empty_response)
    graph.add_node("empty_response_exit", emit_empty_response_exit)
    graph.add_node("retry_incomplete_publication", retry_incomplete_publication)
    graph.add_node("incomplete_publication_exit", emit_incomplete_publication)

    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "call_model")
    graph.add_conditional_edges(
        "call_model",
        should_continue,
        {
            "continue": "call_tools",
            "end": END,
            "max_iter": "max_iter_exit",
            "retry_empty": "retry_empty_response",
            "empty_exit": "empty_response_exit",
            "retry_incomplete": "retry_incomplete_publication",
            "incomplete_exit": "incomplete_publication_exit",
        },
    )
    graph.add_conditional_edges("call_tools", after_tools,
                                {"continue": "prepare_context", "schedule_exit": "schedule_exit"})
    graph.add_edge("schedule_exit", END)
    graph.add_edge("max_iter_exit", END)
    graph.add_edge("retry_empty_response", "prepare_context")
    graph.add_edge("empty_response_exit", END)
    graph.add_edge("retry_incomplete_publication", "prepare_context")
    graph.add_edge("incomplete_publication_exit", END)

    cp = checkpointer if checkpointer is not None else MemorySaver()
    # Each round visits prepare/model/tools; leave room for the exit node.
    # LangGraph's default 25-step budget would stop a 15-round task early.
    return graph.compile(checkpointer=cp).with_config(recursion_limit=3 * MAX_ITERATIONS + 2)
