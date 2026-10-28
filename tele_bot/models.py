from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    TEXT = "text"
    FILE = "file"
    IMAGE = "image"
    INTERACTIVE = "interactive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AttachmentDescriptor:
    """Channel-neutral metadata for an attachment; never contains file bytes."""

    kind: MessageType
    key: str
    name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    source_message_id: str | None = None


@dataclass(frozen=True)
class CardAction:
    """A server-defined interactive-card action."""

    action_id: str
    label: str
    value: str


@dataclass(frozen=True)
class InteractiveCard:
    """Restricted card contract; adapters decide the platform JSON shape."""

    title: str
    body: str
    status: str | None = None
    actions: tuple[CardAction, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    user_id: str
    chat_id: str
    text: str
    message_id: str | None = None
    message_type: MessageType = MessageType.TEXT
    attachments: tuple[AttachmentDescriptor, ...] = ()
    quote_target_message_id: str | None = None
    card_action: CardAction | None = None
    # Resolved by the message service; attachment descriptors remain metadata-only.
    image_data_urls: tuple[str, ...] = field(default=(), repr=False)
    attachment_context: str = field(default="", repr=False)
    # Supplied by the channel adapter, never inferred from message text.
    chat_type: str | None = None
    sender_ids: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class OutgoingMessage:
    channel: str
    chat_id: str
    text: str
    already_sent: bool = False
    reply_to_message_id: str | None = None
    card: InteractiveCard | None = None
    file_paths: tuple[str, ...] = ()
