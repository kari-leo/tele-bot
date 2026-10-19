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

ReAct 每次请求最多执行 15 轮模型调用，达到上限会明确返回未完成提示。
`filesystem_read_file` 支持从 `start_line`（从 1 开始）分页读取；当返回
`truncated: true` 时，用 `next_start_line` 继续读取，直到该字段为 `null`。
每页最多 500 行，模型侧输出通常不超过 8,000 字符；分页会保留完整 JSON 和完整行。
单行超过字符预算时会完整返回该行，以保证续读能继续前进。

### 会话上下文与执行记录

完整消息和工具结果继续保存在 `data/conversations.sqlite` 的 LangGraph 检查点中。
数据库使用 WAL，最近的记录也可能位于同目录的 `conversations.sqlite-wal`。
定时任务的业务执行记录位于 `data/scheduled.sqlite`；启动与异常日志输出到服务控制台。

每次模型调用前，`tele_bot/workflows/context_manager.py` 单独构建受限上下文：

- 输入预算按 token 保守估算，超过 24,000 时压缩，目标 16,000，硬上限 32,000；系统提示和工具定义计入预算。
- 摘要最多约 1,200 tokens，保留最近 6 个完整消息组；并行工具请求及对应结果一起保留或一起压缩。当前用户请求始终保留，必要时继续缩小窗口。
- 摘要记录目标、约束、已完成操作、失败与下一步；旧文件正文压缩为读取凭据，路径、行数和续读游标另存。
- `context_summary`、`summary_until_message_id`、`context_refs` 和 token 校准系数随检查点保存。摘要节点先保存，再调用主模型，因此 API 超时后不用重新压缩全部历史。
- 第一次遇到旧长会话自动迁移模型视图，原始记录保留。摘要按不超过约 8,000 tokens 的小批次生成，只处理游标之后新增的旧消息。
- 摘要请求超时最多 30 秒且不重试；失败时保留已有摘要、使用有界凭据降级，仍执行输入上限。当前请求或固定提示本身超限时明确报错。
- 压缩历史时，飞书状态消息显示开始整理、正在生成第几批摘要和整理完成；摘要失败时显示使用精简历史继续处理，随后恢复正常思考轮次提示。

中文文本使用保守估算，并根据服务端返回的实际输入 token 用量向上校准。
普通会话主模型最多输出 4,096 tokens，摘要最多输出 1,200 tokens；主模型超时采用
`ALIBAILIAN_TIMEOUT_SECONDS`。这些预算在 `ContextPolicy` 中集中配置，无需向量库或新数据库。
定时任务也使用同一上下文管理器，其业务记录仍单独保存。

`schedule_manage` 自动取得该次调用的模型上下文，包括历史摘要、最近的用户消息、任务列表
和本轮此前的工具结果。调度解析器使用最多约 8,000 tokens 的参考记录，保留摘要与最近记录，
以当前原始用户消息为准解析“删除这个任务”等指代。主模型也可通过 `request` 传入整理后的
`operation`、`task_id`、`tasks`（任务内容、时间、周期与范围），减少重复解析。
这些参数只生成提案；身份、确认令牌与执行授权仍取自真实消息，目标不明确时返回澄清。
工具 schema 明确列出任务数组及其字段；创建和修改须在 `tasks` 中填写 `content` 和带时区的
`at`。模型只提供顶层正文、空对象或缺少必要字段时，自动回退到原始消息和会话解析一次；
不会把模型漏填字段当作用户没有提供任务。显式 `/schedule` JSON 命令仍按原命令校验。
单独调用调度工具后，图直接以工具结果结束该轮，保留摘要和令牌，避免重复调用相同请求。

### 按源文件发布原文

新增 `blog_publish_from_file`，直接传入 `source_path`；无需先分页读完或让模型复述正文。
源文件必须是允许目录内的 UTF-8 文件。工具只替换已有 YAML frontmatter、生成元数据，
正文的换行、空白和内容原样保留，输出最多 400,000 字符且不覆盖已有文件。
同一集合内相同正文会复用已有文件，即使换了 slug，也不会重复写入；重启后仍可识别。

```json
{
  "slug": "product-case",
  "source_path": "D:/files_data/Job/产品经理/作品集/telebot/TeleBot-产品案例-v2.md",
  "title": "TeleBot 产品案例",
  "description": "完整产品案例原文",
  "project": "telebot",
  "order": 1
}
```

设置 `project` 会生成 `src/content/projectDocuments/<project>/<slug>.md`，归属现有作品集项目；
项目必须已在 `src/content/projects/<project>.md` 存在。留空则写入普通 posts 集合，需提供
`description`，`published` 默认为当天。博客位置沿用 `WINDBORNE_POSTS_DIR` 配置。
用户明确要求“推出去、推送、发布到博客”时，设置 `git_push_enabled=true` 可直接提交和推送；
授权来自执行器传入的当前用户消息，工具参数本身不能授予推送权限。
用户只要求写入时保持该参数为 `false`。没有明确推送指令时沿用确认令牌流程，
同一会话的多份文件令牌独立保存，互不覆盖。

`blog_push(file_paths=[...])` 专门推送已写入的文件，支持多份文档一次提交和推送，
不会生成任何新文档。发布多份文档时优先先写入，再批量推送。网络失败后用原路径重试一次，
仍失败就报告失败并停止；不要换 slug 重写。成功提交后重试只推送，已提交的文件也不会
因“没有新改动”再次提交失败。Git 操作按仓库串行执行，提交只包含指定文件，单次命令最多等待 90 秒。

多份原文优先使用 `blog_publish_files(documents=[...], project="telebot", git_push_enabled=true)`。
每项包含 `source_path`、`slug`，可提供 `title`、`description`、`order`。它核对本次要求的
全部源文件，不依赖历史路径；全部写入或复用成功后，统一提交和推送。返回每份文件的路径和
`requested_count`、`written_count`、`pushed_count`，任一写入失败都不会推送残缺批次。

模型返回空内容且没有工具调用时，不再视作正常完成：先显示恢复提示，并在 15 轮总预算内
重试一次，要求继续核对当前请求的未完成部分。再次为空或恢复调用失败时，根据本轮工具记录
返回已确认的推送、未确认完成的部分和错误；不会显示“无回复”或借用历史任务的成功结论。
对于明确要求推送多份文档的请求，结束前还会核对本轮已确认的推送数量；只有部分文档完成时
最多尝试一次继续处理，仍不足则返回实际数量和未完成部分。已明确失败的工具结果直接如实报告。

## 配置

至少配置以下文件：

- `tele_bot/config/feishu/local.env`
- `tele_bot/config/llm/local.env`

飞书基础配置：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
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

`FEISHU_VERIFICATION_TOKEN` 与 `FEISHU_ENCRYPT_KEY` 是原有的可选回调安全配置，
定时任务不要求新增或启用它们。

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
- 受控 Codex CLI（显式 `/codex` 命令或已确认的定时任务触发）
- 经确认的文件覆盖和隔离删除

文件发送受允许目录、文件名长度和 30 MB 大小限制保护；工具不会向用户返回文件 key、会话 ID 或底层 API 响应。

## Codex 和文件变更安全边界

即时 Codex 请求可使用明确命令；定时 Codex 请求须创建任务并确认执行权限：

```text
/codex inspect <任务>
/codex plan <任务>
/codex apply <确认令牌>
```

Codex 按飞书 chat 维护短期上下文：plan 的结果会随确认令牌传入 apply，后续
`/codex inspect`、`/codex plan` 或直接 `/codex apply <任务>` 会继承最近一次工作区和
Codex 结果。可在 `local.env` 中用 `TELE_BOT_CODEX_WORKSPACE_ROOT` 指定不带目录时的
默认项目目录；显式写在命令中的目录优先级更高。
`TELE_BOT_CODEX_MODEL` 默认使用经当前 ChatGPT 登录和 Codex CLI 0.160.0
只读验证可用的 `gpt-6.1-sol`，并显式传给 Codex CLI；换账号后应按该账号
实际可用模型调整。
在 `tele_bot/config/llm/local.env` 中修改 `TELE_BOT_CODEX_MODEL` 后重启服务，
新调用会使用指定模型。`TELE_BOT_CODEX_TIMEOUT` 默认 1200 秒，配置更大值也会
限制在 20 分钟以内；定时执行的外层子进程同样最多运行 20 分钟。
按任务指定或修改模型、跨多轮共享 20 分钟预算的编排方案见
[Codex Task Orchestrator 设计文档](CODEX_TASK_ORCHESTRATOR.md)；当前即时命令仍按全局模型配置执行。
若执行结果提示 `model is not supported when using Codex with a ChatGPT account`，
应检查 CLI 版本、登录账号和模型可用性；`Reconnecting`、`request timed out` 表示连接重试，
需单独检查网络。本机升级到 Codex CLI 0.160.0 后，`gpt-6.1-sol` 的只读验证
在 WebSocket 超时并回退 HTTPS 后成功完成。

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

## 定时触发（飞书）

飞书消息可以用自然语言或 `/schedule` 命令管理任务，沿用现有飞书配置；
`FEISHU_VERIFICATION_TOKEN` 仍是原有的可选回调校验配置，定时功能不依赖它。
`/api/messages` 没有可信的用户认证，不开放定时任务管理。
普通 Agent 将定时请求交给进程内的 `schedule_manage` MCP tool；MCP 工具只读取
当前飞书消息和可信会话身份，再调用原有 `ScheduleService` 完成解析、确认和校验。
时间更正与确认回复也走同一工具。模型不能替换消息正文或提交用户身份。
定时任务数据库默认是 `data/scheduled.sqlite`，可用 `TELE_BOT_SCHEDULE_DB`
改路径。服务启动时运行一个扫描线程和一个串行执行线程；执行时最多再启动一个
受 1200 秒超时约束的子进程。多个服务进程共享数据库时，文件锁只允许一个调度器工作。

```text
/schedule list
/schedule get <任务 ID>
/schedule history <任务 ID>
/schedule delete <任务 ID>
/schedule create {"content":"提醒检查报告","at":"2026-10-10T19:00:00+08:00","rule":{"kind":"once"},"mode":"remind"}
/schedule create {"content":"用 Codex 调用 autumn-recruitment-tracker skill 更新机器人/具身智能产品经理岗位，并将新增和修改项分别列成表格","at":"2026-10-10T14:00:00+08:00","rule":{"kind":"once"},"mode":"auto","scope":{"tools":["codex_apply"],"directories":["D:/files_data/Job/Job_workspace"]}}
```

`create` 也接受 JSON 数组，一次提交多条任务。`update` 接受同样的对象，增加
`task_id` 字段。创建、修改、删除先返回摘要和 15 分钟确认令牌，再回复
`确认 <令牌>`；预授权自动执行需要回复 `确认预授权 <令牌>`。同一会话只有一个
待处理项时可直接回复“确认”。查询不需要确认。到期且需要逐次确认时，回复
`授权 <执行 ID>` 或 `拒绝 <执行 ID>`；默认授权有效期为 15 分钟。

周期规则支持 `once`、`daily`、`weekly`；`weekly` 的 `weekdays` 使用 0=周一
到 6=周日，可附加 `interval`、计划触发 `count` 和带时区的 `until`。首次时间必须
带时区。错过计划时间默认 5 分钟后跳过；`missed_policy` 可设为 `skip`、
`run_once` 或 `ask`。关机期间遗漏多个周期时，历史记录逐次标记跳过，至多
补执行或申请授权一次。单次任务结束后移出活跃列表，历史仍可查询。
发送失败的提醒、授权申请和执行结果会留在数据库通知队列，网络恢复后重试。
飞书 Agent 会显示思考和 `schedule_manage` 工具调用进度；到点执行时先通知入队，再通知开始执行，
完成后发送结果。口语中的“四点半点”按“四点半”解析；时间已过时需要重新指定未来时刻。
若自然语言请求因时间已过而未创建，15 分钟内在同一聊天回复“改成五点四十五”
或直接回复“五点四十五”，会由 Agent 调用同一 MCP 工具，沿用原任务内容重新解析，
并再次显示待确认摘要。

当前自动执行工具白名单为 `opencli_search`、`filesystem_list_dir`、
`filesystem_read_file`、`write_report`、`codex_apply`。文件工具必须明确指定 `scope.directories`，
执行端只装载获准工具，并再次限制文件访问目录。`write_report` 仅在授权的第一个
目录新建 Markdown，不修改已有文件。`codex_apply` 需要单独授权一个工作区，
可在该工作区让 Codex 调用用户指定的 skill 并修改文件。`schedule_manage` MCP 工具
仅管理任务，定时执行子进程不能调用任意 MCP 或 Shell 工具；涉及这些操作的请求会在创建时拒绝。创建确认与执行授权
分开，原有操作级限制仍然有效。重要事件统一写入同一数据库的 `events` 表。

### Codex 定时产物与持久状态

Codex 任务通过 UTF-8 标准输入传入 CLI，避免 Windows `codex.cmd` 截断多行任务。
已授权工作区中的技能会准备在 `telebot_runtime/skills/<name>` 的运行副本：
原 `.codex` 文件只读，运行副本保留配置和去重状态，相对画像/台账路径在复制时校正。
原配置有变化时重新加载；否则保留本次用户任务对运行配置的调整。
用户原始任务的岗位方向和输出要求优先于技能默认值。

Codex 返回结构化 `status/result/paths` 收据；要求表格时必须提供本轮生成或修改的
Excel/CSV 文件。仅有中间文件、旧表格或文字日报不能认定表格任务完成。
附件经过目录、类型、大小和本轮更新时间校验后，随结果发送到原飞书会话。
每份附件单独保存通知和发送状态；上传失败后重试，服务重启后继续。
任务部分失败也可发送已完成的产物，同时保留实际失败状态。

## 测试

统一入口为 `run_tests.py`。不传模块名时运行全部测试；可指定
`schedule`、`codex`、`feishu`、`knowledge`、`tools` 或 `core`。
其他参数会直接传给 pytest，例如 `-k model`。`--list` 列出每个模块包含的文件。

```powershell
$py = 'D:\Anaconda3\envs\telebot\python.exe'
& $py .\run_tests.py schedule
& $py .\run_tests.py codex -k model
& $py .\run_tests.py --list
& $py .\run_tests.py all
```
