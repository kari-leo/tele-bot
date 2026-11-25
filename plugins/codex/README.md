# Telebot Codex

复用已登录的本机 Codex 0.160.0，提供 inspect/plan/apply、原生历史、受控续聊、Skill 副本和核验产物。先装 core，再选择 desktop 或 feishu 入口。

inspect/plan 强制只读，飞书 apply 使用一次性确认，原生所有者按入口策略授权。飞书历史仅供配置的本人私聊。发送 `/codex history [关键词]` 或 `/codex history --archived [关键词]` 打开卡片，可点查看、翻页、刷新和继续会话。仍可用 `/codex history <序号>`、`/codex history --next`、`/codex read <会话ID>` 和 `/codex read --next` 操作。

在 Harness 的「Codex 插件配置」中填写「插件新会话模型」，让插件新建会话时使用 CLI 可用的模型 ID。留空时继承 CLI 默认模型。此设置也适用于定时 Codex 任务，续聊仍沿用原会话模型。

点「继续此会话」后，在 10 分钟内直接发送一条文字任务或回复新卡片，也可在本人私聊发送 `/codex resume <序号或会话ID> <任务>`。发送「取消」可退出续聊，`/new` 可另开会话。未点继续时，开始普通对话会使旧卡片失效。原工作区须存在并在 Codex 配置范围内，归档会话只能读。续聊保留原会话与模型，重新设定仅工作区写入权限并关闭 Shell 网络。同一条消息只派发一次。数据在 DSH_HOME/plugins/telebot-codex。

原会话由 Codex 客户端占用时，飞书会提示本轮尚未开始。关闭占用它的客户端后可重新发起，或选择未打开的会话。
