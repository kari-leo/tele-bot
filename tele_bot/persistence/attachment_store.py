"""Private attachment inbox, independent of knowledge spaces and model history."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock

from tele_bot.models import AttachmentDescriptor, IncomingMessage, MessageType


class AttachmentStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("""CREATE TABLE IF NOT EXISTS attachment_messages (
                channel TEXT NOT NULL, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                message_id TEXT NOT NULL, attachments TEXT NOT NULL,
                received_at REAL NOT NULL, pending INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(channel,chat_id,user_id,message_id))""")

    @staticmethod
    def scope(message):
        return message.channel, message.chat_id, message.user_id

    def remember(self, message: IncomingMessage) -> None:
        # Each merged attachment keeps the resource's original message ID.
        grouped: dict[str, list] = {}
        for item in message.attachments:
            source = item.source_message_id or message.message_id
            if source:
                grouped.setdefault(source, []).append(asdict(replace(item, source_message_id=source)))
        with self._lock, self._conn:
            for source, items in grouped.items():
                self._conn.execute("""INSERT OR IGNORE INTO attachment_messages
                    (channel,chat_id,user_id,message_id,attachments,received_at)
                    VALUES(?,?,?,?,?,?)""", (*self.scope(message), source,
                                             json.dumps(items, ensure_ascii=False), time.time()))

    def find(self, message: IncomingMessage, message_id: str | None = None):
        with self._lock:
            if message_id:
                rows = self._conn.execute("""SELECT attachments FROM attachment_messages
                    WHERE channel=? AND chat_id=? AND user_id=? AND message_id=?""",
                    (*self.scope(message), message_id)).fetchall()
            else:
                # Old uploads remain available by explicit quote; only recent ones
                # are implicitly associated with the next instruction.
                rows = self._conn.execute("""SELECT attachments FROM attachment_messages
                    WHERE channel=? AND chat_id=? AND user_id=? AND pending=1 AND received_at>?
                    ORDER BY received_at""", (*self.scope(message), time.time() - 24 * 3600)).fetchall()
        return tuple(AttachmentDescriptor(**{**item, "kind": MessageType(item["kind"])})
                     for row in rows for item in json.loads(row["attachments"]))

    def consume(self, message: IncomingMessage) -> None:
        with self._lock, self._conn:
            for item in message.attachments:
                self._conn.execute("""UPDATE attachment_messages SET pending=0
                    WHERE channel=? AND chat_id=? AND user_id=? AND message_id=?""",
                    (*self.scope(message), item.source_message_id or message.message_id))

    def close(self):
        with self._lock:
            self._conn.close()
