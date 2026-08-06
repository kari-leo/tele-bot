"""Feishu progress reporter.

Feishu clients are friendlier with a single visible progress hint than with
frequent message edits. This reporter sends "processing" once and ignores
step-level ReAct updates, avoiding fragile edit-message calls.
"""

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
                    return self._message_id
            _LOG.warning(
                "FeishuProgressReporter: sendMessage response missing message_id: %r",
                resp,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("FeishuProgressReporter: send_text failed: %s", exc)
        return None

    def update(self, text: str) -> bool:
        """Ignore per-step updates; final answers are sent as normal messages."""
        return False

    @property
    def message_id(self) -> Optional[str]:
        return self._message_id
