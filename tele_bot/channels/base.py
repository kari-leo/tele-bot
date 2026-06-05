from __future__ import annotations

from typing import Protocol

from tele_bot.models import IncomingMessage, OutgoingMessage


class ChannelAdapter(Protocol):
    name: str

    def parse_incoming(self, payload: dict) -> IncomingMessage | None:
        ...

    def is_allowed(self, message: IncomingMessage) -> bool:
        ...

    def build_send_payload(self, message: OutgoingMessage) -> dict:
        ...
