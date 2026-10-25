"""SQLite operation ledger, one-shot confirmation and cross-process table leases."""
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from tele_bot.bitable.models import BitableError, canonical


class BitableStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS operations (
              id TEXT PRIMARY KEY, dedup TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL,
              chat_id TEXT NOT NULL, policy TEXT NOT NULL, target TEXT NOT NULL,
              action TEXT NOT NULL, plan TEXT NOT NULL, status TEXT NOT NULL,
              token TEXT UNIQUE NOT NULL, client_token TEXT NOT NULL, expires REAL NOT NULL,
              result TEXT, updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              source_message TEXT NOT NULL DEFAULT '', request_scope TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS leases (target TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS discovered_targets (
              namespace TEXT NOT NULL, app_token TEXT NOT NULL, table_id TEXT NOT NULL,
              base_name TEXT NOT NULL, table_name TEXT NOT NULL,
              PRIMARY KEY(namespace, app_token, table_id));
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(operations)")}
            for column in ("source_message", "request_scope"):
                if column not in columns:
                    db.execute(f"ALTER TABLE operations ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lease(self, key, ttl=600):
        owner, now = str(uuid.uuid4()), time.time()
        with self.connect() as db:
            db.execute("DELETE FROM leases WHERE target=? AND expires<?", (key, now))
            try:
                db.execute("INSERT INTO leases VALUES (?,?,?)", (key, owner, now + ttl))
            except sqlite3.IntegrityError:
                raise BitableError("conflict", "同一数据表已有操作执行中，请稍后再试") from None
        try:
            yield
        finally:
            with self.connect() as db:
                db.execute("DELETE FROM leases WHERE target=? AND owner=?", (key, owner))

    def discovered_targets(self, namespace):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT app_token,table_id,base_name,table_name FROM discovered_targets WHERE namespace=? ORDER BY rowid",
                (namespace,))]

    def remember_targets(self, namespace, app_token, base_name, tables):
        with self.connect() as db:
            existing = {r[0:2] for r in db.execute(
                "SELECT app_token,table_id FROM discovered_targets WHERE namespace=?", (namespace,))}
            if len(existing | {(app_token, table_id) for table_id in tables}) > 100:
                raise BitableError("limit", "自动发现缓存最多保存 100 张数据表，请缩小范围")
            db.executemany("INSERT INTO discovered_targets VALUES (?,?,?,?,?) "
                           "ON CONFLICT(namespace,app_token,table_id) DO UPDATE SET "
                           "base_name=excluded.base_name,table_name=excluded.table_name",
                           [(namespace, app_token, table_id, base_name, table_name) for table_id, table_name in tables.items()])

    def prepare(self, *, dedup, request_scope, message, policy, target, action, plan, ttl):
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO operations (id,dedup,user_id,chat_id,policy,target,action,plan,status,"
                       "token,client_token,expires,result,updated,attempts,source_message,request_scope) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), dedup, message.user_id, message.chat_id, policy, target,
                        action, canonical(plan), "prepared", secrets.token_urlsafe(24), str(uuid.uuid4()),
                        now + ttl, None, now, 0, message.message_id, request_scope))
            return dict(db.execute("SELECT * FROM operations WHERE dedup=?", (dedup,)).fetchone())

    def get(self, operation_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=? OR token=?", (operation_id, operation_id)).fetchone()
        if row is None:
            raise BitableError("not_found", "操作不存在")
        result = dict(row)
        if result["status"] == "running" and result["updated"] + 600 < time.time():
            self.finish(result["id"], "unknown", {"message": "上次执行中断，必须核对远端结果，不会自动重做"})
            return self.get(result["id"])
        return result

    def finish(self, operation_id, status, result):
        with self.connect() as db:
            db.execute("UPDATE operations SET status=?,result=?,updated=? WHERE id=?",
                       (status, canonical(result), time.time(), operation_id))

    def pending(self, user_id, chat_id, policy):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM operations WHERE user_id=? AND chat_id=? AND policy=? "
                "AND status IN ('prepared','awaiting_confirmation') ORDER BY (expires>?) DESC, updated DESC LIMIT 6",
                (user_id, chat_id, policy, time.time()))]

    def claim(self, operation_id):
        with self.connect() as db:
            changed = db.execute("UPDATE operations SET status='running',updated=?,attempts=attempts+1 "
                                 "WHERE id=? AND status IN ('prepared','awaiting_confirmation')",
                                 (time.time(), operation_id)).rowcount
        return bool(changed)

    def cursor(self, payload, ttl=600):
        token = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute("DELETE FROM cursors WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO cursors VALUES (?,?,?)", (token, canonical(payload), time.time() + ttl))
        return token

    def read_cursor(self, token):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM cursors WHERE id=? AND expires>?", (token, time.time())).fetchone()
        if row is None:
            raise BitableError("pagination", "游标无效或已过期")
        return json.loads(row[0])
