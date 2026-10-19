"""Scheduled delivery includes persisted attachments, not just text."""

from tele_bot.models import OutgoingMessage


def send_scheduled_message(adapter, outgoing: OutgoingMessage) -> dict:
    result = {"code": 0}
    if outgoing.text:
        result = adapter.send_text(outgoing)
        if result.get("code") not in (None, 0):
            return result
    for path in outgoing.file_paths:
        result = adapter.send_file(outgoing.chat_id, path)
        if result.get("code") not in (None, 0):
            return result
    return result
