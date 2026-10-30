"""Administrator-only private history browsing and conversation continuation."""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from threading import RLock

from tele_bot.models import CardAction, InteractiveCard, OutgoingMessage
from tele_bot.tools.codex_history import CodexHistoryError, THREAD_ID, thread_is_archived

_HISTORY = re.compile(r"^(?:查看|看看|列出|显示)?\s*codex\s*(?:的)?\s*(?:最近|历史)?\s*(?:会话|对话)(?:列表|记录)?[。！!？?]?$", re.I)
_RESUME = re.compile(r"^(?:继续|恢复)(?:执行)?(?:这个|此|该)(?:会话|对话)\s*[:：]?\s*(.*)$", re.S)
_PREFIX = "codex_history_"
_TZ = timezone(timedelta(hours=8))


def _safe(text, limit=None):
    text = str(text or "")
    if limit is not None and len(text) > limit:
        text = text[:limit] + "…"
    text = html.escape(text, quote=False)
    text = re.sub(r"([\\`*\[\]])", r"\\\1", text)
    return text[:limit] if limit is not None else text


def _title(thread):
    return str(thread.get("name") or thread.get("preview") or "未命名会话").splitlines()[0][:90]


@dataclass
class _Action:
    admin_id: str
    chat_id: str
    operation: str
    args: dict
    expires: float


class CodexHistoryService:
    def __init__(self, client, admin_user_ids: tuple[str, ...], *, clock=time.monotonic):
        self.client, self.admin_user_ids, self.clock = client, frozenset(admin_user_ids), clock
        self.actions: dict[str, _Action] = {}
        self.pages: dict[str, tuple[list[dict], float]] = {}
        self.selected: dict[str, tuple[dict, float]] = {}
        self.waiting: dict[str, float] = {}
        self.lock = RLock()

    def _prune(self):
        now = self.clock()
        self.actions = {k: v for k, v in self.actions.items() if v.expires > now}
        self.selected = {k: v for k, v in self.selected.items() if v[1] > now}
        self.pages = {k: v for k, v in self.pages.items() if v[1] > now}
        self.waiting = {k: v for k, v in self.waiting.items() if v > now and k in self.selected}
        while len(self.actions) >= 128:
            self.actions.pop(next(iter(self.actions)))

    def _button(self, message, operation, label, **args):
        self._prune()
        token = token_urlsafe(24)
        self.actions[token] = _Action(self._admin_id(message), message.chat_id, operation, args,
                                      self.clock() + 1800)
        return CardAction(_PREFIX + operation, label, token)

    def _reply(self, message, body, *, title="Codex 历史会话", actions=()):
        return OutgoingMessage(message.channel, message.chat_id, body,
                               reply_to_message_id=message.message_id,
                               card=InteractiveCard(title, body, actions=tuple(actions)))

    def _admin_id(self, message):
        if message.channel == "feishu":
            return next((value for value in (message.user_id, *message.sender_ids)
                         if value in self.admin_user_ids), None)
        return None

    def lookup(self, message, *, search="", cursor=None, thread_id="", archived=False):
        """Read native metadata for a trusted private-chat tool call; never run a turn."""
        if message is None or message.chat_type != "p2p" or not self._admin_id(message):
            raise CodexHistoryError("仅配置的管理员可在飞书私聊中查询 Codex 会话。")
        if thread_id:
            if not THREAD_ID.fullmatch(thread_id):
                raise CodexHistoryError("请提供完整的 Codex 会话 ID。")
            rows = [self.client.read_thread(thread_id)]
            next_cursor = None
        else:
            page = self.client.list_threads(search=search[:200], cursor=cursor, archived=archived)
            rows, next_cursor = page["data"][:10], page.get("nextCursor")
        return {"data": [{"id": row["id"], "title": _title(row), "cwd": row.get("cwd"),
                          "model": row.get("model"), "updatedAt": row.get("updatedAt"),
                          "archived": archived or thread_is_archived(row)} for row in rows],
                "nextCursor": next_cursor}

    def handle(self, message, *, trusted=True, progress=None):
        text = message.text.strip()
        parts = text.split(maxsplit=2)
        command = (parts[1].lower() if len(parts) > 1 and parts[0].lower() == "/codex" else "")
        action = message.card_action
        is_action = action is not None and action.action_id.startswith(_PREFIX)
        natural_list = bool(_HISTORY.fullmatch(text))
        natural_resume = _RESUME.fullmatch(text)
        with self.lock:
            self._prune()
            waiting = message.chat_id in self.waiting
            intent = is_action or command in {"history", "resume"} or natural_list or natural_resume
            # Once the administrator presses Continue, the next plain text task
            # belongs to that selection. Slash commands always keep their routing.
            followup = waiting and not text.startswith("/") and action is None
            if not intent and not followup:
                return None
            if not trusted or not self._admin_id(message):
                return self._reply(message, "仅配置的管理员可在飞书私聊中查看或恢复 Codex 会话。")
            if is_action:
                grant = self.actions.get(action.value)
                # Feishu callbacks omit chat_type. A server-issued token binds
                # them to the exact private chat in which the card was issued.
                if (grant is None or grant.admin_id != self._admin_id(message) or grant.chat_id != message.chat_id
                        or action.action_id != _PREFIX + grant.operation
                        or message.chat_type not in {None, "p2p"}):
                    return self._reply(message, "此按钮已过期或不属于当前私聊，请重新发送 /codex history。")
            elif message.chat_type != "p2p":
                return self._reply(message, "请在本人私聊中使用 Codex 历史会话功能。")
            try:
                if is_action:
                    return self._action(message, grant, progress)
                if natural_list or command == "history":
                    self.waiting.pop(message.chat_id, None)
                    query = parts[2].strip() if command and len(parts) > 2 else ""
                    if query.isdigit():
                        return self._detail(message, self._number(message, query))
                    archived = query == "--archived" or query.startswith("--archived ")
                    if archived:
                        query = query[len("--archived"):].strip()
                    return self._list(message, search=query[:200], archived=archived)
                if command == "resume":
                    args = parts[2].split(maxsplit=1) if len(parts) > 2 else []
                    if args and (args[0].isdigit() or THREAD_ID.fullmatch(args[0])):
                        thread = (self._number(message, args[0]) if args[0].isdigit()
                                  else self.client.read_thread(args[0]))
                        self.selected[message.chat_id] = (thread, self.clock() + 1800)
                        task = args[1] if len(args) > 1 else ""
                    else:
                        task = parts[2] if len(parts) > 2 else ""
                    return self._continue(message, task, progress)
                if natural_resume:
                    return self._continue(message, natural_resume[1], progress)
                if followup:
                    if text in {"取消", "cancel"}:
                        self.waiting.pop(message.chat_id, None)
                        return self._reply(message, "已取消继续执行。")
                    if message.attachments:
                        return self._reply(message, "继续执行目前接收文字任务，请发送任务文字或回复“取消”。")
                    return self._continue(message, text, progress)
            except (CodexHistoryError, OSError, ValueError, KeyError, TypeError) as exc:
                # Only our deliberate safe errors may be displayed. Never emit
                # arbitrary backend exceptions, commands, stderr or transcripts.
                body = str(exc) if isinstance(exc, CodexHistoryError) else "Codex 返回的数据无法读取，请检查 CLI 版本后重试。"
                return self._reply(message, body)

    def _number(self, message, number):
        rows = self.pages.get(message.chat_id, ([], 0))[0]
        index = int(number) - 1
        if index < 0 or index >= len(rows):
            raise CodexHistoryError("序号不在当前列表内，请先发送 /codex history。")
        return rows[index]

    def _action(self, message, grant, progress):
        if grant.operation == "list":
            self.waiting.pop(message.chat_id, None)
            return self._list(message, **grant.args)
        if grant.operation == "detail":
            return self._detail(message, **grant.args)
        if grant.operation == "continue":
            self.selected[message.chat_id] = (grant.args["thread"], self.clock() + 1800)
            return self._continue(message, "", progress)
        raise CodexHistoryError("不支持此 Codex 操作。")

    def _list(self, message, *, cursor=None, search="", archived=False):
        page = self.client.list_threads(cursor=cursor, search=search, archived=archived)
        rows = [dict(row, _archived=archived) for row in page["data"][:10]]
        self.pages[message.chat_id] = (rows, self.clock() + 1800)
        lines, buttons = [], []
        for index, thread in enumerate(rows, 1):
            timestamp = thread.get("updatedAt")
            date = datetime.fromtimestamp(timestamp, _TZ).strftime("%m-%d %H:%M") if timestamp else "时间未知"
            lines.append(f"**{index}. {_safe(_title(thread), 90)}**\n{date} · {_safe(thread.get('cwd', ''), 150)}")
            buttons.append(self._button(message, "detail", f"查看 {index}", thread=thread))
        if page.get("nextCursor"):
            buttons.append(self._button(message, "list", "下一页", cursor=page["nextCursor"],
                                        search=search, archived=archived))
        buttons.append(self._button(message, "list", "刷新列表", search=search, archived=archived))
        lines.append("点击查看详情；也可发送 /codex history 序号。时间为北京时间。")
        if not rows:
            lines.insert(0, "没有找到匹配的会话。")
        return self._reply(message, "\n\n".join(lines), title="Codex 已归档会话" if archived else "Codex 最近会话", actions=buttons)

    def _detail(self, message, thread, *, cursor=None, buffer="", tail_cursor=None):
        self.waiting.pop(message.chat_id, None)
        thread = dict(self.client.read_thread(thread["id"]), _archived=thread.get("_archived", False))
        self.selected[message.chat_id] = (thread, self.clock() + 1800)
        if not buffer:
            page = self.client.read_turns(thread["id"], cursor)
            sections, count = [], 0
            for turn in page["data"]:
                sections.append("── 一轮对话 ──")
                for item in turn.get("items", []):
                    if item.get("type") == "userMessage":
                        content = "\n".join(part.get("text", "[非文字输入]") for part in item.get("content", []))
                        label = "你"
                    elif item.get("type") == "agentMessage":
                        content, label = item.get("text", ""), "Codex"
                    else:
                        continue
                    if content and count < 100_000:
                        sections.append(f"{label}：\n{content[:100_000 - count]}")
                        count += len(content)
            if count > 100_000:
                sections.append("（本批文字超过 10 万字，余下内容请在 Codex 中查看。）")
            buffer = _safe("\n\n".join(sections) or "此页没有可展示的文字对话。")
            tail_cursor = page.get("nextCursor")
        # Escaped display data stays in a bounded, chat-scoped token between pages.
        visible, remaining = buffer[:2900], buffer[2900:]
        buttons = []
        if remaining or tail_cursor:
            buttons.append(self._button(message, "detail", "继续阅读" if remaining else "更早对话",
                                        thread=thread, cursor=tail_cursor, buffer=remaining,
                                        tail_cursor=tail_cursor))
        if not thread.get("_archived"):
            buttons.append(self._button(message, "continue", "继续此会话", thread=thread))
        buttons.append(self._button(message, "list", "返回列表"))
        body = (f"**{_safe(_title(thread))}**\n{_safe(thread.get('cwd'), 200)}\n"
                "按轮次由新到旧，仅展示用户和助手文字。\n\n" + visible)
        return self._reply(message, body, title="Codex 会话详情", actions=buttons)

    def _continue(self, message, task, progress):
        selected = self.selected.get(message.chat_id)
        if selected is None:
            raise CodexHistoryError("请先打开会话详情，或发送 /codex resume 序号 任务。")
        thread = selected[0]
        if thread_is_archived(thread):
            raise CodexHistoryError("此会话已归档，当前只能查看。请先在 Codex 中取消归档，再刷新列表继续执行。")
        if not task.strip():
            self.waiting[message.chat_id] = self.clock() + 600
            return self._reply(message, f"将继续：{_safe(_title(thread))}\n"
                               f"目录：{_safe(thread.get('cwd'), 200)}\n\n"
                               "请在 10 分钟内发送接下来要执行的文字任务，或回复“取消”。"
                               "发送任务后将沿用该会话历史，并可在该工作区修改文件。",
                               title="继续 Codex 会话")
        self.waiting.pop(message.chat_id, None)
        if progress:
            progress("Codex 正在恢复所选会话并执行任务，请稍候…")
        result = self.client.resume(thread["id"], task, progress=progress)
        buttons = (self._button(message, "detail", "查看最新对话", thread=thread),
                   self._button(message, "continue", "继续此会话", thread=thread))
        text = _safe(result)
        if len(text) > 2900:
            text = text[:2900] + "\n（回复过长，点击查看最新对话继续阅读。）"
        return self._reply(message, text, title="Codex 本轮执行完成", actions=buttons)
