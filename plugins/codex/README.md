# Telebot Codex

复用已登录的本机 Codex 0.160.0，提供 inspect/plan/apply、原生历史、受控续聊、Skill 副本和核验产物。先装 core，再选择 desktop 或 feishu 入口。

inspect/plan 强制只读；飞书写入与续聊须一次性确认，原生所有者按入口策略授权。历史不继承旧宽权限，unknown 不重交，投递失败不重跑。数据在 DSH_HOME/plugins/telebot-codex。
