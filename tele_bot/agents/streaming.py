"""
StreamingProgressReporter — periodic Telegram editMessage progress updates.

Phase 4 scope. Sends a placeholder message at start, then edits it as the
ReAct loop advances. Throttled at >= 1.1s per edit (Telegram limits ~1/sec
per chat, with 0.1s safety margin).

Failure handling:
- editMessage HTTP failure: log WARN, continue. The final reply still goes
  via sendMessage in the normal flow, so progress loss is non-fatal.
- The reporter never raises into the ReAct loop.

Not multi-thread safe; one instance per chat-conversation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from tele_bot.channels.telegram import TelegramAdapter
from tele_bot.models import OutgoingMessage

_LOG = logging.getLogger(__name__)

PLACEHOLDER_TEXT = "正在处理..."
EDIT_THROTTLE_SECONDS = 1.1


@dataclass
class StreamingProgressReporter:
    adapter: TelegramAdapter
    chat_id: str
    placeholder_text: str = PLACEHOLDER_TEXT
    throttle_seconds: float = EDIT_THROTTLE_SECONDS
    _message_id: Optional[int] = field(default=None, init=False)
    _last_edit_at: float = field(default=0.0, init=False)
    _last_text: str = field(default="", init=False)

    def start(self) -> Optional[int]:
        """Send the placeholder message and remember its message_id.

        Returns the message_id, or None if sendMessage failed (in which case
        subsequent update() calls become no-ops).
        """
        try:
            resp = self.adapter.send_text(
                OutgoingMessage(channel=self.adapter.name,
                                chat_id=self.chat_id,
                                text=self.placeholder_text)
            )
            mid = resp.get("result", {}).get("message_id")
            if isinstance(mid, int):
                self._message_id = mid
                self._last_text = self.placeholder_text
                return mid
            _LOG.warning(
                "StreamingProgressReporter: sendMessage response missing message_id: %r",
                resp,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("StreamingProgressReporter: send_text failed: %s", exc)
        return None

    def update(self, text: str) -> bool:
        """Edit the placeholder with new progress text.

        Throttled. Returns True if an edit was actually performed, False if
        skipped (no message yet, throttled, identical text, or HTTP error).

        Failed edit attempts also consume the throttle window: if the HTTP
        call raises, _last_edit_at is still advanced so the next update()
        call within `throttle_seconds` is skipped. This prevents a runaway
        burst of editMessage requests while Telegram is returning errors
        (e.g. 429 backoff loop or transient outage).
        """
        if self._message_id is None:
            return False
        if not text:
            return False
        if text == self._last_text:
            return False
        now = time.monotonic()
        if now - self._last_edit_at < self.throttle_seconds:
            return False
        # Reserve the window BEFORE making the call. Whether it succeeds or
        # raises, the next attempt waits at least throttle_seconds.
        self._last_edit_at = now
        try:
            self.adapter.edit_message(
                chat_id=self.chat_id,
                message_id=self._message_id,
                text=text,
            )
            self._last_text = text
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "StreamingProgressReporter: edit_message failed (chat=%s msg=%s): %s",
                self.chat_id, self._message_id, exc,
            )
            return False

    @property
    def message_id(self) -> Optional[int]:
        return self._message_id
