"""SQLite state transitions for scheduled tasks and individual occurrences."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
TERMINAL = {"success", "failed", "timeout", "skipped", "cancelled", "reminded"}
TASK_SUMMARY_SQL = (
    "SELECT t.*, "
    "(SELECT COUNT(*) FROM runs r WHERE r.task_id=t.id AND r.started_at IS NOT NULL) actual_count, "
    "(SELECT COUNT(*) FROM runs r WHERE r.task_id=t.id AND r.status='success') success_count, "
    "(SELECT COUNT(*) FROM runs r WHERE r.task_id=t.id AND r.status='skipped') skipped_count "
    "FROM tasks t "
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def next_time(planned: datetime, rule: dict) -> datetime | None:
    kind = rule["kind"]
    if kind == "once":
        return None
    local = planned.astimezone(TZ)
    if kind == "daily":
        return (local + timedelta(days=rule.get("interval", 1))).astimezone(timezone.utc)
    if kind == "weekly":
        days = sorted(set(rule["weekdays"]))
        interval = rule.get("interval", 1)
        origin_monday = local.date() - timedelta(days=local.weekday())
        for distance in range(1, 7 * interval + 8):
            candidate = local + timedelta(days=distance)
            candidate_monday = candidate.date() - timedelta(days=candidate.weekday())
            week_distance = (candidate_monday - origin_monday).days // 7
            if candidate.weekday() in days and week_distance % interval == 0:
                return candidate.astimezone(timezone.utc)
    raise ValueError(f"unsupported rule: {kind}")


class ScheduleStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                  chat_id TEXT NOT NULL, content TEXT NOT NULL, mode TEXT NOT NULL,
                  scope_json TEXT NOT NULL, rule_json TEXT NOT NULL,
                  first_at TEXT NOT NULL, next_at TEXT, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                  planned_count INTEGER NOT NULL DEFAULT 0, missed_policy TEXT NOT NULL,
                  grace_seconds INTEGER NOT NULL DEFAULT 300, auth_seconds INTEGER NOT NULL DEFAULT 900,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status,next_at);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                  task_version INTEGER NOT NULL, planned_at TEXT NOT NULL, status TEXT NOT NULL,
                  auth_expires_at TEXT, started_at TEXT, finished_at TEXT,
                  result TEXT, error TEXT, created_at TEXT NOT NULL,
                  UNIQUE(task_id,task_version,planned_at)
                );
                CREATE INDEX IF NOT EXISTS runs_ready ON runs(status,created_at);
                CREATE TABLE IF NOT EXISTS proposals (
                  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                  chat_id TEXT NOT NULL, operation TEXT NOT NULL, payload_json TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_drafts (
                  owner_id TEXT NOT NULL, channel TEXT NOT NULL, chat_id TEXT NOT NULL,
                  original_text TEXT NOT NULL, expires_at TEXT NOT NULL,
                  PRIMARY KEY (owner_id, channel, chat_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                  event_type TEXT NOT NULL, task_id TEXT, run_id TEXT,
                  user_id TEXT, result TEXT, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS notifications (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT,
                  user_id TEXT NOT NULL, channel TEXT NOT NULL, chat_id TEXT NOT NULL,
                  body TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'generic',
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT NOT NULL, last_error TEXT,
                  created_at TEXT NOT NULL, sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS notifications_due ON notifications(status,next_attempt_at);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(notifications)")}
            if "kind" not in columns:
                db.execute("ALTER TABLE notifications ADD COLUMN kind TEXT NOT NULL DEFAULT 'generic'")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _event(db, event_type, task_id=None, run_id=None, user_id=None, result=None, detail=None):
        db.execute("INSERT INTO events(occurred_at,event_type,task_id,run_id,user_id,result,detail) VALUES(?,?,?,?,?,?,?)",
                   (iso(now_utc()), event_type, task_id, run_id, user_id, result, detail))

    def event(self, *args, **kwargs):
        with self._db() as db:
            self._event(db, *args, **kwargs)

    def enqueue_notification(self, task: dict, body: str, kind: str = "generic") -> dict:
        notice = {
            "id": uuid4().hex[:16],
            "task_id": task.get("task_id") or task["id"],
            "run_id": task.get("run_id"),
            "user_id": task["owner_id"], "channel": task["channel"],
            "chat_id": task["chat_id"], "body": body, "kind": kind,
        }
        stamp = iso(now_utc())
        with self._db() as db:
            db.execute("INSERT INTO notifications(id,task_id,run_id,user_id,channel,chat_id,body,kind,status,next_attempt_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (notice["id"], notice["task_id"], notice["run_id"], notice["user_id"],
                 notice["channel"], notice["chat_id"], body, kind, "pending", stamp, stamp))
            self._event(db, "feedback_queued", notice["task_id"], notice["run_id"], notice["user_id"], "pending")
        return notice

    def pending_notifications(self, current: datetime, limit: int = 20) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM notifications WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at LIMIT ?", (iso(current), limit)).fetchall()
            return [dict(row) for row in rows]

    def claim_notification(self, notice_id: str) -> dict | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("UPDATE notifications SET status='sending' WHERE id=? AND status='pending' AND next_attempt_at<=?", (notice_id, iso(now_utc()))).rowcount
            if not updated:
                db.execute("ROLLBACK")
                return None
            row = dict(db.execute("SELECT * FROM notifications WHERE id=?", (notice_id,)).fetchone())
            db.execute("COMMIT")
            return row

    def mark_notification(self, notice: dict, error: str | None = None) -> None:
        with self._db() as db:
            if error is None:
                db.execute("UPDATE notifications SET status='sent',sent_at=?,attempts=attempts+1,last_error=NULL WHERE id=? AND status='sending'", (iso(now_utc()), notice["id"]))
                self._event(db, "feedback_sent", notice["task_id"], notice["run_id"], notice["user_id"], "success")
            else:
                attempt = int(notice.get("attempts", 0)) + 1
                delay = min(3600, 2 ** min(attempt, 10))
                db.execute("UPDATE notifications SET status='pending',attempts=?,next_attempt_at=?,last_error=? WHERE id=? AND status='sending'",
                    (attempt, iso(now_utc() + timedelta(seconds=delay)), error[:1000], notice["id"]))
                self._event(db, "feedback_failed", notice["task_id"], notice["run_id"], notice["user_id"], "pending", error[:1000])

    def is_authorizable(self, run_id: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT 1 FROM runs WHERE id=? AND status='awaiting_auth' AND auth_expires_at>=?", (run_id, iso(now_utc()))).fetchone()
            return row is not None

    def is_run_running(self, run_id: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM runs WHERE id=? AND status='running'", (run_id,)).fetchone() is not None

    def supersede_notification(self, notice: dict) -> None:
        with self._db() as db:
            db.execute("UPDATE notifications SET status='superseded' WHERE id=? AND status='sending'", (notice["id"],))
            self._event(db, "feedback_superseded", notice["task_id"], notice["run_id"], notice["user_id"], "superseded")

    def propose(self, owner: str, channel: str, chat: str, operation: str, payload: dict, ttl=900) -> str:
        token = uuid4().hex[:16]
        with self._db() as db:
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?)", (token, owner, channel, chat, operation,
                       json.dumps(payload, ensure_ascii=False), iso(now_utc() + timedelta(seconds=ttl))))
            self._event(db, "proposal_created", user_id=owner, detail=operation)
        return token

    def consume_proposal(self, token: str, owner: str, channel: str, chat: str) -> tuple[str, dict] | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM proposals WHERE id=? AND owner_id=? AND channel=? AND chat_id=?",
                             (token, owner, channel, chat)).fetchone()
            if row is None or row["expires_at"] < iso(now_utc()):
                db.execute("ROLLBACK")
                return None
            db.execute("DELETE FROM proposals WHERE id=?", (token,))
            db.execute("COMMIT")
            return row["operation"], json.loads(row["payload_json"])

    def pending_for_chat(self, owner: str, channel: str, chat: str) -> tuple[list[str], list[str]]:
        with self._db() as db:
            proposals = [row["id"] for row in db.execute(
                "SELECT id FROM proposals WHERE owner_id=? AND channel=? AND chat_id=? AND expires_at>=?",
                (owner, channel, chat, iso(now_utc()))).fetchall()]
            runs = [row["id"] for row in db.execute(
                "SELECT r.id FROM runs r JOIN tasks t ON t.id=r.task_id "
                "WHERE t.owner_id=? AND t.channel=? AND t.chat_id=? AND t.status='active' "
                "AND r.status='awaiting_auth' AND r.auth_expires_at>=?",
                (owner, channel, chat, iso(now_utc()))).fetchall()]
            return proposals, runs

    def save_draft(self, owner: str, channel: str, chat: str, original_text: str,
                   ttl: int = 900) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO schedule_drafts(owner_id,channel,chat_id,original_text,expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(owner_id,channel,chat_id) DO UPDATE SET "
                "original_text=excluded.original_text,expires_at=excluded.expires_at",
                (owner, channel, chat, original_text, iso(now_utc() + timedelta(seconds=ttl))),
            )

    def get_draft(self, owner: str, channel: str, chat: str) -> str | None:
        with self._db() as db:
            row = db.execute(
                "SELECT original_text FROM schedule_drafts WHERE owner_id=? AND channel=? "
                "AND chat_id=? AND expires_at>=?",
                (owner, channel, chat, iso(now_utc())),
            ).fetchone()
            return str(row["original_text"]) if row else None

    def clear_draft(self, owner: str, channel: str, chat: str) -> None:
        with self._db() as db:
            db.execute(
                "DELETE FROM schedule_drafts WHERE owner_id=? AND channel=? AND chat_id=?",
                (owner, channel, chat),
            )

    def create_many(self, owner: str, channel: str, chat: str, specs: list[dict]) -> list[str]:
        ids = []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for spec in specs:
                task_id = uuid4().hex[:12]
                stamp = iso(now_utc())
                db.execute("INSERT INTO tasks(id,owner_id,channel,chat_id,content,mode,scope_json,rule_json,first_at,next_at,status,missed_policy,grace_seconds,auth_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, owner, channel, chat, spec["content"], spec["mode"],
                     json.dumps(spec["scope"], ensure_ascii=False), json.dumps(spec["rule"]),
                     spec["at"], spec["at"], "active", spec["missed_policy"], spec["grace_seconds"], spec["auth_seconds"], stamp, stamp))
                self._event(db, "task_created", task_id, user_id=owner, result="active")
                ids.append(task_id)
            db.execute("COMMIT")
        return ids

    def get(self, task_id: str, owner: str) -> dict | None:
        with self._db() as db:
            row = db.execute(TASK_SUMMARY_SQL + "WHERE t.id=? AND t.owner_id=?", (task_id, owner)).fetchone()
            return dict(row) if row else None

    def list_tasks(self, owner: str, *, history=False) -> list[dict]:
        with self._db() as db:
            rows = db.execute(TASK_SUMMARY_SQL + "WHERE t.owner_id=? " + ("" if history else "AND t.status='active' ") + "ORDER BY t.created_at DESC", (owner,)).fetchall()
            self._event(db, "task_query", user_id=owner, result=str(len(rows)))
            return [dict(row) for row in rows]

    def history(self, task_id: str, owner: str) -> list[dict] | None:
        with self._db() as db:
            if not db.execute("SELECT 1 FROM tasks WHERE id=? AND owner_id=?", (task_id, owner)).fetchone():
                return None
            rows = db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY planned_at DESC", (task_id,)).fetchall()
            self._event(db, "history_query", task_id, user_id=owner, result=str(len(rows)))
            return [dict(row) for row in rows]

    def update(self, task_id: str, owner: str, spec: dict) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE id=? AND owner_id=? AND status='active'", (task_id, owner)).fetchone()
            if not task or db.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task_id,)).fetchone():
                db.execute("ROLLBACK")
                return False
            pending = db.execute("SELECT id FROM runs WHERE task_id=? AND status IN ('awaiting_auth','queued')", (task_id,)).fetchall()
            db.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE task_id=? AND status IN ('awaiting_auth','queued')", (iso(now_utc()), task_id))
            for run in pending:
                self._event(db, "execution_cancelled_by_update", task_id, run["id"], owner, "cancelled")
            db.execute("UPDATE tasks SET content=?,mode=?,scope_json=?,rule_json=?,first_at=?,next_at=?,missed_policy=?,grace_seconds=?,auth_seconds=?,version=version+1,planned_count=0,updated_at=? WHERE id=?",
                       (spec["content"], spec["mode"], json.dumps(spec["scope"], ensure_ascii=False), json.dumps(spec["rule"]), spec["at"], spec["at"], spec["missed_policy"], spec["grace_seconds"], spec["auth_seconds"], iso(now_utc()), task_id))
            self._event(db, "task_updated", task_id, user_id=owner, result="active")
            db.execute("COMMIT")
            return True

    def cancel(self, task_id: str, owner: str) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("UPDATE tasks SET status='cancelled',next_at=NULL,updated_at=? WHERE id=? AND owner_id=? AND status='active'", (iso(now_utc()), task_id, owner)).rowcount
            if updated:
                pending = db.execute("SELECT id FROM runs WHERE task_id=? AND status IN ('awaiting_auth','queued')", (task_id,)).fetchall()
                db.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE task_id=? AND status IN ('awaiting_auth','queued')", (iso(now_utc()), task_id))
                for run in pending:
                    self._event(db, "execution_cancelled_by_task", task_id, run["id"], owner, "cancelled")
                self._event(db, "task_cancelled", task_id, user_id=owner, result="cancelled")
                if db.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task_id,)).fetchone():
                    self._event(db, "execution_cancel_requested", task_id, user_id=owner, result="pending")
            db.execute("COMMIT")
            return bool(updated)

    def is_run_cancelled(self, run_id: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT t.status FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=?", (run_id,)).fetchone()
            return row is None or row["status"] == "cancelled"

    def due(self, current: datetime) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks WHERE status='active' AND next_at<=? ORDER BY next_at LIMIT 100", (iso(current),)).fetchall()]

    def trigger(self, task_id: str, expected_at: str, current: datetime) -> dict | None:
        """Atomically reserve one occurrence and advance the recurrence cursor."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE id=? AND status='active' AND next_at=?", (task_id, expected_at)).fetchone()
            if not task:
                db.execute("ROLLBACK")
                return None
            task = dict(task)
            rule = json.loads(task["rule_json"])
            # Collapse an offline interval into one possible catch-up execution.
            # Every missed planned trigger still has its own historical run.
            planned = [datetime.fromisoformat(expected_at)]
            count = task["planned_count"]
            following = planned[0]
            while True:
                count += 1
                following = next_time(following, rule)
                if following and rule.get("count") and count >= rule["count"]:
                    following = None
                if following and rule.get("until") and iso(following) > rule["until"]:
                    following = None
                if following is None or following > current:
                    break
                planned.append(following)
            overlap = db.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('awaiting_auth','queued','running')", (task_id,)).fetchone()
            last_planned = planned[-1]
            late = (current - last_planned).total_seconds() > task["grace_seconds"]
            mode = task["mode"]
            if overlap:
                state = "skipped"
                reason = "previous occurrence still active"
            elif late and task["missed_policy"] == "skip":
                state = "skipped"
                reason = f"missed {len(planned)} planned occurrence(s)"
            elif mode == "remind":
                state = "reminded"
                reason = "reminder delivered"
            elif mode == "confirm" or (late and task["missed_policy"] == "ask"):
                state = "awaiting_auth"
                reason = "execution authorization required"
            else:
                state = "queued"
                reason = "ready"
            run_id = uuid4().hex[:16]
            expires = iso(current + timedelta(seconds=task["auth_seconds"])) if state == "awaiting_auth" else None
            for older in planned[:-1]:
                older_id = uuid4().hex[:16]
                db.execute("INSERT INTO runs(id,task_id,task_version,planned_at,status,finished_at,result,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (older_id, task_id, task["version"], iso(older), "skipped", iso(current), "coalesced after missed schedule", iso(current)))
                self._event(db, "missed_occurrence_skipped", task_id, older_id, task["owner_id"], "skipped")
            db.execute("INSERT INTO runs(id,task_id,task_version,planned_at,status,auth_expires_at,finished_at,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, task["version"], iso(last_planned), state, expires, iso(current) if state in TERMINAL else None, reason if state in TERMINAL else None, iso(current)))
            db.execute("UPDATE tasks SET next_at=?,planned_count=?,updated_at=? WHERE id=?", (iso(following) if following else None, count, iso(current), task_id))
            self._event(db, "schedule_triggered", task_id, run_id, task["owner_id"], state, reason)
            if following:
                self._event(db, "cycle_rearranged", task_id, run_id, task["owner_id"], detail=iso(following))
            if state in TERMINAL and not following:
                self._finish_task_if_done(db, task_id)
            db.execute("COMMIT")
            return {**task, "run_id": run_id, "run_status": state, "reason": reason, "auth_expires_at": expires, "next_at": iso(following) if following else None}

    def authorize(self, run_id: str, owner: str, approve: bool) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.*,t.owner_id,t.status task_status FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND t.owner_id=?", (run_id, owner)).fetchone()
            if not row or row["status"] != "awaiting_auth" or row["task_status"] != "active" or row["auth_expires_at"] < iso(now_utc()):
                db.execute("ROLLBACK")
                return False
            status = "queued" if approve else "skipped"
            db.execute("UPDATE runs SET status=?,finished_at=?,result=? WHERE id=?", (status, None if approve else iso(now_utc()), None if approve else "authorization declined", run_id))
            self._event(db, "authorization_result", row["task_id"], run_id, owner, status)
            if not approve:
                self._finish_task_if_done(db, row["task_id"])
            db.execute("COMMIT")
            return True

    def expire_authorizations(self, current: datetime) -> list[dict]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(row) for row in db.execute("SELECT r.id run_id,r.task_id,t.owner_id,t.channel,t.chat_id FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.status='awaiting_auth' AND r.auth_expires_at<?", (iso(current),)).fetchall()]
            for row in rows:
                db.execute("UPDATE runs SET status='skipped',finished_at=?,result='authorization expired' WHERE id=?", (iso(current), row["run_id"]))
                self._event(db, "authorization_expired", row["task_id"], row["run_id"], row["owner_id"], "skipped")
                self._finish_task_if_done(db, row["task_id"])
            db.execute("COMMIT")
            return rows

    def claim_run(self) -> dict | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.*,t.owner_id,t.channel,t.chat_id,t.content,t.mode,t.scope_json,t.status task_status,t.next_at FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.status='queued' ORDER BY r.created_at LIMIT 1").fetchone()
            if not row:
                db.execute("ROLLBACK")
                return None
            row = dict(row)
            if row["task_status"] != "active":
                db.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE id=?", (iso(now_utc()), row["id"]))
                db.execute("COMMIT")
                return None
            db.execute("UPDATE runs SET status='running',started_at=? WHERE id=? AND status='queued'", (iso(now_utc()), row["id"]))
            self._event(db, "execution_started", row["task_id"], row["id"], row["owner_id"], "running")
            db.execute("COMMIT")
            return row

    def finish_run(self, run_id: str, status: str, result: str, error: str = "") -> dict | None:
        if status not in {"success", "failed", "timeout", "cancelled"}:
            raise ValueError(status)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.task_id,t.owner_id,t.channel,t.chat_id,t.status task_status,t.next_at FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND r.status='running'", (run_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return None
            row = dict(row)
            db.execute("UPDATE runs SET status=?,finished_at=?,result=?,error=? WHERE id=?", (status, iso(now_utc()), result, error, run_id))
            self._event(db, "execution_finished", row["task_id"], run_id, row["owner_id"], status, error or result[:500])
            self._finish_task_if_done(db, row["task_id"])
            db.execute("COMMIT")
            return row

    def recover(self) -> list[dict]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE notifications SET status='pending',next_attempt_at=? WHERE status='sending'", (iso(now_utc()),))
            rows = [dict(row) for row in db.execute("SELECT r.id run_id,r.task_id,t.owner_id,t.channel,t.chat_id FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.status='running'").fetchall()]
            for row in rows:
                db.execute("UPDATE runs SET status='failed',finished_at=?,error='service interrupted' WHERE id=?", (iso(now_utc()), row["run_id"]))
                self._event(db, "interruption_recovered", row["task_id"], row["run_id"], row["owner_id"], "failed")
                self._finish_task_if_done(db, row["task_id"])
            db.execute("COMMIT")
            return rows

    @staticmethod
    def _finish_task_if_done(db, task_id: str):
        row = db.execute("SELECT next_at,status,owner_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row["status"] == "active" and row["next_at"] is None:
            pending = db.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('awaiting_auth','queued','running')", (task_id,)).fetchone()
            if not pending:
                db.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?", (iso(now_utc()), task_id))
                ScheduleStore._event(db, "task_completed", task_id, user_id=row["owner_id"], result="completed")
