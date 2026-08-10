"""Feishu progress reporter using one editable, temporary status message."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from tele_bot.channels.feishu import FeishuWebhookAdapter
from tele_bot.models import OutgoingMessage

_LOG = logging.getLogger(__name__)

PLACEHOLDER_TEXT = "正在处理，请稍候..."


@dataclass
class FeishuProgressReporter:
    adapter: FeishuWebhookAdapter
    chat_id: str
    placeholder_text: str = PLACEHOLDER_TEXT
    _message_id: Optional[str] = field(default=None, init=False)
    _last_text: Optional[str] = field(default=None, init=False)

    def start(self) -> Optional[str]:
        """Send one placeholder message and remember its message_id if present."""
        try:
            resp = self.adapter.send_text(
                OutgoingMessage(
                    channel=self.adapter.name,
                    chat_id=self.chat_id,
                    text=self.placeholder_text,
                )
            )
            if resp.get("code") == 0:
                message_id = resp.get("data", {}).get("message_id")
                if message_id:
                    self._message_id = str(message_id)
                    self._last_text = self.placeholder_text
                    return self._message_id
            _LOG.warning(
                "FeishuProgressReporter: sendMessage response missing message_id: %r",
                resp,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("FeishuProgressReporter: send_text failed: %s", exc)
        return None

    def update(self, text: str) -> bool:
        """Edit the temporary status message in place."""
        if not self._message_id or not text.strip() or text == self._last_text:
            return False
        try:
            response = self.adapter.edit_message(self._message_id, text)
            if response.get("code") == 0:
                self._last_text = text
                return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("FeishuProgressReporter: edit_message failed: %s", exc)
        return False

    def finish(self, text: str) -> bool:
        """Turn the temporary status message into the final reply in place."""
        if not self._message_id:
            return False
        return self.update(text)

    @property
    def message_id(self) -> Optional[str]:
        return self._message_id
