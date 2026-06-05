from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    user_id: str
    chat_id: str
    text: str


@dataclass(frozen=True)
class OutgoingMessage:
    channel: str
    chat_id: str
    text: str
