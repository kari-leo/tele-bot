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
import re

from tele_bot.agent import AgentCore
from tele_bot.agents.feishu_streaming import FeishuProgressReporter
from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.models import CardAction, IncomingMessage, InteractiveCard, OutgoingMessage
from tele_bot.router.codex_service import CodexCommandService
from tele_bot.router.knowledge_service import KnowledgeCommandService
from tele_bot.workflows.react_graph import PROGRESS_CONTEXTVAR


class MessageService:
    def __init__(
        self,
        agent_core: AgentCore,
        feishu_adapter: Optional[FeishuWebhookAdapter] = None,
        streaming_enabled: bool = False,
        codex_service: CodexCommandService | None = None,
        knowledge_service: KnowledgeCommandService | None = None,
    ) -> None:
        self.agent_core = agent_core
        self.feishu_adapter = feishu_adapter
        self.streaming_enabled = streaming_enabled
        self.codex_service = codex_service
        self.knowledge_service = knowledge_service

    def handle(self, message: IncomingMessage) -> OutgoingMessage:
        if self.knowledge_service is not None:
            knowledge_response = self.knowledge_service.handle(message)
            if knowledge_response is not None:
                return knowledge_response
        if self.codex_service is not None:
            confirmation = self.codex_service.handle_confirmation(message)
            if confirmation is not None:
                return OutgoingMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    text=confirmation,
                    reply_to_message_id=message.message_id,
                )
        if self.codex_service is not None and message.text.lstrip().lower().startswith("/codex"):
            response = self.codex_service.handle(message)
            if response is not None:
                token_match = re.search(
                    r'"confirmation_token"\s*:\s*"([A-Za-z0-9_-]+)"|/codex apply ([A-Za-z0-9_-]+)',
                    response,
                )
                token = (
                    next((value for value in token_match.groups() if value), None)
                    if token_match else None
                )
                return OutgoingMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    text=response,
                    reply_to_message_id=message.message_id,
                    card=(
                        InteractiveCard(
                            title="Codex 操作确认",
                            body=response[:3500],
                            status="pending",
                            actions=(
                                CardAction("codex_confirm", "确认", token),
                                CardAction("codex_cancel", "取消", token),
                            ),
                        )
                        if token else None
                    ),
                )
        if self._should_stream_feishu(message):
            reporter = FeishuProgressReporter(
                adapter=self.feishu_adapter,
                chat_id=message.chat_id,
            )
            reporter.start()
            token = PROGRESS_CONTEXTVAR.set(reporter.update)
            try:
                response = self.agent_core.handle_message(message)
                if reporter.finish(response.text):
                    for file_path in response.file_paths:
                        self.feishu_adapter.send_file(response.chat_id, file_path)
                    return OutgoingMessage(
                        channel=response.channel,
                        chat_id=response.chat_id,
                        text=response.text,
                        already_sent=True,
                        reply_to_message_id=response.reply_to_message_id,
                        file_paths=response.file_paths,
                    )
                return response
            finally:
                PROGRESS_CONTEXTVAR.reset(token)
        return self.agent_core.handle_message(message)

    def _should_stream_feishu(self, message: IncomingMessage) -> bool:
        return (
            self.streaming_enabled
            and self.feishu_adapter is not None
            and message.channel == self.feishu_adapter.name
        )
