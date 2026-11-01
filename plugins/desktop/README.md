# 原生桌面对话入口

安装到官方 Harness 0.2.1-rc.5 的 Web/Desktop profile，依赖 core，业务包按需安装，无需飞书。仅在官方 Connection 限定本机的组合中启用；使用 SessionController 经认证的 owner 请求事实，不接受工具参数声明身份。

默认完整权限仅适用于可信原生入口的已配置插件能力；不改变飞书策略或宿主全局权限。不增加账户或修改全局 Codex 配置。明确的 inspect/plan 仍只读；写入/发布/auto 任务由桌面策略预授权，保留冻结/防重/unknown 账本。配置页 `/telebot/desktop/setup` 可主动改为受限模式。

`telebot_desktop` 工具管理 Codex 历史/续聊、发布状态和任务查询/停止等操作；其他业务工具直接可用。文件通过官方附件服务呈现，定时结果回到原原生会话；缺失回执不盲重做业务。
