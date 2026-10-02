from tele_bot.agents import ReactAgentExecutor
from tele_bot.models import IncomingMessage, OutgoingMessage


class AgentCore:
    """Platform-agnostic agent core that wraps ReactAgentExecutor."""

    def __init__(self, executor: ReactAgentExecutor | None = None, llm_client=None) -> None:
        if executor is None and llm_client is None:
            raise ValueError("executor or llm_client is required")
        self.executor = executor
        self.llm_client = llm_client

    def handle_message(self, message: IncomingMessage) -> OutgoingMessage:
        if self.executor is not None:
            result = self.executor.handle(message)
            reply_text = result.reply_text
            report_path = getattr(result, "report_path", None)
        else:
            reply_text = self.llm_client.generate_reply(message.text)
            report_path = None
        return OutgoingMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            text=reply_text,
            reply_to_message_id=message.message_id,
            file_paths=(report_path,) if report_path else (),
        )
