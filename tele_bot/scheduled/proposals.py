"""Model-visible scheduling fields; incomplete proposals are reparsed by service."""

from typing import Annotated, Any

from pydantic import Field
from typing_extensions import TypedDict


class ScheduleTaskProposal(TypedDict, total=False):
    content: Annotated[str, Field(description="任务目标、skill、输出要求；不填写在 request 顶层。")]
    at: Annotated[str, Field(description="首次触发时间，ISO8601 且带时区，例如 2026-10-07T03:30:00+08:00。")]
    rule: Annotated[dict[str, Any], Field(description="周期：kind=once/daily/weekly，interval、weekdays、count、until。")]
    mode: Annotated[str, Field(description="remind=仅提醒，auto=明确预授权自动执行，confirm=每次执行前确认。")]
    scope: Annotated[dict[str, Any], Field(description="授权范围：directories 为目录数组，tools 为工具数组。Codex 使用 codex_apply。")]
    missed_policy: Annotated[str, Field(description="错过触发策略：skip/run_once/ask。")]
    grace_seconds: int
    auth_seconds: int


class ScheduleRequest(TypedDict, total=False):
    operation: Annotated[str, Field(description="create/update/delete/list/get/history/clarify；不支持模型确认或授权。")]
    task_id: Annotated[str, Field(description="update/delete/get/history 的目标任务 ID，从当前对话中解析。")]
    tasks: Annotated[list[ScheduleTaskProposal], Field(
        description="create/update 使用非空数组，每项填写 content 和带时区的 at；不能只提供顶层 content。"
                    "不能完整确定时省略 request，由工具解析当前原始消息与会话。")]
    question: Annotated[str, Field(description="operation=clarify 时的具体澄清问题。")]


def is_complete_proposal(request: dict) -> bool:
    """Check required information, leaving dates, permissions and ownership to validation."""
    operation = request.get("operation")
    if operation in {"update", "delete", "get", "history"}:
        task_id = request.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return False
    if operation in {"create", "update"}:
        tasks = request.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return False
        return all(isinstance(task, dict) and all(
            isinstance(task.get(key), str) and task[key].strip() for key in ("content", "at"))
            for task in tasks)
    return operation in {"delete", "list", "get", "history", "clarify"}
