"""Agent package exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tele_bot.agents.react_executor import AgentExecutionResult, ReactAgentExecutor

__all__ = ["AgentExecutionResult", "ReactAgentExecutor"]


def __getattr__(name: str):
    if name in __all__:
        from tele_bot.agents.react_executor import AgentExecutionResult, ReactAgentExecutor

        exports = {
            "AgentExecutionResult": AgentExecutionResult,
            "ReactAgentExecutor": ReactAgentExecutor,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
