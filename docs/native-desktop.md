# 原生桌面对话入口

安装 `core + desktop + 所需业务包`，在官方 Web/桌面的普通会话中使用；无需安装飞书、填写飞书身份或申请 OAuth。原生入口本轮适配 Codex/adviser/publishing/automation；统一飞书包内的资源业务仍通过飞书入口使用，知识库使用上游自己的原生工具。沿用官方模型、Agent、会话、提问、附件及计时服务，没有另建聊天前端。

`@telebot/desktop` 安装后默认停用，首次需在官方插件管理器启用 `telebot-desktop` 行。它的业务配置默认 `fullAccess: true`：经官方本机 RPC 认证的宿主所有者无需反复确认 Codex apply/续聊、发布或 auto 定时任务。`inspect`/`plan` 始终只读，显式 `confirm` 定时任务仍逐次确认；OS、当前账户、官方宿主强制策略继续生效。插件不自动改变 Harness 自带 Shell 的全局权限预设。

身份取自当前执行轮的官方 SessionController 来源记录：`kind=user`、有效 `rpcId`、无非所有者 principal；已结束的轮次、飞书/ACP、子智能体、任务通知均不能凭文字或工具参数获得所有者权限。仅接受 loopback 宿主；需公网飞书时使用独立 webhook 监听服务。两个入口共用业务实现，但管理员、允许目录和确认规则相互隔离。

配置位于 `$DSH_HOME/plugins/telebot-desktop/config/`；本机设置网页地址与能力令牌的打开方法见[安装指南](installation.md)。可关闭完整权限并填写受限目录，此后写入冻结提案需通过原生提问确认。身份及投递账本默认在同一插件目录的 `desktop.sqlite`，可在 UI 修改数据目录；迁移前停机备份。执行中拒绝切换策略。既有任务在执行/投递前重新核对入口策略，不能沿用已撤销的完整权限。

在原生会话中可说：

- “用本机 Codex 检查这个项目，只分析不修改。”→ `telebot_codex` inspect。
- “用 Codex 修复这个问题并给出改动文件。”→ apply；默认完整权限，无逐次授权。
- “把这份原文发布到配置的文章目录。”→ `publish_source`，仍保持原文、新文件和指定 Git 文件规则。
- “明天下午三点提醒我……”→ `telebot_schedule`，由官方计时器唤醒。
- “列出定时任务 / 看这个 Codex 线程 / 继续上次线程。”→ `telebot_desktop` 控制入口；模型是否选中工具仍取决于所选模型及当前 preset 的工具目录。

后台结果回到创建任务的原生 Session；文件先核对大小和 SHA-256，再交给官方附件服务。投递账本独立于业务执行，重复投递复用回执；发送状态不明不会盲目重发。只有明确失败/尚未发送的项可以请求重试，不能由投递失败触发重新执行代码或发布。附件呈现失败时保留原业务回执、路径和哈希，不会自动重跑业务。

当前 Windows 验证使用外部官方 rc.5 Web 的真实所有者 RPC 和与桌面共用的 SessionController；正式 Electron 插件已安装并通过启动落盘检查，正式 UI 和实际账户业务仍待验收。具体已通过项目与限制以[状态页](status.md)为准，不能把 Web 验证当作桌面已安装。
