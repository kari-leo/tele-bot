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
# 允许创建知识库项目空间的飞书 user_id/open_id，多个值用英文逗号分隔
KB_CREATOR_USER_IDS=u_123456,ou_abcdef
```

不要将包含密钥的 `local.env` 提交到 Git。

### 知识库 MCP

知识库同时提供本地 stdio MCP Server。它与飞书入口共用同一个 `KB_SQLITE_PATH`，
并按项目空间成员关系鉴权。在 `tele_bot/config/feishu/local.env` 固定 MCP 身份：

```env
KB_MCP_USER_ID=ou_xxx
```

启动命令：

```powershell
& 'D:\Anaconda3\envs\telebot\python.exe' .\start.py kb-mcp
```

MCP Host 也可以直接以模块方式启动：

```json
{
  "mcpServers": {
    "tele-bot-kb": {
      "command": "D:\\Anaconda3\\envs\\telebot\\python.exe",
      "args": ["-m", "tele_bot.mcp_server"],
      "cwd": "D:\\files_data\\windborne\\tele_bot"
    }
  }
}
```

提供 `kb_list_spaces`、`kb_search`、`kb_list_documents` 和 `kb_budget` 四个只读工具。
MCP 工具不能传入用户 ID，身份只能由服务端的 `KB_MCP_USER_ID` 决定；导入、删除、
成员管理和目录绑定仍必须走飞书确认流程。

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

Codex 按飞书 chat 维护短期上下文：plan 的结果会随确认令牌传入 apply，后续
`/codex inspect`、`/codex plan` 或直接 `/codex apply <任务>` 会继承最近一次工作区和
Codex 结果。可在 `local.env` 中用 `TELE_BOT_CODEX_WORKSPACE_ROOT` 指定不带目录时的
默认项目目录；显式写在命令中的目录优先级更高。

Windows 未配置时，Codex 默认工作目录为 `D:\files_data\`。自然语言中的模糊目录不会
被正则直接猜测；系统会在允许根目录内寻找候选，出现多个候选时先要求用户选择。

`inspect` 和 `plan` 使用只读沙箱；覆盖文件和删除文件必须先生成操作摘要，再由同一会话使用确认令牌执行。删除默认移动到隔离目录，不直接永久删除。

Windows 示例：

```env
TELE_BOT_WORKSPACE_ROOT=D:\\files_data\\windborne\\tele_bot
TELE_BOT_ALLOWED_ROOTS=D:\\files_data
TELE_BOT_QUARANTINE_ROOT=D:\\files_data\\windborne\\tele_bot-quarantine
TELE_BOT_CODEX_WORKSPACE_ROOT=D:\\files_data\\
```

Ubuntu 示例：

```env
TELE_BOT_WORKSPACE_ROOT=/srv/telebot/workspace
TELE_BOT_ALLOWED_ROOTS=/srv/telebot/workspace:/srv/telebot/tele_bot-quarantine
TELE_BOT_QUARANTINE_ROOT=/srv/telebot/tele_bot-quarantine
```

Ubuntu 隔离目录需要由运行 telebot 的用户拥有写权限。Codex 进程使用固定工作区、`shell=False`、超时和输出上限；MCP 接入必须复用同一套工作区策略，不能绕过确认层。

## 项目知识库 MVP

知识库项目空间与飞书 chat 相互独立；创建者自动成为空间管理员。任何读取都同时
校验当前 chat 绑定的空间和提问者成员资格，不支持跨空间全局搜索。只有在飞书配置
文件 `tele_bot/config/feishu/local.env` 的 `KB_CREATOR_USER_IDS` 中列出的用户可以
创建空间；空间可绑定到多个群聊或私聊，每个
会话同一时间只绑定一个空间。管理员使用以下显式命令：

```text
/kb whoami
/kb create [空间名]
/kb spaces
/kb use <空间ID或名称>
/kb bind <允许导入的目录>
/kb import <文件、目录或模糊目录描述>
/kb import-doc <飞书 docx 链接>
/kb add-member <user_id> [member|admin]
/kb delete <完整文档 ID>
```

成员可使用 `/kb search <问题>`、`/kb list` 和 `/kb budget`。绑定目录、导入、成员
变更和删除都会先返回确认卡片；确认令牌绑定原 chat、用户和短期操作，仍可回复
“确认”或“取消”作为兼容回退。飞书上传的文件同样先确认再入库。

`/kb spaces` 列出当前用户有权读取的空间；用户可在另一个群聊或私聊中通过
`/kb use <空间ID或名称>` 将该会话绑定到同一空间。绑定不会自动授权群内其他用户，
每个提问者仍必须是该空间成员。在 `tele_bot/config/feishu/local.env` 中配置创建权限：

```env
KB_CREATOR_USER_IDS=u_123456,ou_abcdef
```

可先发送 `/kb whoami` 查看应写入白名单的飞书用户标识，修改配置后需重启服务。

`/kb import` 可以接收精确文件、精确目录，也可以接收目录层级线索，例如：

```text
/kb import 将 files_data 目录下的 Job 目录下的八股目录里的文档全部导入
```

模糊解析只搜索当前空间已绑定目录；唯一匹配后递归收集支持格式并展示确认清单。
存在多个匹配目录时不会猜测，必须补充更精确的目录名或路径。单次最多导入 200 个
文件，隐藏目录、Git 元数据、依赖目录和非白名单文件会跳过。

支持 Markdown、TXT、PDF 和 DOCX；压缩包、可执行文件和未知二进制会拒绝。图片只
归档事件元数据并明确提示“一期不支持 OCR”，不会传给 Embedding API。文件必须在
管理员绑定且同时通过 `WorkspacePolicy` 的目录内，默认单空间最多 10,000 个分块。

知识库向量由百炼 OpenAI 兼容 `/embeddings` 接口生成，默认模型
`text-embedding-v4`、1024 维。文档分块和查询文本会发送给百炼；聊天模型凭据与
Embedding 凭据相互独立。请在 `tele_bot/config/llm/local.env` 配置：

```env
ALIBAILIAN_EMBEDDING_API_KEY=
ALIBAILIAN_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ALIBAILIAN_EMBEDDING_MODEL=text-embedding-v4
ALIBAILIAN_EMBEDDING_DIMENSIONS=1024
ALIBAILIAN_EMBEDDING_DAILY_TOKEN_BUDGET=1000000
ALIBAILIAN_EMBEDDING_DAILY_COST_BUDGET_CNY=1.0
KB_SQLITE_PATH=data/knowledge.sqlite
KB_MAX_CHUNKS_PER_SPACE=10000
```

系统按文档和分块 SHA-256 去重，未变化内容不会再次请求 Embedding；达到每日 token
或金额预算后，索引作业暂停并保留失败原因。估算金额默认按 ￥0.00025/千 token
记录，仅用于本地预算门，实际费用以百炼所在区域、业务空间和账单为准。

飞书应用至少需要消息读取/发送、消息资源下载以及云文档只读权限。遵循最小权限，
云文档不会自动同步，仅管理员显式导入 `/docx/` 链接时读取一次。备份或恢复时同时
处理会话 SQLite 与 `KB_SQLITE_PATH`；数据库启用 WAL，复制前应先停止服务或使用
SQLite 在线备份。

## 测试

```powershell
& 'D:\Anaconda3\envs\telebot\python.exe' -m pytest -q
```

当前测试中若出现旧测试与新版 Agent 接口不一致、Linux 固定路径或缺少 chat_id 上下文等失败，应按对应测试和运行环境单独处理。
