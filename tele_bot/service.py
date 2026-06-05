from tele_bot.agent import AgentCore
from tele_bot.models import IncomingMessage, OutgoingMessage


class MessageService:
    def __init__(self, agent_core: AgentCore) -> None:
        self.agent_core = agent_core

    def handle(self, message: IncomingMessage) -> OutgoingMessage:
        return self.agent_core.handle_message(message)
