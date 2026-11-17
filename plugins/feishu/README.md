# Telebot Feishu

统一飞书渠道、模型主动消息和文档、表格、日历、任务业务，先安装 core；原生桌面另安装 desktop。不再单独安装 feishu-tools。使用官方原生设置页及 credentials。

feishu_messages 发现已接入会话、立即发送文本/Markdown、查询回执及重试明确失败项；桌面完整权限可选择目标，飞书限当前 chat/thread。复用渠道 outbox 和 worker，已排队不等于已送达，unknown 不盲重试。

资源只用应用身份。可信桌面完整权限使用内部授权冻结并核验写入；飞书仍校验 sender/operator、管理员及资源范围。多维表格记录新增和修改（含多条）直接执行并核验；删除及其他资源写入保留冻结预览，15 分钟内本人确认后一次执行。支持自然语言批量确认；混合消息中的其他内容持久化后继续进入官方对话。逐项富文本卡片回执进入后续上下文，原始用户消息保留。数据默认在 DSH_HOME/plugins/telebot-feishu，卸载保留。
