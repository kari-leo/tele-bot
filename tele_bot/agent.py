from tele_bot.agents import ReactAgentExecutor
from tele_bot.models import IncomingMessage, OutgoingMessage


class AgentCore:
    """Platform-agnostic agent core that wraps ReactAgentExecutor."""

    def __init__(self, executor: ReactAgentExecutor) -> None:
        self.executor = executor

    def handle_message(self, message: IncomingMessage) -> OutgoingMessage:
        result = self.executor.handle(message)
        return OutgoingMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            text=result.reply_text,
        )

