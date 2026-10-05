"""User-facing task management, confirmation and strict proposal validation."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tele_bot.models import IncomingMessage, OutgoingMessage
from tele_bot.scheduled.store import ScheduleStore, TZ, iso, now_utc
from tele_bot.tools.workspace_policy import WorkspacePolicy
from tele_bot.tools.workspace_resolver import WorkspaceResolver
from tele_bot.tools.codex_runner import CodexRunner

TOOLS = {"opencli_search", "filesystem_list_dir", "filesystem_read_file", "write_report", "codex_apply"}
MODES = {"remind": "仅提醒", "auto": "预授权自动执行", "confirm": "每次执行前确认"}
MISSED = {"skip": "跳过", "run_once": "补执行一次", "ask": "重新申请授权"}


class ScheduleParser:
    """LLM extracts a proposal only; all writes remain behind validation/confirmation."""

    def __init__(self, llm) -> None:
        self.llm = llm

    def parse(self, text: str) -> dict:
        prompt = (
            "把用户的定时任务请求转换成 JSON 对象，不要 Markdown。当前时间："
            f"{datetime.now(TZ).isoformat(timespec='minutes')}，时区 Asia/Shanghai。"
            "格式：{\"operation\":\"create|update|delete|list|history|get|clarify\","
            "\"task_id\":\"\",\"tasks\":[{\"content\":\"\",\"at\":\"ISO8601 带时区\","
            "\"rule\":{\"kind\":\"once|daily|weekly\",\"weekdays\":[0-6],\"interval\":1,\"count\":null,\"until\":null},"
            "\"mode\":\"remind|auto|confirm\",\"scope\":{\"directories\":[],\"tools\":[]},"
            "\"missed_policy\":\"skip|run_once|ask\",\"grace_seconds\":300,\"auth_seconds\":900}],"
            "\"question\":\"\"}。一个请求多个任务分别放入 tasks。"
            "‘四点半点’视为‘四点半’。日期和时间不明确、缺少周期任务的执行时刻、'七点完成'之类截止时间与开始时间不清时，"
            "operation=clarify 并提出具体问题，不要猜。未明确授权时 mode=remind；"
            "只有用户明确表示未来自动执行才可用 auto。任务范围和工具只提取用户明确给出的内容，"
            "不要凭空扩权。可用工具：opencli_search、filesystem_list_dir、filesystem_read_file、write_report、codex_apply。"
            "用户明确要求未来用 Codex 执行时，工具设为 codex_apply，目录写入 scope.directories；"
            "Codex 可以在该目录调用指定 skill，skill 名称保留在 content 中。"
            "如果用户更正了先前的触发时间，以最后一次更正为准，并保留原任务内容。"
            "不要把查询任务写成创建。用户请求：\n" + text
        )
        response = self.llm.invoke(prompt)
        raw = response.content if hasattr(response, "content") else str(response)
        if isinstance(raw, list):
            raw = "".join(str(part) for part in raw)
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise ValueError("未能解析定时任务，请补充明确时间和任务内容。")
        return json.loads(match.group(0))


class ScheduleService:
    def __init__(self, store: ScheduleStore, parser: ScheduleParser | None = None,
                 policy: WorkspacePolicy | None = None) -> None:
        self.store = store
        self.parser = parser
        self.policy = policy or WorkspacePolicy.from_env()

    @staticmethod
    def _out(message: IncomingMessage, text: str) -> OutgoingMessage:
        return OutgoingMessage(message.channel, message.chat_id, text,
                               reply_to_message_id=message.message_id)

    @staticmethod
    def _looks_scheduled(text: str) -> bool:
        return bool(re.search(r"^/schedule\b|定时|提醒我|每(天|周|个星期|月)|(?:今天|明天|后天|周[一二三四五六日天]|星期[一二三四五六日天]).{0,12}(?:点|[:：][0-9]{2})|任务\s*(?:ID|id)|(?:删除|取消|修改|查看|查询).{0,4}任务", text, re.I))

    @staticmethod
    def _looks_time_correction(text: str) -> bool:
        time_pattern = r"(?:[零一二两三四五六七八九十\d]{1,3}点(?:半|[零一二两三四五六七八九十\d]{1,3}分?)?|(?:[01]?\d|2[0-3])[:：][0-5]\d)"
        if not re.search(time_pattern, text):
            return False
        if re.search(r"说错|更正|改成|改为|应该是|换成|推迟|提前|不是", text):
            return True
        return bool(re.fullmatch(
            rf"\s*(?:今天|明天|后天)?\s*(?:上午|下午|晚上|中午|凌晨)?\s*{time_pattern}\s*[。！!]?\s*",
            text,
        ))

    def will_handle(self, message: IncomingMessage) -> bool:
        text = message.text.strip()
        return self._looks_scheduled(text) or (
            self._looks_time_correction(text)
            and self.store.get_draft(message.user_id, message.channel, message.chat_id) is not None
        )

    def handle(self, message: IncomingMessage) -> OutgoingMessage | None:
        text = message.text.strip()
        if not text:
            return None
        if text in {"确认", "确认预授权", "取消", "授权", "拒绝"}:
            proposals, runs = self.store.pending_for_chat(message.user_id, message.channel, message.chat_id)
            if text in {"授权", "拒绝"} and len(runs) == 1:
                text = f"{text} {runs[0]}"
            elif text == "确认" and not proposals and len(runs) == 1:
                text = f"授权 {runs[0]}"
            elif text in {"确认", "确认预授权", "取消"} and len(proposals) == 1:
                text = f"{text} {proposals[0]}"
            elif proposals or runs:
                return self._out(message, "存在多个待处理任务，请带上消息中的确认或授权令牌。")
        confirmation = re.fullmatch(r"(?:/schedule\s+)?(确认预授权|确认|取消)\s+([a-f0-9]{16})", text, re.I)
        if confirmation:
            decision, token = confirmation.groups()
            if decision == "取消":
                proposal = self.store.consume_proposal(token, message.user_id, message.channel, message.chat_id)
                return self._out(message, "已取消待确认的任务变更。" if proposal else "确认令牌无效或已过期。")
            proposal = self.store.consume_proposal(token, message.user_id, message.channel, message.chat_id)
            if not proposal:
                return self._out(message, "确认令牌无效、已过期或不属于当前用户。")
            operation, payload = proposal
            if payload.get("requires_preauth") and decision != "确认预授权":
                # The token was consumed; issue a fresh one to avoid ambiguous authorization.
                fresh = self.store.propose(message.user_id, message.channel, message.chat_id, operation, payload)
                return self._out(message, f"此变更包含未来自动执行授权，请明确回复：确认预授权 {fresh}")
            try:
                return self._apply(message, operation, payload)
            except (ValueError, KeyError, TypeError) as exc:
                if str(exc) == "触发时间必须在未来。" and operation in {"create", "update"}:
                    specs = payload.get("specs") or []
                    if len(specs) == 1 and self._looks_scheduled(specs[0].get("content", "")):
                        self.store.save_draft(message.user_id, message.channel, message.chat_id,
                                              specs[0]["content"])
                return self._out(message, f"定时任务信息需要补充：{exc}")
        authorize = re.fullmatch(r"(?:/schedule\s+)?(授权|拒绝)\s+([a-f0-9]{16})", text, re.I)
        if authorize:
            action, run_id = authorize.groups()
            approved = action == "授权"
            ok = self.store.authorize(run_id, message.user_id, approved)
            return self._out(message, ("本次执行已授权，进入执行队列。" if approved else "已拒绝本次执行。") if ok else "授权无效、已过期或不属于当前用户。")
        draft = (self.store.get_draft(message.user_id, message.channel, message.chat_id)
                 if self._looks_time_correction(text) else None)
        if not self._looks_scheduled(text) and draft is None:
            return None
        if re.search(r"\bMCP\b", text, re.I):
            return self._out(message, "当前定时执行尚不支持 MCP Tools。")
        if re.search(r"(?:周[一二三四五六日天]|星期[一二三四五六日天]).{0,4}(?:[0-9一二三四五六七八九十]{1,2}点|[0-9]{1,2}[:：][0-9]{2}).{0,5}(?:完成|交付|提交)", text):
            return self._out(message, "请确认该时间是开始执行，还是要求在此之前完成？")
        request = None
        source_text = draft or text
        try:
            if text.lower().startswith("/schedule "):
                request = self._parse_command(text[10:].strip())
            elif self.parser:
                parse_text = (f"{draft}\n\n触发时间更正（以此为准）：{text}"
                              if draft is not None else text)
                normalized_text = re.sub(r"([一二三四五六七八九十\d]+点半)点", r"\1", parse_text)
                request = self.parser.parse(normalized_text)
                self._preserve_codex_request(request, parse_text)
            else:
                return self._out(message, "自然语言定时任务解析暂不可用，请使用 /schedule 命令。")
            response = self._dispatch(message, request)
            if request.get("operation") in {"create", "update"} and response.text.startswith("待"):
                self.store.clear_draft(message.user_id, message.channel, message.chat_id)
            return response
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if (str(exc) == "触发时间必须在未来。" and isinstance(request, dict)
                    and request.get("operation") in {"create", "update"}):
                self.store.save_draft(message.user_id, message.channel, message.chat_id, source_text)
            return self._out(message, f"定时任务信息需要补充：{exc}")

    @staticmethod
    def _preserve_codex_request(request: dict, original_text: str) -> None:
        if (request.get("operation") not in {"create", "update"}
                or len(request.get("tasks") or []) != 1
                or not re.search(r"(?:用|调用|运行|执行)\s*codex|codex\s*(?:调用|运行|执行)", original_text, re.I)
                or "提醒我" in original_text):
            return
        task = request["tasks"][0]
        # The original text is the authority for tool, skill name and requested
        # output. The model only supplies the schedule date and recurrence.
        task["content"] = original_text
        task["mode"] = "auto"
        scope = task.get("scope") or {}
        task["scope"] = scope
        scope["tools"] = ["codex_apply"]
        if not scope.get("directories"):
            match = re.search(r"在\s*([\w.-]+)目录", original_text, re.I)
            if match:
                scope["directories"] = [match.group(1)]

    @staticmethod
    def _parse_command(text: str) -> dict:
        parts = text.split(None, 1)
        action = parts[0].lower() if parts else ""
        value = parts[1].strip() if len(parts) > 1 else ""
        if action in {"list", "history", "get", "delete"}:
            return {"operation": action, "task_id": value}
        if action == "runs":
            return {"operation": "history", "task_id": value}
        if action in {"create", "update"}:
            obj = json.loads(value)
            if action == "create":
                return {"operation": action, "tasks": obj if isinstance(obj, list) else [obj]}
            return {"operation": action, "task_id": obj["task_id"], "tasks": [obj]}
        raise ValueError("命令支持 /schedule create|update|delete|list|get|history")

    def _dispatch(self, message: IncomingMessage, request: dict) -> OutgoingMessage:
        op = request.get("operation")
        owner = message.user_id
        if op == "clarify":
            return self._out(message, request.get("question") or "请明确触发时间和任务目标。")
        if op == "list":
            rows = self.store.list_tasks(owner, history=False)
            return self._out(message, "当前活跃任务：\n" + ("\n".join(self._format(row) for row in rows) if rows else "无"))
        task_id = str(request.get("task_id") or "").strip()
        if op in {"get", "history", "update", "delete"}:
            if not task_id:
                return self._out(message, "请提供任务 ID。")
            task = self.store.get(task_id, owner)
            if not task:
                return self._out(message, "找不到属于你的该任务。")
            if op == "get":
                return self._out(message, self._format(task))
            if op == "history":
                runs = self.store.history(task_id, owner) or []
                return self._out(message, self._format(task) + "\n执行历史：\n" +
                                 ("\n".join(f"{r['id']} {r['planned_at']} {r['status']} {r['result'] or r['error'] or ''}" for r in runs) if runs else "无"))
            if task["status"] != "active":
                return self._out(message, "该任务已结束，无法修改或删除；可查询历史。")
            if op == "delete":
                payload = {"task_id": task_id, "version": task["version"]}
                token = self.store.propose(owner, message.channel, message.chat_id, op, payload)
                return self._out(message, f"待删除任务：\n{self._format(task)}\n确认删除：确认 {token}")
        if op not in {"create", "update"}:
            raise ValueError("无法识别任务操作。")
        specs = [self._validate(item) for item in request.get("tasks", [])]
        if not specs:
            raise ValueError("没有可创建的任务。")
        if op == "update" and len(specs) != 1:
            raise ValueError("一次只能修改一个任务。")
        payload: dict[str, Any] = {"specs": specs, "requires_preauth": any(s["mode"] == "auto" for s in specs)}
        if op == "update":
            payload.update(task_id=task_id, version=task["version"])
        token = self.store.propose(owner, message.channel, message.chat_id, op, payload)
        summary = "\n".join(f"任务 {i + 1}：{self._format_spec(spec)}" for i, spec in enumerate(specs))
        verb = "创建" if op == "create" else f"修改任务 {task_id}"
        confirmation_word = "确认预授权" if payload["requires_preauth"] else "确认"
        return self._out(message, f"待{verb}：\n{summary}\n请回复：{confirmation_word} {token}\n或：取消 {token}")

    def _apply(self, message: IncomingMessage, operation: str, payload: dict) -> OutgoingMessage:
        owner = message.user_id
        if operation == "create":
            # Validate again: paths may have changed since proposal creation.
            specs = [self._validate(spec) for spec in payload["specs"]]
            ids = self.store.create_many(owner, message.channel, message.chat_id, specs)
            return self._out(message, "已创建任务：" + "、".join(ids))
        task_id = payload["task_id"]
        task = self.store.get(task_id, owner)
        if not task or task["version"] != payload["version"]:
            return self._out(message, "任务已变更或不属于当前用户，请重新发起操作。")
        if operation == "delete":
            ok = self.store.cancel(task_id, owner)
            return self._out(message, f"任务 {task_id} 已取消后续调度。" if ok else "任务已结束。")
        spec = self._validate(payload["specs"][0])
        ok = self.store.update(task_id, owner, spec)
        return self._out(message, f"任务 {task_id} 已更新。" if ok else "任务正在执行或已结束，暂不能修改。")

    def _validate(self, raw: dict) -> dict:
        content = str(raw["content"]).strip()
        if not content or len(content) > 4000:
            raise ValueError("任务内容不能为空且最多 4000 字。")
        moment = datetime.fromisoformat(str(raw["at"]))
        if moment.tzinfo is None:
            raise ValueError("触发时间必须包含时区。")
        if moment <= now_utc():
            raise ValueError("触发时间必须在未来。")
        rule = raw.get("rule") or {"kind": "once"}
        if not isinstance(rule, dict):
            raise ValueError("周期规则格式无效。")
        kind = rule.get("kind", "once")
        if kind not in {"once", "daily", "weekly"}:
            raise ValueError("仅支持单次、每天或每周规则。")
        interval = int(rule.get("interval") or 1)
        if interval < 1 or interval > 52:
            raise ValueError("周期间隔必须在 1 到 52 之间。")
        weekdays = rule.get("weekdays") or []
        if kind == "weekly" and (not weekdays or any(int(day) not in range(7) for day in weekdays)):
            raise ValueError("每周任务必须指定星期几（0=周一）。")
        count = rule.get("count")
        if count is not None and (int(count) < 1 or int(count) > 10000):
            raise ValueError("重复次数必须在 1 到 10000 之间。")
        until = rule.get("until")
        if until:
            until_dt = datetime.fromisoformat(str(until))
            if until_dt.tzinfo is None or until_dt < moment:
                raise ValueError("结束时间需带时区且晚于首次触发。")
            until = iso(until_dt)
        rule = {"kind": kind, "interval": interval, "weekdays": [int(day) for day in weekdays],
                "count": int(count) if count is not None else None, "until": until}
        mode = str(raw.get("mode", "remind"))
        if mode not in MODES:
            raise ValueError("无效权限模式。")
        scope = raw.get("scope") or {}
        if not isinstance(scope, dict):
            raise ValueError("权限范围格式无效。")
        requested_tools = set(scope.get("tools") or [])
        if mode != "remind" and "codex_apply" not in requested_tools and re.search(
            r"(?:修改|删除|重构|修复|提交|发布|完成).{0,30}(?:代码|文件|项目|仓库|README)|执行命令|运行脚本|推送代码",
            content, re.I,
        ):
            raise ValueError("当前定时执行尚不支持代码或已有文件修改、命令及发布操作；可改为仅提醒。")
        if set(scope) - {"tools", "directories", "skills", "mcp_tools"}:
            raise ValueError("权限范围包含未支持的字段。")
        if scope.get("mcp_tools") or (scope.get("skills") and "codex_apply" not in requested_tools):
            raise ValueError("定时执行暂不支持调用 Skills 或 MCP Tools。")
        tools = list(dict.fromkeys(scope.get("tools") or []))
        if set(tools) - TOOLS:
            raise ValueError("工具超出定时执行白名单。")
        if "codex_apply" in tools and (len(tools) != 1 or mode == "remind" or not re.search("codex", content, re.I)):
            raise ValueError("Codex 定时执行需单独授权，且任务内容须明确要求 Codex。")
        directories = []
        for value in scope.get("directories") or []:
            path = Path(str(value)).expanduser().resolve()
            if not path.is_dir() and "codex_apply" in tools:
                roots = tuple(root for root in (CodexRunner.from_env().default_workspace,) if root and root.is_dir())
                matches = WorkspaceResolver(self.policy, search_roots=roots or self.policy.allowed_roots).find(str(value))
                exact = [candidate for candidate in matches if candidate.path.name.casefold() == str(value).casefold()]
                if exact:
                    matches = exact
                if len(matches) != 1:
                    raise ValueError(f"无法唯一确定 Codex 工作区：{value}")
                path = matches[0].path
            if not path.is_dir() or not any(path == root or root in path.parents for root in self.policy.allowed_roots):
                raise ValueError(f"目录不存在或不在系统允许范围：{value}")
            directories.append(str(path))
        if mode == "remind":
            tools = []
            directories = []
        elif not tools:
            raise ValueError("执行模式必须明确列出允许工具；仅提醒模式无需工具。")
        if "codex_apply" in tools and len(directories) != 1:
            raise ValueError("Codex 定时执行必须且只能指定一个工作区。")
        if any(tool.startswith("filesystem_") or tool == "write_report" for tool in tools) and not directories:
            raise ValueError("文件工具必须指定允许目录。")
        missed = str(raw.get("missed_policy", "skip"))
        if missed not in MISSED:
            raise ValueError("无效错过执行策略。")
        grace = int(raw.get("grace_seconds") or 300)
        auth = int(raw.get("auth_seconds") or 900)
        if not 0 <= grace <= 86400 or not 60 <= auth <= 86400:
            raise ValueError("宽限时间或授权有效期超出范围。")
        return {"content": content, "at": iso(moment), "rule": rule, "mode": mode,
                "scope": {"directories": directories, "tools": tools},
                "missed_policy": missed, "grace_seconds": grace, "auth_seconds": auth}

    @staticmethod
    def _format_spec(spec: dict) -> str:
        local = datetime.fromisoformat(spec["at"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        rule = spec["rule"]
        timing = {"once": "单次", "daily": f"每 {rule['interval']} 天", "weekly": f"每 {rule['interval']} 周，星期 {rule['weekdays']}"}[rule["kind"]]
        scope = spec["scope"]
        return (f"{spec['content']}｜首次触发 {local}（Asia/Shanghai）｜{timing}｜"
                f"{MODES[spec['mode']]}｜工具 {scope['tools']}｜目录 {scope['directories']}｜"
                f"错过执行 {MISSED[spec['missed_policy']]}｜授权有效期 {spec['auth_seconds']} 秒")

    @classmethod
    def _format(cls, task: dict) -> str:
        spec = {"content": task["content"], "at": task["next_at"] or task["first_at"],
                "rule": json.loads(task["rule_json"]), "mode": task["mode"],
                "scope": json.loads(task["scope_json"]), "missed_policy": task["missed_policy"],
                "auth_seconds": task["auth_seconds"]}
        counts = (f"｜计划触发 {task['planned_count']} 次｜实际执行 {task['actual_count']} 次"
                  f"｜成功 {task['success_count']} 次｜跳过 {task['skipped_count']} 次")
        return f"{task['id']} [{task['status']}] " + cls._format_spec(spec) + counts
