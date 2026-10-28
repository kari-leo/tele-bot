"""Server-owned, bounded write requests that can survive clarification turns.

Conversation context can authorize preparing a preview, never a remote write.
Only the original request's live reads can establish its target/record scope.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import re
import time
import uuid

from tele_bot.bitable.models import canonical, digest


WRITE_INTENT = {
    "create": re.compile(r"新增|增加|添加|增添|创建|写入|录入|\b(create|add|insert)\b", re.I),
    "update": re.compile(r"修改|更改|更新|换成|改成|改为|改到|改一下|改下|设为|设成|设置|定在|定为|调到|调整|标记|标为|标成|挪到|延期到|延后到|提前到|清空|\b(update|change|set|clear)\b", re.I),
    "delete": re.compile(r"删除|移除|\b(delete|remove)\b", re.I),
}
BLOCKED = re.compile(
    r"取消|算了|不用了|不要|禁止|不许|别(?:再|去|给|帮)?(?:新增|增加|添加|增添|创建|录入|删除|修改|更新|写入|更改|换成|改|设|标记|清空|移除|调整)"
    r"|\b(cancel|do not|don't|never)\b", re.I)
QUESTION = re.compile(r"\s*(?:请问)?(?:如何|怎么|为什么|假如|如果)")


def write_actions(text):
    # Values supplied during clarification are data, including URLs whose path
    # happens to contain words such as /update/ or /delete/.
    text = re.sub(r"https?://\S+", "", text)
    if BLOCKED.search(text) or QUESTION.match(text) or text.lstrip().startswith("/"):
        return []
    return [action for action, pattern in WRITE_INTENT.items() if pattern.search(text)]


@dataclass
class IntentTurn:
    key: str
    message: object
    grant: dict | None
    touched: bool = False
    scopes: dict = field(default_factory=dict)


CURRENT_INTENT = ContextVar("bitable_intent_turn", default=None)


class ConversationIntents:
    def __init__(self, service):
        self.service = service

    @contextmanager
    def turn(self, message, *, trusted):
        store = self.service.store
        if not trusted or store is None or not message.message_id:
            token = CURRENT_INTENT.set(None)
            try:
                yield
            finally:
                CURRENT_INTENT.reset(token)
            return
        key = digest([message.channel, message.user_id, message.chat_id])
        # Serialize a user's conversation across processes, including preparation
        # and consumption. Table leases alone do not protect multiple targets.
        with store.lease("conversation-intent:" + key):
            now = time.time()
            actions = write_actions(message.text)
            with store.connect() as db:
                db.execute("DELETE FROM conversation_intents WHERE expires<=?", (now,))
                row = db.execute("SELECT * FROM conversation_intents WHERE identity=?", (key,)).fetchone()
                grant = dict(row) if row else None
                if grant and grant["policy"] != self.service.version:
                    grant = None
                    db.execute("DELETE FROM conversation_intents WHERE identity=?", (key,))
                if BLOCKED.search(message.text) or QUESTION.match(message.text) or message.text.lstrip().startswith("/"):
                    db.execute("DELETE FROM conversation_intents WHERE identity=?", (key,))
                    grant = None
                elif actions and (grant is None or grant["source_message"] != message.message_id):
                    fresh = db.execute("INSERT OR IGNORE INTO conversation_intent_messages VALUES (?,?)",
                                       (key, message.message_id)).rowcount
                    grant = None
                    if fresh:
                        grant = dict(identity=key, id=str(uuid.uuid4()), policy=self.service.version,
                                     source_message=message.message_id, source_text=message.text,
                                     actions=canonical(actions), scopes="{}", state="active",
                                     expires=now + self.service.settings.confirm_ttl)
                        db.execute("INSERT OR REPLACE INTO conversation_intents "
                                   "(identity,id,policy,source_message,source_text,actions,scopes,state,expires) "
                                   "VALUES (:identity,:id,:policy,:source_message,:source_text,:actions,:scopes,:state,:expires)", grant)
            turn = IntentTurn(key, message, grant, scopes=json.loads(grant["scopes"]) if grant else {})
            token = CURRENT_INTENT.set(turn)
            try:
                yield
            except BaseException:
                self.close(turn)
                raise
            finally:
                try:
                    # A normal chat turn without table activity ends the task.
                    # Expiry is fixed at the initial request, never sliding.
                    if not turn.touched or not turn.scopes:
                        self.close(turn)
                finally:
                    CURRENT_INTENT.reset(token)

    def current(self, message):
        turn = CURRENT_INTENT.get()
        if (turn is None or message is None or not turn.grant
                or any(getattr(turn.message, name) != getattr(message, name)
                       for name in ("channel", "user_id", "chat_id", "message_id", "text"))):
            return None
        if turn.grant["state"] != "active" or turn.grant["expires"] <= time.time():
            return None
        return turn

    def observe(self, message, target, fields, record_ids=()):
        turn = self.current(message)
        if turn is None:
            return
        turn.touched = True
        # Follow-up reads must not expand a previously granted scope.
        if turn.grant["source_message"] != message.message_id or turn.grant["scopes"] != "{}":
            return
        named = [name for name in fields if name in turn.grant["source_text"]]
        selected = named or list(fields)
        scope = turn.scopes.setdefault(target.key, {"fields": [], "records": []})
        scope["fields"] = sorted(set(scope["fields"]) | set(selected))
        scope["records"] = sorted(set(scope["records"]) | set(record_ids))
        with self.service.store.connect() as db:
            db.execute("UPDATE conversation_intents SET scopes=? WHERE identity=? AND id=? AND state='active'",
                       (canonical(turn.scopes), turn.key, turn.grant["id"]))

    def allows_preview(self, action, target, message, records):
        turn = self.current(message)
        if (turn is None or turn.grant["source_message"] == message.message_id
                or action not in json.loads(turn.grant["actions"])
                or BLOCKED.search(message.text) or QUESTION.match(message.text)):
            return False
        scope = turn.scopes.get(target.key)
        if scope is None:
            return False
        if action == "delete":
            return set(records) <= set(scope["records"])
        return all((action == "create" or record["record_id"] in scope["records"])
                   and (set(record["fields"]) | set(record.get("clear_fields", []))) <= set(scope["fields"])
                   for record in records)

    def close(self, turn=None):
        turn = turn or CURRENT_INTENT.get()
        if turn is None or turn.grant is None:
            return
        with self.service.store.connect() as db:
            db.execute("UPDATE conversation_intents SET state='closed' WHERE identity=? AND id=?",
                       (turn.key, turn.grant["id"]))
        turn.grant["state"] = "closed"
