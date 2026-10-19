"""In-process MCP boundary for the authenticated Feishu schedule workflow."""

from __future__ import annotations

from mcp.server import MCPServer

from tele_bot.models import IncomingMessage
from tele_bot.scheduled.service import ScheduleService
from tele_bot.scheduled.proposals import ScheduleRequest


def create_schedule_server(service: ScheduleService, message: IncomingMessage, *,
                           context_messages=None) -> MCPServer:
    """Bind one trusted incoming message to a model-callable scheduling tool.

    Conversation references are injected by the graph. Model arguments are
    proposals only; identity and confirmation still use the incoming message.
    """
    server = MCPServer("tele-bot-schedule")

    @server.tool()
    def schedule_manage(request: ScheduleRequest | None = None) -> dict[str, str | bool]:
        """Handle the current user's scheduling request or follow-up.

        Call for requests to schedule, remind, run Codex later, list/change/cancel
        scheduled tasks, correct a previously rejected time, or confirm/authorize
        a pending scheduled task. Optionally supply a structured proposal with
        operation, task_id and tasks. It cannot confirm or authorize execution.
        Return the tool response verbatim to the user, including any token.
        """
        response = service.handle(message, context_messages=context_messages, request=request)
        if response is None:
            return {"handled": True, "text": "未找到可处理的定时请求；请说明触发时间和任务内容。"}
        return {"handled": True, "text": response.text}

    return server
