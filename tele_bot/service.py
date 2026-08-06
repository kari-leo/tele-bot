"""
MessageService — channel-agnostic entry that calls the agent core.

Phase 6 (D3/D4/D7): when `streaming_enabled` is True and a Feishu adapter is
provided, the service creates a progress reporter per message,
publishes a placeholder, and installs the reporter into `PROGRESS_CONTEXTVAR`
so the ReAct graph can emit step-level updates. The contextvar is reset in a
`finally` block so concurrent messages stay isolated.

Supports Feishu streaming progress updates.
"""

from __future__ import annotations

from typing import Optional

from tele_bot.agent import AgentCore
from tele_bot.agents.feishu_streaming import FeishuProgressReporter
from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.models import IncomingMessage, OutgoingMessage
from tele_bot.workflows.react_graph import PROGRESS_CONTEXTVAR


class MessageService:
    def __init__(
        self,
        agent_core: AgentCore,
        feishu_adapter: Optional[FeishuWebhookAdapter] = None,
        streaming_enabled: bool = False,
    ) -> None:
        self.agent_core = agent_core
        self.feishu_adapter = feishu_adapter
        self.streaming_enabled = streaming_enabled

    def handle(self, message: IncomingMessage) -> OutgoingMessage:
        if self._should_stream_feishu(message):
            reporter = FeishuProgressReporter(
                adapter=self.feishu_adapter,
                chat_id=message.chat_id,
            )
            reporter.start()
            token = PROGRESS_CONTEXTVAR.set(reporter.update)
            try:
                return self.agent_core.handle_message(message)
            finally:
                PROGRESS_CONTEXTVAR.reset(token)
        return self.agent_core.handle_message(message)

    def _should_stream_feishu(self, message: IncomingMessage) -> bool:
        return (
            self.streaming_enabled
            and self.feishu_adapter is not None
            and message.channel == self.feishu_adapter.name
        )
