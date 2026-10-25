# tele_bot

一个以飞书为唯一消息入口的本地 Agent 服务。服务接收飞书事件，交给 LangGraph ReAct Agent 处理，再通过飞书回复文本或发送本地文件。

回复统一通过富文本 `post` 消息的 `md` 标签发送，保留标题、加粗、列表、链接、代码块和表格格式；
普通发送、引用回复、定时推送及流式状态原地更新共用同一格式。部分样式需要使用最新版飞书客户端。
交互确认仍使用带按钮的卡片。消息格式参见[飞书官方文档](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json)。

### 附件与需求分开发送

单独发送 Excel、文档或图片时，机器人先暂存附件，等待你补充需求，不自动分析或进入知识库。
推荐先发一个或多个附件，再发“解析这些岗位并加入校招多维表”等需求，模型会一次读取附件和文字。
同一用户在同一会话中连续发送的普通消息等待 3 秒后合并，连续输入最多等待 10 秒；
文字先发、附件紧接着发送也可合并。命令和确认按钮即时入队，不与普通消息合并。
同一用户的会话内执行按顺序进行，避免多个模型轮次同时改动同一会话。

待处理附件在 24 小时内随下一条需求读取；原消息和附件标识保存在检查点同目录的
`attachments.sqlite`，服务重启后可继续使用。更早的文件、已处理文件或升级前的文件，
通过飞书“回复/引用”原文件消息重新读取；原消息必须属于当前会话且机器人仍有读取权限。
直接分析不要求创建知识库。文档支持 TXT、Markdown、PDF、DOCX、CSV、XLSX；图片支持
现有多模态格式，也可作为文件发送。单个文档最多 30 MiB，单次文档正文最多 120,000 字符；
超出正文预算时明确标记内容截断，不能当作已处理全表。

只有明确说“加入知识库”，或在上传/引用附件后发送 `/kb import-attachment`，才生成入库确认卡片。
卡片默认只显示文件、范围、简要统计和必要的共享说明；`/kb details` 可查看完整待确认范围。
取消时只回复操作结果，避免重复整段预览。

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
模型调用失败时，控制台记录模型、异常类型、HTTP 状态、错误码与 request ID。
百炼返回 `Arrearage` 时需检查阿里云账户账单和余额；`insufficient_quota` 表示调用额度已耗尽。
这两类错误会直接提示恢复账户或额度后重新发送请求，并保留本轮工具执行结果。

每次模型调用前，`tele_bot/workflows/context_manager.py` 单独构建受限上下文：

- 按当前模型最大输入 991K（按 991,000 tokens 保守计算）设计：估算输入超过 600,000 时压缩，目标约 400,000，硬上限 900,000；预留约 91,000 tokens 余量。系统提示、工具定义和校准系数均计入预算。
- 摘要最多约 4,000 tokens；压缩时按 token 预算尽量保留完整近期历史，以最近 24 个消息组为搜索起点，不固定裁到 24 组。并行工具请求及对应结果一起保留或一起压缩。当前用户请求始终保留，必要时继续缩小窗口。
- 摘要记录目标、约束、已完成操作、失败与下一步；旧文件正文压缩为读取凭据，路径、行数和续读游标另存。
- `context_summary`、`summary_until_message_id`、`context_refs` 和 token 校准系数随检查点保存。摘要节点先保存，再调用主模型，因此 API 超时后不用重新压缩全部历史。
- 第一次遇到旧长会话自动迁移模型视图，原始记录保留。摘要按不超过约 64,000 tokens 的批次生成，只处理游标之后新增的旧消息。已经压缩的旧正文不会自动还原到模型上下文。
- 摘要请求超时最多 60 秒（不超过主模型配置）且不重试；失败时保留已有摘要、使用有界凭据降级，仍执行输入上限。当前请求或固定提示本身超限时明确报错。
- 压缩历史时，飞书状态消息显示开始整理、正在生成第几批摘要和整理完成；摘要失败时显示使用精简历史继续处理，随后恢复正常思考轮次提示。

中文文本使用保守估算，并根据服务端返回的实际输入 token 用量向上校准。
普通会话主模型总输出上限保持 4,096 tokens；摘要调用总输出额度为 5,024 tokens，
其中为思考和格式预留 1,024 tokens，最终摘要按保守估算裁至 4,000 tokens。
主模型超时采用 `ALIBAILIAN_TIMEOUT_SECONDS`。输入与摘要预算在 `ContextPolicy` 中集中配置，无需向量库或新数据库。
定时任务也使用同一上下文管理器，其业务记录仍单独保存。

Qwen3.5/3.6/3.7/3.8 的 Chat 调用显式限制思考预算：主模型 1,024 tokens、摘要模型
256 tokens，给总输出额度中的正文保留空间，避免思考耗尽额度后返回空内容。
每次 ReAct 调用都注入最新的 Asia/Shanghai 时间，今天、明天等日期以此解析。
空回复会记录模型、结束原因、输出及思考 token 数，便于区分额度耗尽与其他异常。

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

### 联网搜索与网页读取

普通对话和定时任务使用百炼 Responses API 的内置工具：`web_search` 搜索公开信息，
`web_extractor(url)` 读取指定页面并返回正文信息及来源。用户提供链接要求摘要时优先调用
`web_extractor`。旧 `opencli_search` 已移除，搜索不再依赖 OpenCLI / DuckDuckGo。
OpenCLI 仍用于 `ask_adviser` 和 `knowledge_restore`，与联网工具无关。

这两个入口通过函数工具适配现有 ReAct 图，在内部调用百炼原生工具。搜索请求启用
`{"type":"web_search"}`；网页读取同时启用 `web_search` 与 `web_extractor`，并设置
`enable_thinking=true`。主对话模型及其他工具保持现有调用流程。

复用 `ALIBAILIAN_API_KEY` 和 `ALIBAILIAN_BASE_URL`，可独立配置：

```dotenv
ALIBAILIAN_WEB_MODEL=qwen3.7-plus
ALIBAILIAN_WEB_TIMEOUT_SECONDS=120
```

网页模型须支持百炼 Responses API 的搜索和网页抓取工具。设置修改后重启服务生效。
工具保留实际搜索来源和调用次数；接口失败、未实际执行所需工具或没有有效内容时返回错误，
不能声称已读到页面。登录、验证页面和站点访问限制仍须根据真实结果说明。
长内容最多返回 12,000 字符并标记 `truncated`，不保证完整长文。
既有定时任务中的旧搜索授权在执行时映射为 `web_search`，不会自动增加网页读取授权。

官方说明：[联网搜索](https://docs.bailian.console.aliyun.com/zh/model-studio/web-search)、
[网页抓取](https://help.aliyun.com/zh/model-studio/web-extractor)。

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

Windows 下 `dir /s /b /ad "目录/*名称*"` 支持递归查找目录并输出完整路径；
不支持的列表选项会明确报错，路径不存在或读取失败时返回非零退出码。

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
/kb import-doc <飞书 docx/wiki/sheets/base 链接> [--options JSON]
/kb inspect-doc <链接>
/kb sources
/kb refresh <来源ID>
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

支持 Markdown、TXT、PDF、DOCX、XLSX 和 CSV；压缩包、可执行文件和未知二进制会拒绝。
飞书图片消息及富文本中的图片会下载为多模态内容，交给当前聊天模型直接分析；支持发图后继续追问。
支持 PNG、JPEG、WebP、GIF、BMP 和 TIFF，单张最多 20 MiB，单条最多 8 张、合计最多 30 MiB。
图片不自动导入知识库，也不传给 Embedding API；下载失败会直接报告，不会仅凭占位文字分析。
原图保留在会话检查点中以支持追问；上下文估算按视觉 token 计费方式预留预算，摘要不包含 Base64。
知识库文件必须在
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
云文档不会自动同步，仅管理员显式导入或手动刷新时读取。备份或恢复时同时
处理会话 SQLite 与 `KB_SQLITE_PATH`；数据库启用 WAL，复制前应先停止服务或使用
SQLite 在线备份。

## 知识库检索与 Skill 按需加载

主 Agent 已注册 `knowledge_search(query, limit)`，可以用自然语言查询已导入的项目资料；
`/kb search` 保留为直接查看片段的入口。主 Agent 使用 LangChain 适配器直接调用
`KnowledgeSearch`，与独立只读 MCP 共用成员校验和当前版本过滤，没有另一套授权规则。
工具参数不接受身份或空间；服务端使用已认证飞书消息中的用户及当前 chat 绑定的空间。
未认证的 `/api/messages` 不开放知识库工具和管理命令。Agent 检查点按 chat、用户、绑定空间
隔离，避免用户切换或重新绑定空间后读到旧检索结果；旧检查点保留，新上下文重新开始。

在 `tele_bot/config/llm/local.env` 或环境变量配置（环境变量优先，重启生效）：

| 配置 | 默认值 | 含义 |
|---|---|---|
| `KB_SEARCH_MODE` | `vector` | `hybrid` 并行 BM25 + 向量并以 RRF 融合；`keyword` 仅关键词 |
| `KB_SEARCH_CANDIDATES` | `30` | 每支候选数；单次不得小于最终数量 |
| `KB_SEARCH_RESULTS` | `5` | 默认返回数，1–20；显式工具 limit 可覆盖 |
| `KB_RRF_CONSTANT` | `60` | 每支贡献 `1/(k+rank)`；分数仅用于排序 |
| `KB_MIN_VECTOR_SCORE` | `-1` | 保留原基线全量余弦排序；可在真实评测后提高阈值 |
| `KB_QUERY_EXPANSION` | `false` | 启用可选别名扩写，始终保留原始查询 |
| `KB_QUERY_MAX_EXPANSIONS` | `2` | 最多附加查询数，0–5 |
| `KB_QUERY_EXPANSIONS_FILE` | 空 | 可信 JSON：`{"原查询": ["别名查询"]}`；无映射时使用原查询 |
| `KB_CHUNK_MAX_CHARS` | `1200` | 单块最大字符数 |
| `KB_CHUNK_OVERLAP_CHARS` | `120` | 超长块拆分的重叠长度，小于最大长度 |
| `KB_EMBEDDING_CONTEXT_PREFIX` | `false` | 为向量输入附加独立保存的文档名和章节路径 |

分块优先保留 Markdown ATX/Setext 标题、DOCX Heading/outline 标题与自然段边界；
PDF 保留页码并识别显式标题、编号和中文章节。视觉标题无法识别时按页和自然段回退，
无结构文本按自然段、句子、字符顺序回退。Markdown 普通管道表和 DOCX 普通表格与
XLSX/CSV 共用结构规范化器；代码内的 `#` 或管道表不当作文档标题或表格。
普通正文和代码超长时按句子/行或字符拆分，保留可追溯范围。
`source_locator` 中的字符范围是规范化提取文本的半开区间，不是 PDF 字节坐标；
DOCX 另有块号、PDF 另有页码。每块同时保留章节路径、文档版本、序号和原文哈希。
上下文前缀不进入展示原文；向量复用键包含端点、模型、维度和完整向量输入。
不同位置的相同正文各保留引用，共用兼容向量；重导历史内容会重新激活相应内容版本。
数据库自动迁移至 v5，保留旧分块；新增云来源登记、范围配置、revision、成功读取时间和状态序号。
旧资料重新导入后才构建结构元数据和明确模型身份的复用键。

Skill 常驻提示只包含 `global.md` 规则和名称/用途目录。模型通过 `load_skill(name)` 加载
相关领域正文；多个匹配可分别加载。正文仅在当前请求的系统提示中生效，历史正文在模型
视图中变成凭据，完整记录仍保留。Skill 声明的 `tool`/逗号分隔 `tools` 与实际注册工具校验；
缺失时标记 unavailable，不能靠加载 Skill 授权工具。名称错误、不可读或过长正文均显式降级；
全局规则或目录超限会明确报错，关键要求不会被静默截断。未选择 Skill 时按全局规则和
工具说明处理，普通闲聊不载入领域正文。

可复现评测及结果见 [离线回归报告](evaluation/KNOWLEDGE_REPORT.md) 和
[真实 Embedding 报告](evaluation/KNOWLEDGE_LIVE_REPORT.md)：

```powershell
python -m tele_bot.knowledge.evaluation --output evaluation/KNOWLEDGE_REPORT.md
# 调用已配置的 Embedding API，消耗少量 token；只处理公开评测语料。
python -m tele_bot.knowledge.evaluation --live --output evaluation/KNOWLEDGE_LIVE_REPORT.md
python run_tests.py knowledge
```

公开小语料上，真实向量与混合模式的 MRR@5 均为 0.938，未证明默认启用混合更优；
人工别名扩写有所提升，样本仍不足以支持默认开启。固定向量报告只验证排名算法。
无相关资料仍可能返回近邻；RRF 分数不代表答案可信度，回答必须核对引用原文。
检索召回和生成答案忠实度需分开评估。

### 本地表格与附件（阶段 A）

先安装 `requirements.txt`（新增锁定依赖 `openpyxl==3.1.5`）。`/kb import` 支持
绑定目录中的 XLSX/CSV 扫描和显式文件导入；飞书文件附件由同一解析器处理。
本地文件和附件先读取并展示目标空间、表名、工作表/区域、表头源行、字段、记录数和
缺失公式缓存，再由管理员确认索引。确认绑定预览快照和目标空间；本地文件在预览后
变化或会话切换空间时需重新预览。新附件下载发生在预览阶段，Embedding 只在确认后调用。
预览过长或解析超限会要求缩小范围，不会静默省略字段或导入半份文件。

XLSX 按工作表顺序读取，使用空白行/列分隔的数据区域；仅按真实合并范围传播值。
默认每个区域首行为表头，不推测业务含义；多层表头或无表头用显式配置：

```text
/kb import D:/资料/项目.xlsx --table-options {"sheets":["项目进度"],"cell_range":"A1:F200","header_rows":2}
/kb import D:/资料/清单.csv --table-options {"header_rows":0,"delimiter":";","encoding":"gb18030"}
```

`sheets`、`cell_range` 仅用于 XLSX，范围不能切断合并单元格。
显式 `cell_range` 视作一个区域，保留其中完全空白的行列，不再按空白分隔。
CSV 支持分隔符探测、
引号内换行和 UTF-8/BOM、GB18030、带 BOM 的 UTF-16，可显式指定编码。
CSV 来源位置使用逻辑记录序号，与物理文本行号不同。空白/重复表头保留源列号，
不覆盖列。DOCX 优先采用连续的 `tblHeader` 行，否则使用首行；Markdown 使用标准
表头分隔行。没有明确结构的复杂表头需先整理或通过 `header_rows` 明确选择。
当前对一个导入文件使用同一表头行数；不同区域表头层数不同的文件请拆分或选定区域。

表格片段按完整行分组，重复表名和字段路径；超长记录按字段/单元格分段并保留记录
与字段上下文，避免静默截断。源定位包含 sheet/表块、区域、行列/逻辑记录与最终规范化
文本字符范围。单元格内换行在检索文本中显示为 `↵`，中间结构保留原值。
数字零、空值、公式缺失缓存分别表示；公式仅保留表达式及源文件缓存，不执行宏或重算。
公式缓存缺失时，预览和导入结果列出具体表名/源行列范围，并标记部分成功。
日期以 ISO 值保留，百分比/货币等附带原始数字格式，不声称完全复现 Excel 显示结果。

配置可写入 `tele_bot/config/llm/local.env` 或环境变量，后者优先：

| 配置 | 默认值 | 含义 |
|---|---|---|
| `KB_TABLE_MAX_UNPACKED_BYTES` | `104857600` | Office 解压后总字节上限 |
| `KB_TABLE_MAX_SHEETS` | `32` | 工作表数量 |
| `KB_TABLE_MAX_ROWS` | `50000` | 源行号/CSV 逻辑记录数上限 |
| `KB_TABLE_MAX_COLUMNS` | `256` | 源列号上限 |
| `KB_TABLE_MAX_CELLS` | `500000` | 单元格/展开区域预算 |
| `KB_TABLE_MAX_CELL_CHARS` | `100000` | 单元格字符数 |
| `KB_TABLE_MAX_OUTPUT_CHARS` | `10000000` | 单文件及批次预览规范化输出字符数 |

继续受 `KB_MAX_IMPORT_BYTES` 和空间分块总数限制。XLSX 在加载前检查压缩包、XML、
区域和合并大小；超限需缩小/拆分源文件。修改导入配置或来源位置会更新引用，未变化的
兼容 Embedding 输入继续复用。重导相同配置与内容不会重复请求 Embedding。
Agent、`/kb search`、只读 MCP 都返回定位、版本和索引时间；返回片段不等于整表，
完整性未验证时不能据此回答全表计数、金额求和或全量筛选。

`.xls`、`.xlsm`、OCR、复杂 PDF 表格重建和 DOCX 嵌套表格不在当前结构化支持范围。
PDF 继续保留文本与页码，导入预览会提示表格结构能力边界。

### 飞书云来源、范围选择与刷新（阶段 B）

截至 2026-10-20，**统一表格与云文档接入需求已完成**：阶段 A 本地表格/附件和阶段 B
云来源、范围预览、导入检索及手动刷新均已实现。知识库专项回归 102 项测试、18 个子测试
通过；按项目完成口径，剩余真实租户专项联调和生成答案评测作为后续验证，不阻塞本次完成。
真实环境仍需验证 docx 合并块/嵌入引用、Sheets 显示值/合并区域、Base 视图/隐藏列/filter
和高级权限，以及答案事实支持与引用正确性；现有真实 Embedding 报告只评估检索。
这里提供的是知识库导入与检索能力，尚不包含远端表格记录增删改。

新版 docx 使用文档块和完整分页恢复标题、正文、普通表格、合并关系与块定位；
分页读取使用 latest（`document_revision_id=-1`），并校验读取前后 revision，不需要历史版本的编辑权限。
Wiki 先读取节点的实际资源身份，再分派到 docx、Sheets 或 Base。旧版 `/docs/`、
Wiki/文件夹递归导入、自动同步仍不支持。Docs 客户端独立于消息适配器，只向
`https://open.feishu.cn` 或 `https://open.larksuite.com` 请求；来源地域必须匹配配置。
认证缓存、有限限流/超时重试、单响应字节、分页数/记录数与输出预算均在服务端执行。
单次来源及其嵌入读取共用最多 512 次请求、100 MiB 累计响应预算；分页单接口最多
100 页/50,000 项，Sheets 每批 200 行，继续受本地表格各项大小限制。

```text
/kb import-doc https://team.feishu.cn/docx/DOCUMENT_TOKEN
/kb import-doc https://team.feishu.cn/wiki/NODE_TOKEN
/kb inspect-doc https://team.feishu.cn/sheets/SPREADSHEET_TOKEN
/kb import-doc https://team.feishu.cn/sheets/SPREADSHEET_TOKEN --options {"sheet_id":"SHEET_ID","range":"A1:F200","header_rows":2}
/kb inspect-doc https://team.feishu.cn/base/APP_TOKEN
/kb import-doc https://team.feishu.cn/base/APP_TOKEN --options {"table_id":"TABLE_ID","field_ids":["FIELD_ID_1","FIELD_ID_2"]}
/kb sources
/kb refresh cloud:FULL_SOURCE_ID
```

`inspect-doc` 供管理员查看元数据/字段与嵌入引用，不调用 Embedding。Sheets 必须明确
单个工作表和有界 A1 区域，按批次读取显示值，保留源行列和合并关系，校验 revision；
不会读取其他 sheet 的值，不自行计算复杂单元格。Base 必须明确单个数据表和非空字段 ID
列表，先读字段定义再完整分页搜索记录；可附加显式 API `filter` 对象和 `view_id`。
view 仅作选择条件，不视为权限隔离；隐藏列/视图过滤语义仍需租户样本联调。

Base 文本化保留数字零、日期时区、数字/货币格式、全部选项、已授权人员展示名或身份、
标题及 URL、公式/查找返回值、record_id 与字段 ID。附件仅名称/类型/引用，关联记录
仅已返回展示值及身份，不下载正文或递归展开。未知字段/无法解释结果在预览、导入结果
和检索状态中标记缺失，不直接序列化内部对象。Base 搜索 API 无跨页事务快照保证；
本地只在完整分页成功后发布，不能将它解释为源端事务一致性承诺。

内嵌 Sheets/Base 默认只列出引用，并将父文档标为部分成功。管理员可先 `inspect-doc`
查看 block_id，再在父文档配置中明确指定实际资源和范围：

```text
/kb import-doc https://team.feishu.cn/docx/DOCUMENT_TOKEN --options {"embeds":{"BLOCK_ID":{"url":"https://team.feishu.cn/sheets/SPREADSHEET_TOKEN","selection":{"sheet_id":"SHEET_ID","range":"A1:F200"}}}}
```

连接器用显式身份核对 API 返回的引用 token，不猜测拆分不明 token。内嵌正文作为独立
来源索引，引用实际表格 URL 和行列/record_id；父文档保存入口和引用清单。
无权限、未知引用、超限或未选择范围均明确显示，父正文不会伪装成整份内容完整成功。

确认绑定目标空间、选定配置和已读快照；预览不会调用 Embedding。相同资源及同一范围的
Wiki/直链使用相同来源身份；不同范围独立登记。`refresh` 复用已确认配置并重新预览，
不会自动增加工作表、字段或内嵌资源。范围变化需重新 `import-doc`。导入成为空间成员
可读的副本，不继承源文件/Base 的行列 ACL。

云索引采用事务内发布：全部向量/分块就绪后才更新当前版本、来源状态和成功读取时间。
超时、分页失败、预算耗尽或 Embedding 失败保留旧版本及其成功读取时间；明确观察到撤权
或资源删除后，服务端检索过滤该源，普通失败不会重新启用它。新的成功读取须再次确认。
来源状态序号同时防止旧预览或并发索引覆盖后续撤权/删除。该机制只在显式读取时检查
远端状态，没有后台 ACL 轮询。检索结果携带 `source_read_at/source_status/source_revision/source_error`，
远端 revision 独立保存，原 `source_version` 继续表示索引配置指纹。

新增锁定依赖 `tzdata==2026.5` 为无系统 IANA 时区数据的环境提供日期解释支持。
API 契约以[飞书官方文档](https://open.feishu.cn/document/server-docs/docs/bitable-v1/bitable-structure)
与[官方 SDK 结构](https://github.com/larksuite/oapi-sdk-python/tree/v2_main/lark_oapi/api/docx/v1/model)为依据。
可复现离线验证：`python run_tests.py knowledge`；云端样本使用模拟 HTTP 与 Embedding。
真实租户后续验证应覆盖上述结构、范围与权限组合，以及刷新后的修改/删除/撤权、
普通 Agent/命令/MCP 的来源引用和生成答案忠实度；这些验证尚未宣称通过。

## 多维表格实时记录操作

新增五个工具：`bitable_inspect`、`bitable_query`、`bitable_create_records`、
`bitable_update_records`、`bitable_delete_records`。它们直接操作授权 Base 的记录，
与知识库快照检索独立；仅此模块使用锁定的 `lark-oapi==1.7.2`，保留现有消息与知识库
httpx 链路。五个工具默认常态注册，无需启用开关；SDK 仍在首次工具调用时加载和初始化。
目标或凭据配置缺失时，工具返回配置提示，不影响普通聊天和知识库。

安装 `requirements.txt` 后，在 `tele_bot/config/feishu/local.env` 配置：

```env
# 两项同时留空则复用 FEISHU_APP_ID/FEISHU_APP_SECRET；也可配置独立表格应用。
FEISHU_BITABLE_APP_ID=
FEISHU_BITABLE_APP_SECRET=
FEISHU_BITABLE_TARGETS_FILE=tele_bot/config/feishu/bitable_targets.json
FEISHU_BITABLE_SQLITE_PATH=data/bitable.sqlite
FEISHU_BITABLE_TIMEOUT_SECONDS=30
FEISHU_BITABLE_QUERY_PAGE_SIZE=50
FEISHU_BITABLE_MAX_QUERY_RECORDS=500
FEISHU_BITABLE_MAX_WRITE_RECORDS=100
FEISHU_BITABLE_CONFIRM_TTL_SECONDS=600
```

无需维护逐表名称、链接或权限 JSON。向机器人发送 Base 直链，例如
“看看 https://ccnae2vjwuda.feishu.cn/base/ZmTbbJudzajY18soG28c87LKnmc 中有哪些数据表”。
`bitable_inspect` 自动读取飞书名称、URL、数据表及应用文档权限，保存已发现资源的
自动缓存；多张表时先选择具体数据表再读字段或操作记录。之后可使用实际名称、唯一别名
或带 `?table=tbl...` 的 Base 链接定位。缓存持久化到现有 SQLite，按应用 ID 与 API 域隔离，
名称可自动更新；权限每次清单查询重新向飞书读取，不将缓存当作权限依据。
说“登记/记住这张多维表格”也直接触发自动发现。若链接中的 `table` 参数不匹配飞书
返回的真实数据表，Base 仍会被记住，工具列出真实数据表供选择；不会误报需要管理员后台登记，
也不会把后续记录操作自动转到另一张表。

多维表格模块不设用户、会话或记录操作权限管理，所有飞书调用者默认可调用增删改查；
能否成功由 Friday 的飞书 API scope、文档权限及高级权限决定。全部记录操作不包含表结构管理。
可选旧 `bitable_targets.json` 仍支持作为发现入口与唯一别名配置，但无需创建；
旧 `users`、`chats`、`operations` 已忽略，旧展示名称不作为清单的真实名称来源。
`local.env` 的 `FEISHU_ALLOWED_USER_IDS` 是整个飞书机器人入口的用户白名单（逗号分隔，
留空不限制入口），属于机器人全局配置，本次没有修改；多维表格模块不再另设权限管理。

发送“现在你管着哪些多维表格？”或 `/bitable list`，会列出当前已知资源，
例如“1. 飞书返回的 Base 名称 / 内容选题管理（可阅读、可编辑）”。
`/bitable list 2` 翻到第二页，每页五项；工具使用 `bitable_inspect(target="", page=1)`，
按 `next_page` 续读。清单名称、URL、权限来自飞书 API，`permission_source=feishu_api`。
权限仅在接口返回明确布尔结果时展示“可阅读、可编辑 / 只读 / 无阅读编辑权限”；
缺少权限查询 scope、限流或失败显示“权限未核实”，不能据可读或本地默认开放推断可写。
这些是应用文档级权限，不能替代高级权限的行列与记录操作校验。
名称唯一时可直接问“内容管理里有哪些选题？把 A 的状态修改为已完成”。一个 Base
发现了多张表或名称重复时，需选清单中的唯一别名。

`coverage=known_resources_only` 表示清单仅覆盖从链接或旧配置已发现的资源，不能声称
这是 Friday 全部有权限的 Base。当前官方 SDK/API 没有找到“按 App ID 反查全部可访问 Base”
的全量接口；文件夹清单接口只按指定目录枚举，不能当作全部授权资源列表。
要自动发现新增表，可后续接入共享文件夹或知识空间作为发现入口，目录外单独授权的表
仍可能遗漏。当前实现为 Base 直链自动发现，不包含文件夹/知识空间递归扫描或用户 OAuth 搜索。

应用需开通并发布 `base:table:read`、`base:field:read`、`base:record:retrieve`、
`base:record:create`、`base:record:update`、`base:record:delete` 等对应权限，并获得
目标文档权限；高级权限需单独核对。`99991672` 表示接口权限不足，需要检查应用 scope。
读取真实名称可使用 `base:app:read` 等元数据 scope；核验文档权限可使用
`docs:permission.member:auth`（完整编码），接口也接受 `bitable:app` 等替代 scope；
后续文件夹发现需 `space:document:retrieve` 或该接口支持的其他 scope。
2026-10-21 用户补开并发布相关权限后最终只读复测通过：Friday 已能读取测试 Base 名称
**API_test**，文档权限返回 `view=true`、`edit=true`，清单展示“可阅读、可编辑”，无 scope 错误。
可从[Friday 权限申请页](https://open.feishu.cn/app/cli_aaf9f13e4b78dd11/auth?q=docs%3Apermission.member%3Aauth&op_from=openapi&token_type=tenant)
定位完整权限编码并开通发布；`bitable:app` 与 `base:*` 不是同一权限编码。
本地权限管理移除不会替应用增加飞书端权限。
如果能读结构但写入返回 HTTP 403 / `91403`，需由 Base 所有者/管理员在右上角
「…」→「…更多」→「添加文档应用」中给应用可管理权限，开启高级权限时同时检查目标表。
应用身份与提问者身份不同；工具仍绑定原始消息身份以防重复写入、隔离查询上下文并确认原操作，未认证 `/api/messages`
不开放该能力。它不要求先建立知识库空间，也不继承知识库成员的写权限。

可直接发送“查看项目表字段”“查询项目表进行中的记录”“新增项目 A，预算 1000”
或“把 A 项目的状态改为已完成”。Agent 先读取结构和记录，重名时消歧。单条新增/修改
按目标策略执行；批量写入和物理删除返回完整、有界的确认卡片，需原用户确认。
配置 `confirm_all=true` 可使该目标所有写入都确认。卡片过长时要求缩小范围。
“截止时间定在今天”“把状态标记为已完成”等自然写入表达可直接生成变更，批量修改只需看过预览后确认一次。
预览仅显示记录名和本次涉及字段，日期按 Asia/Shanghai 显示为可读日期或时间，不展示整行原始 JSON。
更新确认只核对本次要改的字段；其他字段变化不再使确认失败，要改的字段或表结构变化仍会中止。

```text
/bitable whoami
/bitable list [页码]
/bitable confirm <确认令牌>
/bitable cancel <确认令牌>
/bitable status <操作ID>
```

同一用户、会话只有一个待确认表格操作时可直接回复“确认”“确认修改”或“取消”，由服务端执行冻结计划，
不再交给模型重新调用写入工具。多个待确认项必须点击对应卡片或带明确令牌选择。工具不能自行确认。
卡片回调兼容飞书当前 operator 用户字段，立即返回已收到提示；执行后原卡片更新为结果并移除按钮，
更新卡片失败时在会话发送结果。成功反馈说明修改条数、记录名、字段前后值及回读核验结果。
飞书开发者后台的“事件与回调”中，卡片回传交互 `card.action.trigger` 也须配置到同一
`/feishu/webhook` 地址；只订阅消息接收事件不会向服务投递卡片点击。
新增持久化幂等键；
同一消息重复调用复用原结果，同一内容来自新消息仍可作为新请求。状态 `unknown`
表示远端结果未确定，停止盲目重试，使用操作 ID 和实时记录核对。`submitted=true`
只表示已取得提交成功响应，还需看 `verification`；`awaiting_confirmation` 不是写入成功。
同表通过 SQLite 租约协调本模块的多个进程，不能将读前/写后检查视作远端原子事务。

查询默认 50 条、单页最多 100 条，续页累计默认最多 500 条；游标绑定身份与查询选择。
`has_more`、`limit_reached` 或 `truncated` 表示覆盖不完整。超长单字段可用 record_id、
单字段 fields 和 start_char 补读 JSON 片段，核对相同 value_hash 后拼接，不能据片段宣称整表统计。
文本、数字、复选框、已有选项、日期、超链接和电话号码可写；清空使用 `clear_fields`，
缺省字段保持不变。人员/关联/附件写入、结构管理、自动同步、OAuth 和定时写入暂不提供。

备份操作账本应使用 SQLite 在线备份，或停服务后连同 WAL 一起处理；不要删除账本来重试未知写入。
SDK 初始化/权限故障仅使该能力报错，消息与知识库仍可使用。运行 `python run_tests.py bitable`
做离线验证，`python run_tests.py all` 做全量回归；测试默认不调用真实飞书。
用户授权测试 Base 的独立联调命令是 `python -m tests.bitable.live_smoke`（只读发现）和
`python -m tests.bitable.live_smoke --write`（新增、查询、修改、清空并删除本次测试记录），
结果保存到 `.runtime/bitable-live-report.json`，不会自动开放生产目标。
2026-10-21 验证：全量离线回归 327 项测试、54 个子测试通过；真实授权 Base 的数据表、
字段读取及记录新增、查询、修改、清空、删除均通过，测试记录已清理。应用 Friday 的
原有记录 CRUD scopes 和测试 Base 文档权限已补齐；工具默认可调用，本地不限制用户和记录操作。
登记清单与名称定位已增加离线验证，覆盖不同用户/会话默认可用、旧权限字段忽略、同名消歧、
清单翻页及真实 ReAct 图的清单→结构→实时查询→修改链路（模拟模型与飞书接口）。
链接自动发现与真实权限展示的多维表格专项 42 项测试、23 个子测试通过；之后新增的
登记请求 ReAct 图回归已包含在全量 327 项中，覆盖不匹配 table 参数的真实故障模式。Ruff 检查通过。
本轮真实只读联调在没有目标 JSON 的情况下自动发现并缓存测试 Base 的 6 张数据表，
未修改飞书记录；后续补开权限后 Base 名称已核实为 API_test，文档级阅读、编辑权限均核验通过。

## 工具与 Skill 召回评测

默认评测包含工具调用与 Skill 加载各 50 条不同的自然语言正例，通过实际 ReAct 图测量
正确调用率、Micro/Macro Recall、Precision、延迟均值/P50/P95、token 和路由成本，以及失败分桶。
业务工具全部模拟，不实际发布、创建任务或操作文件；Skill 加载和模型 API 调用真实执行。
`python run_tests.py recall` 验证本地评测框架；真实运行及轨迹重放命令见
[100 例测试说明](evaluation/CAPABILITY_100_TEST.md)。输出 Markdown、JSON、CSV 和逐例断点，费用为
按官方价格及实测 token 用量的估算，模拟业务工具耗时不当作真实服务耗时。
本轮 [100 例实测报告](evaluation/CAPABILITY_100_REPORT.md) 中，工具组召回率/正确调用率为
98%/80%，Skill 组为 29.6%/28%；[逐例 CSV](evaluation/CAPABILITY_100_REPORT.csv) 可直接查看耗时、费用和失败分桶。
此前 24 条混合正负例及其结果保留在 [旧评测说明](evaluation/CAPABILITY_TEST.md) 和
[旧实测报告](evaluation/CAPABILITY_REPORT.md)，不混入各 50 例的新分母。

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

当前自动执行工具白名单为 `web_search`、`web_extractor`、`filesystem_list_dir`、
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
`core`、`schedule`、`codex`、`feishu`、`recall`、`knowledge` 或 `tools`。
其他参数会直接传给 pytest，例如 `-k model`。`--list` 列出每个模块包含的文件。

测试按模块放在同名目录，入口自动发现目录内的测试，无需维护文件清单：

| 目录 | 覆盖范围 |
|---|---|
| `tests/core/` | 消息、路由、执行器、上下文、状态与模型客户端 |
| `tests/schedule/` | 调度命令/MCP、任务执行与产物交付 |
| `tests/codex/` | Codex 命令与运行器 |
| `tests/feishu/` | 飞书适配器 |
| `tests/recall/` | 技能加载、能力召回与评测指标 |
| `tests/knowledge/` | 文档解析、检索、存储、命令、云连接器和 Agent/MCP 接口 |
| `tests/tools/` | 文件操作、工作区策略、搜索、Git 与发布流程 |

知识库测试对应 `test_ingestion.py`、`test_retrieval.py`、`test_store.py`、
`test_commands.py`、`test_cloud.py`、`test_interfaces.py` 六个功能文件。
新增用例优先扩展所属功能文件和测试类，不按开发阶段或单个格式另建一套测试。
通用夹具放在 `support.py`，模拟云 API 放在 `cloud_fakes.py`；测试文件之间不相互导入。
相同规则的多组输入使用参数化或 `subTest`，避免复制夹具和断言流程。

```powershell
$py = 'D:\Anaconda3\envs\telebot\python.exe'
& $py .\run_tests.py schedule
& $py .\run_tests.py codex -k model
& $py .\run_tests.py --list
& $py .\run_tests.py all
```
