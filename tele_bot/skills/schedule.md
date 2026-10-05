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

该工具没有参数，使用当前已认证的用户原话和会话身份。不要改写用户的确认内容。工具返回
待确认摘要、令牌、任务 ID 或错误时，将结果原样答复用户，不得自行声称任务已创建或已执行。
