"""Render current-request receipts when the model cannot produce a reply."""

from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, ToolMessage


def message_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(part if isinstance(part, str) else part.get("text", "")
                         for part in content if isinstance(part, str) or isinstance(part, dict)
                         and part.get("type") in {"text", "output_text"}).strip()
    return ""


def current_turn(messages, request_id=None):
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage) and (request_id is None or message.id == request_id):
            return messages[index:]
    return [] if request_id is not None else messages


def publication_receipts(messages) -> dict:
    turn = current_turn(messages)
    tools = [message for message in turn if isinstance(message, ToolMessage)]
    pushed = set()
    errors = []
    expected = None
    last_failed = False
    request = next((message_text(m.content) for m in turn if isinstance(m, HumanMessage)), "")
    count = re.search(r"([一二三四五六七八九十\d]+)(?:个|份|篇)[^，。\n]{0,10}?文档", request)
    if count:
        numeral = count.group(1)
        expected = int(numeral) if numeral.isdigit() else {
            "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
            "七": 7, "八": 8, "九": 9, "十": 10}.get(numeral)
    for result in tools:
        text = message_text(result.content)
        last_failed = result.status == "error"
        if result.status == "error":
            errors.append(f"{result.name or '工具'}：{text.splitlines()[0][:400]}")
            continue
        if result.name == "blog_publish_files":
            try:
                receipt = json.loads(text)
            except (ValueError, TypeError):
                continue
            requested_count = receipt.get("requested_count")
            if isinstance(requested_count, int):
                expected = max(expected or 0, requested_count)
            if receipt.get("status") == "pushed":
                pushed.update(item["path"] for item in receipt.get("files", []) if item.get("path"))
            if receipt.get("error"):
                errors.append(str(receipt["error"])[:400])
                last_failed = True
        elif result.name in {"blog_push", "blog_publish", "blog_publish_from_file"} and text.startswith("git push 已完成"):
            match = re.search(r"^文件：(.+)$", text, re.M)
            if match:
                pushed.update(match.group(1).split(", "))
    return {"expected": expected, "pushed": pushed, "errors": errors, "has_tools": bool(tools),
            "last_failed": last_failed,
            "has_publication": any(result.name in {"blog_publish_files", "blog_push", "blog_publish",
                                                   "blog_publish_from_file"} for result in tools)}


def incomplete_reply(messages, *, reason="模型未返回有效回复") -> str:
    receipts = publication_receipts(messages)
    expected, pushed, errors = receipts["expected"], receipts["pushed"], receipts["errors"]
    confirmed_all = bool(expected and len(pushed) == expected)
    lines = [f"{reason}；" + ("以下为本轮工具记录确认的推送结果。" if confirmed_all
                              else "本次请求尚未确认完成。")]
    if expected:
        remaining = "" if confirmed_all else f"；其余 {max(0, expected - len(pushed))} 份尚未确认完成"
        lines.append(f"本轮已确认推送 {len(pushed)}/{expected} 份文档{remaining}。")
    if pushed:
        lines.append("已确认推送的文件：\n" + "\n".join(f"- {path}" for path in sorted(pushed)[:10]))
    elif receipts["has_tools"]:
        lines.append("已保留本轮工具执行记录；不能据此认定整个任务已完成。")
    else:
        lines.append("本轮尚未记录工具执行结果。")
    if errors and not confirmed_all:
        lines.append("最近的工具错误：\n" + "\n".join(errors[-3:]))
    return "\n".join(lines)
