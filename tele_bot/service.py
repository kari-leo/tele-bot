"""
MessageService — channel-agnostic entry that calls the agent core.

Phase 6 (D3/D4/D7): when `streaming_enabled` is True and a Telegram adapter is
provided, the service creates a `StreamingProgressReporter` per message,
publishes a placeholder, and installs the reporter into `PROGRESS_CONTEXTVAR`
so the ReAct graph can emit step-level updates. The contextvar is reset in a
`finally` block so concurrent messages stay isolated.

Legacy executor (D6) is unaffected: it never reads PROGRESS_CONTEXTVAR, and
the placeholder is only sent when streaming is enabled (controlled at
construction time by app.py).
"""

from __future__ import annotations

from typing import Optional

from tele_bot.agent import AgentCore
from tele_bot.agents.streaming import StreamingProgressReporter
from tele_bot.channels.telegram import TelegramAdapter
from tele_bot.models import IncomingMessage, OutgoingMessage
from tele_bot.workflows.react_graph import PROGRESS_CONTEXTVAR


class MessageService:
    def __init__(
        self,
        agent_core: AgentCore,
        telegram_adapter: Optional[TelegramAdapter] = None,
        streaming_enabled: bool = False,
    ) -> None:
        self.agent_core = agent_core
        self.telegram_adapter = telegram_adapter
        self.streaming_enabled = streaming_enabled

    def handle(self, message: IncomingMessage) -> OutgoingMessage:
        if self._should_stream(message):
            reporter = StreamingProgressReporter(
                adapter=self.telegram_adapter,
                chat_id=message.chat_id,
            )
            reporter.start()
            token = PROGRESS_CONTEXTVAR.set(reporter.update)
            try:
                return self.agent_core.handle_message(message)
            finally:
                PROGRESS_CONTEXTVAR.reset(token)
        return self.agent_core.handle_message(message)

    def _should_stream(self, message: IncomingMessage) -> bool:
        return (
            self.streaming_enabled
            and self.telegram_adapter is not None
            and message.channel == self.telegram_adapter.name
        )
