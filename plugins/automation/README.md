# Telebot Automation

先装 core，复用官方 schedule 唯一时钟，支持 remind/auto/confirm、时区和补跑；任务在独立会话执行。

飞书任务变更和 auto 执行授权分开，confirm 逐次确认。默认允许授权用户使用实际存在目录，显式受限原生入口仍受限。research 禁止 Shell/MCP/发布/任意飞书写入，codex_apply 复用 Codex 执行器；投递失败不重做业务。数据在 DSH_HOME/plugins/telebot-automation。
