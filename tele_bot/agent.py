from tele_bot.agents import ControlledAgentExecutor
from tele_bot.llm.alibailian import AliBailianChatClient
from tele_bot.models import IncomingMessage, OutgoingMessage


class AgentCore:
    """Platform-agnostic agent core.

    The current implementation is intentionally minimal so the Telegram
    message loop can be validated before any LLM integration is added.
    """

    def __init__(
        self,
        llm_client: AliBailianChatClient | None = None,
        executor: ControlledAgentExecutor | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.executor = executor

    def handle_message(self, message: IncomingMessage) -> OutgoingMessage:
        if self.executor is not None:
            result = self.executor.handle(message)
            reply_text = result.reply_text
        else:
            if self.llm_client is None:
                raise ValueError("llm_client is required")

            reply_text = self.llm_client.generate_reply(message.text)

        return OutgoingMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            text=reply_text,
        )

