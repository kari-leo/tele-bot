"""Trusted Base confirmation/status routes and exact receipt rendering."""
import json
import re
import time

from tele_bot.bitable.models import ACTION_LABELS, BitableError, preview_body
from tele_bot.models import CardAction, InteractiveCard, OutgoingMessage


def render_bitable_receipt(message, result):
    status, operation = result.get("status"), result.get("operation_id", "")
    if status == "awaiting_confirmation":
        plan = result["preview"]
        # SDK/schema internals are not meaningful in the user's confirmation flow.
        body = preview_body(plan, result["action"]) + "\n\n点击下方按钮，或直接回复“确认”执行；回复“取消”放弃。"
        token = result["confirmation_token"]
        return OutgoingMessage(message.channel, message.chat_id,
                               f"待确认表格操作\n{body}\n备用指令：/bitable confirm {token}",
                               reply_to_message_id=message.message_id,
                               card=InteractiveCard("多维表格操作确认", body, "pending", (
                                   CardAction("bitable_confirm", "确认", token),
                                   CardAction("bitable_cancel", "取消", token)),
                                   metadata={"operation_id": operation}))
    action = ACTION_LABELS.get(result.get("action"), "处理")
    label = {"succeeded": f"✅ 已{action} {result.get('succeeded_count', 0)} 条记录",
             "partial": f"⚠️ 部分完成：已{action} {result.get('succeeded_count', 0)} 条记录",
             "unknown": "⚠️ 结果未确定，请先核对，避免重复提交",
             "cancelled": "已取消，本次操作未执行", "conflict": "操作中止：待修改内容或表结构发生变化",
             "expired": "确认已过期，请重新提出修改要求"}.get(status, "执行失败")
    if status == "conflict" and result.get("submitted"):
        label = "⚠️ 已提交，但回读核验不一致，请核对实际记录"
    if result.get("error") == "unauthorized":
        label = "无权限"
    lines = [label]
    if result.get("message"):
        lines.append(result["message"])
    summary = result.get("change_summary")
    if summary:
        if status != "succeeded":
            lines.append("\n本次申请的变更（不代表均已执行）：")
        lines.append("\n" + summary)
    if status == "succeeded" and result.get("verification") == "verified":
        lines.append("\n已回读核对修改结果。")
    if status not in {"succeeded", "cancelled"} and operation:
        lines.append(f"\n查询状态：/bitable status {operation}")
    text = "\n".join(lines)
    terminal_card = None
    if message.card_action is not None and operation and len(text) <= 4000:
        card_status = "success" if status == "succeeded" else "cancelled" if status in {"cancelled", "expired"} else "failed"
        terminal_card = InteractiveCard("多维表格操作结果", text, card_status,
                                        metadata={"operation_id": operation, "status": status})
    return OutgoingMessage(message.channel, message.chat_id, text,
                           reply_to_message_id=message.message_id, card=terminal_card)


class BitableCommandService:
    def __init__(self, service):
        self.service = service

    def handle(self, message):
        if message.text.strip() == "/bitable whoami":
            return OutgoingMessage(message.channel, message.chat_id,
                                   f"飞书用户：{message.user_id}\n当前会话：{message.chat_id}")
        parts = message.text.strip().split()
        if parts[:2] == ["/bitable", "list"]:
            if len(parts) > 3 or (len(parts) == 3 and (len(parts[2]) > 3 or not parts[2].isascii() or not parts[2].isdigit())):
                return OutgoingMessage(message.channel, message.chat_id, "用法：/bitable list [页码]")
            page = int(parts[2]) if len(parts) == 3 else 1
            try:
                result = self.service.inspect("", message, page=page)
            except BitableError as exc:
                return render_bitable_receipt(message, exc.result())
            targets = result["targets"]
            pages = result["pages"]
            start = (page - 1) * 5
            lines = [f"{i}. {t['base_name']} / {t['table_name']}（{t['permission_label']}；别名：{t['alias']}）"
                     for i, t in enumerate(targets, start + 1)]
            text = ("当前已知的多维表格：\n" + "\n".join(lines) if lines else
                    "当前尚未发现多维表格，可发送 Base 链接进行自动识别。")
            text += "\n此清单仅覆盖已知资源，不代表 Friday 的全部可访问表。权限来自飞书查询，未核实不等于无权限。"
            if pages > 1:
                text += f"\n第 {page}/{pages} 页，共 {result['total']} 个数据表；/bitable list <页码> 翻页。"
            return OutgoingMessage(message.channel, message.chat_id, text)
        action = message.card_action
        if action is not None and action.action_id in {"bitable_confirm", "bitable_cancel"}:
            verb, token = ("cancel" if action.action_id == "bitable_cancel" else "confirm"), action.value
        elif message.text.strip().startswith("/bitable "):
            parts = message.text.strip().split()
            if len(parts) != 3 or parts[1] not in {"confirm", "cancel", "status"}:
                return OutgoingMessage(message.channel, message.chat_id,
                                       "用法：/bitable whoami；/bitable list；/bitable confirm|cancel|status <确认令牌或操作 ID>")
            _, verb, token = parts
        else:
            normalized = re.sub(r"[\s，,。.!！]", "", message.text).lower()
            confirm = {"确认", "确定", "确认执行", "确认修改", "确认提交", "按预览修改", "确认按预览修改", "确认按预览执行", "confirm"}
            cancel = {"取消", "取消操作", "取消修改", "不用了", "先不改了", "算了", "cancel"}
            if normalized not in confirm | cancel or self.service.store is None:
                return None
            rows = self.service.store.pending(message.user_id, message.chat_id, self.service.version)
            active = [r for r in rows if r["expires"] > time.time()]
            if not rows:
                return None
            if not active:
                return render_bitable_receipt(message, self.service.receipt(rows[0]))
            if len(active) != 1:
                lines = ["有多个待确认的表格操作，请点击对应卡片的按钮，或使用以下命令选择："]
                verb = "cancel" if normalized in cancel else "confirm"
                for row in active:
                    plan = json.loads(row["plan"])
                    lines.append(f"- {plan.get('table_name') or plan['alias']}：{ACTION_LABELS[row['action']]} {len(plan['records'])} 条记录\n"
                                 f"  /bitable {verb} {row['token']}")
                return OutgoingMessage(message.channel, message.chat_id, "\n".join(lines))
            verb, token = ("cancel" if normalized in cancel else "confirm"), active[0]["token"]
        try:
            if verb == "status":
                row = self.service.store.get(token)
                # Reuse authorization checks without triggering execution.
                plan = json.loads(row["plan"])
                self.service._target(plan["alias"], message, row["action"])
                if (row["user_id"], row["chat_id"], row["policy"]) != (message.user_id, message.chat_id, self.service.version):
                    raise BitableError("unauthorized", "无权查看此操作")
                result = self.service.receipt(row)
            else:
                result = self.service.confirm(token, message, cancel=verb == "cancel")
        except BitableError as exc:
            result = exc.result()
        except Exception:
            result = {"status": "failed", "message": "表格配置或操作状态不可用"}
        return render_bitable_receipt(message, result)
