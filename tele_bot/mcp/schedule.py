"""In-process MCP boundary for the authenticated Feishu schedule workflow."""

from __future__ import annotations

from mcp.server import MCPServer

from tele_bot.models import IncomingMessage
from tele_bot.scheduled.service import ScheduleService


def create_schedule_server(service: ScheduleService, message: IncomingMessage) -> MCPServer:
    """Bind one trusted incoming message to a model-callable scheduling tool.

    The model supplies no arguments. In particular, it cannot invent a user,
    chat, confirmation token, or replacement message text for this call.
    """
    server = MCPServer("tele-bot-schedule")

    @server.tool()
    def schedule_manage() -> dict[str, str | bool]:
        """Handle the current user's scheduling request or follow-up.

        Call for requests to schedule, remind, run Codex later, list/change/cancel
        scheduled tasks, correct a previously rejected time, or confirm/authorize
        a pending scheduled task. The current authenticated message is used as-is.
        Return the tool response verbatim to the user, including any token.
        """
        response = service.handle(message)
        if response is None:
            return {"handled": True, "text": "未找到可处理的定时请求；请说明触发时间和任务内容。"}
        return {"handled": True, "text": response.text}

    return server
