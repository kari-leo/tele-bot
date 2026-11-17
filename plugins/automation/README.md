# Telebot Automation

先装 core，复用官方 schedule 唯一时钟，支持 remind/auto/confirm、时区和补跑；任务在独立会话执行。

飞书任务变更和 auto 执行授权分开，confirm 逐次确认。默认允许授权用户使用实际存在目录，显式受限原生入口仍受限。research 禁止 Shell/MCP/发布/任意飞书写入，codex_apply 复用 Codex 执行器；投递失败不重做业务。数据在 DSH_HOME/plugins/telebot-automation。

## 模型工具参数

`telebot_schedule` 使用结构化 `task`、`task_id` 和 `mutations`。创建任务的 `task` 必须包含 `title`、`prompt`、`mode`、`executor`、`catchUp`、`rule`；时间放在 `rule.kind/first/timeZone/interval`，不用 `at` 或 `cron`。`first` 必须带时区偏移。修改使用 `task_id` 加完整替换 `task`，删除及申请执行授权只传 `task_id`。批量使用 `mutations: [{action, task_id?, task?}]`。

旧 `input_json` 兼容原有结构，但不能与结构化参数混用。执行预授权、逐次确认、身份绑定、冻结与未知结果保护不变。
