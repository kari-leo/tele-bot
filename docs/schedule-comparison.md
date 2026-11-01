# 官方计时与 Telebot 自动化

固定目标官方 0.2.1-rc.5。以下依据安装包公开 schedule/SessionController API 与本项目代码。

| 边界 | 官方 schedule | Telebot automation 插件 |
|---|---|---|
| 到点行为 | 将提示持久投入绑定 Session，必要时冷恢复 | 只接收结构化计时回执；业务执行另由冻结 Actor/任务授权决定 |
| 规则 | 相对延时、时刻、间隔、daily/weekly、cron | 保留旧 once/daily/weekly、间隔 N 天/周、count/until、时区/DST；用官方一次性计时串联，无扫描时钟 |
| 授权 | Session/工具权限 | 创建确认与 auto 单独预授权分开；confirm 每次 execute/reject；授权绑定用户/会话/cwd/冻结请求 |
| 遗漏与并发 | 按原生规则投递 | 默认迟到 5 分钟，skip/run_once/ask，逐次遗漏审计、最多补跑最新一次；串行、重叠跳过 |
| 回执 | inbox 已持久接收，不证明业务成功 | 独立执行/产物/逐文件投递状态；取消/超时/unknown；投递失败不重做业务 |
| 删除/恢复 | 删除计时任务会删原生投递历史；inbox/task 两次提交存在崩溃窗口 | 独立审计与 occurrence 防重，重启 running→unknown，不自动再执行；官方任务外部修改会暂停旧授权 |

现在可以在飞书自然语言提出“在 XXX 时做 XXX”：模型通过 `telebot_schedule` 提交结构化提案，真实用户确认创建；auto 再单独预授权，confirm 到点按次确认。业务执行使用受限官方研究 Agent 或确认过的本机 Codex，结果和产物交给持久 outbox。宿主须持续运行，真实飞书触发/通知仍待租户验收。

命令：`/schedule create/update/delete/batch`、`confirm/cancel`、`authorize`、`execute/reject`、`stop`、`list/get/history/status/retry-delivery`。创建确认不会自动授予执行权限；原生 schedule 投递成功也不等于业务成功。

普通 `/remind` 继续只发送提醒。它和 automation 的唤醒 Session 都用持久受限 preset，冷恢复也没有 Shell/业务工具，且不调用模型；P0 专项检查公开 SessionController 的真实冷恢复。没有增加第二套 Agent 循环或计时器。

自然语言到提案、计时、auto/confirm 执行及产物交付已在实际官方宿主 + loopback 模型/飞书下组合验证；当前数量和真实边界见[状态页](status.md)。生产旧任务不自动迁移，切换须核对归属和 unknown，确保只有一个写者。
