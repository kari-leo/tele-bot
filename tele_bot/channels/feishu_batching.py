"""Coalesce nearby webhook events and serialize turns per sender and chat."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from threading import Condition, Thread

from tele_bot.models import IncomingMessage, MessageType

_LOG = logging.getLogger(__name__)


def is_bare_attachment(message: IncomingMessage) -> bool:
    text = message.text.strip()
    return bool(message.attachments) and (
        not text or text == "[图片]" or
        (message.message_type is MessageType.FILE and text.startswith("[文件]"))
    )


def merge_messages(messages: list[IncomingMessage]) -> IncomingMessage:
    instructions = [message for message in messages if not is_bare_attachment(message)]
    base = instructions[-1] if instructions else messages[-1]
    attachments = {}
    for message in messages:
        for item in message.attachments:
            item = replace(item, source_message_id=item.source_message_id or message.message_id)
            attachments[(item.source_message_id, item.key)] = item
    return replace(base, text="\n".join(m.text for m in instructions) if instructions else base.text,
                   message_type=MessageType.TEXT if instructions else base.message_type,
                   attachments=tuple(attachments.values()))


class FeishuMessageBatcher:
    def __init__(self, callback, *, wait_seconds: float = 3.0, max_wait_seconds: float = 10.0):
        self.callback = callback
        self.wait_seconds, self.max_wait_seconds = wait_seconds, max_wait_seconds
        self._condition = Condition()
        self._queues: dict[tuple, list] = {}

    def submit(self, message: IncomingMessage):
        key = message.channel, message.chat_id, message.user_id
        with self._condition:
            start = key not in self._queues
            self._queues.setdefault(key, []).append((time.monotonic(), message))
            if start:
                Thread(target=self._run, args=(key,), daemon=True, name="feishu-turn").start()
            self._condition.notify_all()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._queues:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    @staticmethod
    def _immediate(message):
        return message.card_action is not None or message.text.lstrip().startswith("/")

    def _run(self, key):
        while True:
            with self._condition:
                queue = self._queues[key]
                if not queue:
                    del self._queues[key]
                    self._condition.notify_all()
                    return
                first_at, first = queue[0]
                count = 1
                if not self._immediate(first):
                    # Commands and distinct quoted replies form their own turns.
                    while count < len(queue):
                        candidate_at, candidate = queue[count]
                        if (self._immediate(candidate)
                                or candidate.quote_target_message_id != first.quote_target_message_id
                                or candidate_at - queue[count - 1][0] > self.wait_seconds
                                or candidate_at - first_at > self.max_wait_seconds):
                            break
                        count += 1
                    boundary = count < len(queue)
                    delay = min(queue[count - 1][0] + self.wait_seconds,
                                first_at + self.max_wait_seconds) - time.monotonic()
                    if not boundary and delay > 0:
                        self._condition.wait(delay)
                        continue
                batch = [message for _, message in queue[:count]]
                del queue[:count]
            try:
                self.callback(merge_messages(batch))
            except Exception:
                _LOG.exception("Feishu message batch failed")
