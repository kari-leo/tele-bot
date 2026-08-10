# tele_bot

一个以飞书为唯一消息入口的本地 Agent 服务。服务接收飞书事件，交给 LangGraph ReAct Agent 处理，再通过飞书回复文本或发送本地文件。

## 功能结构

```text
start.py                         启动 Feishu webhook
app.py                           FastAPI 应用与消息处理编排
tele_bot/channels/feishu.py      飞书事件解析、鉴权、文本/文件发送
tele_bot/agents/                 ReAct Agent 执行器与流式回复
tele_bot/router/                 请求模式路由
tele_bot/tools/                  文件、搜索、报告、Shell 等受控工具
tele_bot/config/                 飞书、LLM、运行时配置
tele_bot/persistence/             SQLite 会话检查点
```

## 配置

至少配置以下文件：

- `tele_bot/config/feishu/local.env`
- `tele_bot/config/llm/local.env`

飞书配置至少包含：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
FEISHU_ENCRYPT_KEY=xxx
FEISHU_WEBHOOK_HOST=127.0.0.1
FEISHU_WEBHOOK_PORT=3000
FEISHU_WEBHOOK_PATH=/feishu/webhook
```

不要将包含密钥的 `local.env` 提交到 Git。

## 启动

使用项目指定的 Conda 环境：

```powershell
& 'D:\Anaconda3\envs\telebot\python.exe' -m pip install -r requirements.txt
& 'D:\Anaconda3\envs\telebot\python.exe' .\start.py feishu-webhook
```

默认本地地址为 `http://127.0.0.1:3000/feishu/webhook`。部署到公网时，将飞书事件订阅 URL 指向对应的公网地址，并订阅 `im.message.receive_v1`。

健康检查：

```text
GET /health
```

## Agent 能力

- 普通对话与多步 ReAct 工作流
- 基础搜索和研究报告输出
- 受控本地文件读取与文件发送到当前飞书会话
- 报告写入及可选 Git 推送确认流程
- 受限 Shell 执行
- SQLite 会话状态持久化
- 受控 Codex CLI（仅显式 `/codex` 命令触发）
- 经确认的文件覆盖和隔离删除

文件发送受允许目录、文件名长度和 30 MB 大小限制保护；工具不会向用户返回文件 key、会话 ID 或底层 API 响应。

## Codex 和文件变更安全边界

Codex 不会被普通自然语言或 Agent 自动调用。只有明确的命令才会进入 Codex 流程：

```text
/codex inspect <任务>
/codex plan <任务>
/codex apply <确认令牌>
```

`inspect` 和 `plan` 使用只读沙箱；覆盖文件和删除文件必须先生成操作摘要，再由同一会话使用确认令牌执行。删除默认移动到隔离目录，不直接永久删除。

Windows 示例：

```env
TELE_BOT_WORKSPACE_ROOT=D:\\files_data\\windborne\\tele_bot
TELE_BOT_ALLOWED_ROOTS=D:\\
TELE_BOT_QUARANTINE_ROOT=D:\\files_data\\windborne\\tele_bot-quarantine
```

Ubuntu 示例：

```env
TELE_BOT_WORKSPACE_ROOT=/srv/telebot/workspace
TELE_BOT_ALLOWED_ROOTS=/srv/telebot/workspace:/srv/telebot/tele_bot-quarantine
TELE_BOT_QUARANTINE_ROOT=/srv/telebot/tele_bot-quarantine
```

Ubuntu 隔离目录需要由运行 telebot 的用户拥有写权限。Codex 进程使用固定工作区、`shell=False`、超时和输出上限；MCP 接入必须复用同一套工作区策略，不能绕过确认层。

## 测试

```powershell
& 'D:\Anaconda3\envs\telebot\python.exe' -m pytest -q
```

当前测试中若出现旧测试与新版 Agent 接口不一致、Linux 固定路径或缺少 chat_id 上下文等失败，应按对应测试和运行环境单独处理。
