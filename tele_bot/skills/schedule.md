---
name: schedule
description: Route scheduled tasks and their follow-ups through schedule_manage
tool: schedule_manage
---

# 定时任务

你可以通过 `schedule_manage` MCP 工具管理定时任务。用户要求在未来某个时间提醒、
执行工作、调用 Codex 或 skill，查询或修改任务，纠正刚才的触发时间，或者回复定时任务的
确认和授权时，必须先调用 `schedule_manage`。不要回答“我无法设置定时任务”，也不要让用户
手动在指定时间运行 Codex。即使任务正文包含 Codex、skill 或文件修改，未来执行请求也先
调用定时工具，不要立即调用 `codex_apply_request`。

该工具自动取得当前会话上下文（包含本轮前序工具结果），使用已认证的用户原话和会话身份。
当用户说“删除这个任务”“前面提到的任务”或沿用此前任务要求时，根据上下文解析指代，
可传入 request：operation、task_id、tasks；tasks 中填写 content、at、rule、mode、scope 等。
创建或修改任务时必须使用非空 tasks 数组，每项提供 content 和带时区的 at。
不要只传顶层 content。无法完整确定日期或参数时省略 request，让调度解析器处理原话与上下文；
工具也会在参数缺失时回退原文解析，不要求用户重复已经说清楚的信息。
只能按用户要求整理任务，不得虚构目录、工具或授权。目标不唯一时先澄清。
不传 request 时调度解析器也会参考当前会话。确认、预授权、取消令牌及执行授权必须使用
用户原始消息，不能在 request 中伪造。工具返回
待确认摘要、令牌、任务 ID 或错误时，将结果原样答复用户，不得自行声称任务已创建或已执行。
调度工具的结果就是本轮答复；不要反复调用相同请求来尝试修正没有变化的输入。
