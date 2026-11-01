# 飞书与业务插件运行说明

P0–P4 当前可用代码包括文字/卡片、命令/模型/目录/权限、审批提问、进度/合并、附件/普通提醒，以及可选的四类飞书业务、Codex、adviser。指定 Base 与获批三条群测试已通过，其他真实场景仍待执行，见 [清单](feishu-live-checklist.md)。另有上游 KB、publishing 和 automation；当前验收范围见状态页。

## 配置与参考参数

首次/缺项配置与后续维护优先使用 [Harness 桌面设置与文件配置](feishu-configuration.md)。下列环境 patch 仅作尚无文件时的启动输入；文件生效后以文件为准。渠道/业务属于同一安装包。

使用 [配置示例](../examples/feishu.patch.example.yml)，安装步骤见 [installation.md](installation.md)。沿用参考项目的 `127.0.0.1:3000/feishu/webhook`；HTTP listener 由官方 webServer 提供。服务端将 HTTPS webhook 路径反向代理到该地址即可，不要将整套 Harness Web API 一并暴露给公网。官方服务器要求具体网卡地址，不接受 `0.0.0.0`。

官方 patch 的 row.config 是整体替换。追加覆盖配置时应保留该 row 的完整必要字段，或直接编辑示例；单独用一个只有 model 的 config 覆盖会丢失 appId、管理员及 workspace 等字段。示例中的 TELEBOT_MAX_CONCURRENT_SESSIONS 可调整并发，默认 4。

凭据使用官方 credentials 引用。默认引用名为 `FEISHU_APP_SECRET`、`FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY`，可来自独立 DSH_HOME 的凭据存储或启动进程环境；官方托管凭据优先于同名环境变量。配置示例没有真实 app ID、用户 ID 或密钥。

参考项目 `D:/files_data/windborne/tele_bot/tele_bot/config/feishu/local.env` 中，app ID、app secret 和管理员已配置，Verification Token 本轮已确认非空，EncryptKey 仍为空。新插件至少要求一种 webhook 来源校验：只设置 Verification Token 可接收非加密事件；配置 EncryptKey 时，普通事件必须通过原始请求体签名和时间窗校验，支持 AES 加密事件。两者同时配置时同时校验。先在飞书开发者后台获取或设置这些值，并保持双方一致；不能仅凭 body 中的管理员 ID 开放执行。

如需沿用参考参数，可以在 PowerShell 中导入到当前进程（不回显、复制或修改原文件）：

```powershell
. ./examples/load-feishu-reference.ps1 -Path 'D:/files_data/windborne/tele_bot/tele_bot/config/feishu/local.env'
$env:TELEBOT_WORKSPACE = 'D:/your/workspace' # 已存在的绝对目录
# 补齐飞书后台配置的来源校验值，或使用官方 credentials 存储。
# FEISHU_ADMIN_USER_IDS 为逗号/分号/空格分隔的 user_id 或 open_id。
# 指定模型时同时设置 TELEBOT_PROVIDER 和 TELEBOT_MODEL；否则使用 Harness 默认模型。
```

导入器只读取所列飞书变量，按字面值解析基本 KEY=value/成对引号，不执行插值或 shell 语句；不导入旧项目的 LLM、KB、Bitable、调度及数据路径。不要把含凭据的环境转储写入报告。Linux/macOS 可由服务管理器向官方进程注入相同变量；这些平台尚未原生验收。

在飞书后台为机器人订阅 `im.message.receive_v1` 及交互卡片回调 `card.action.trigger`，配置同一 webhook URL，授予发送/回复消息所需的应用权限并发布应用。只有事件中真实 sender/operator ID 命中管理员名单才接受；名单为空时所有人都被拒绝。卡片额外绑定最初操作人、chat、消息、有效期及一次性令牌，按钮中的任意 text/user_id/sessionId 不作为执行指令。

## 会话、执行和投递

同一 app/chat/thread 使用同一 Harness 会话，同会话文字串行；不同 thread/chat 可并发，默认最多 4 个活动会话。线程回复使用原生 reply_in_thread。工作目录及模型配置保存到映射中；通过 `/model`、`/cd` 显式切换，启动配置不静默改动已有会话。Agent 循环、工具、模型和会话日志全部由官方 Harness 负责。

webhook 返回 200 表示已持久接收，不表示 Agent 或外部工具已成功。event ID 和 message ID 都持久去重；同会话运行时 `/status` 与 `/stop` 不必等待模型结束。`/stop` 请求取消当前轮，排队消息保留；已经发生的外部变更不会撤销。模型错误、取消、未知执行及未知投递不会被当作成功。

默认数据库：`$DSH_HOME/plugins/telebot-feishu/<appId>/channel.sqlite`。独立保存 inbox、outbox、会话映射与状态卡片令牌，不改写原项目数据。SQLite schema 版本为 1；不识别的版本拒绝启动。同一个数据库只允许一个持有租约的 worker。正常停用会取消/等待所属 Agent、移除路由、关闭数据库；进程崩溃后最多需等待租约过期，默认 10 秒。

重启时，已执行请求只读取 Harness 的持久 turn 回执，不重新提交输入。结果无法证实时保留 `unknown` 并阻止同会话后续普通消息继续，`/status` 仍可用。P1 不提供强制重放或解除 unknown 的命令；先核对宿主会话日志和外部结果，避免重复写入。

执行结果和发送结果独立。明确限流/令牌拒绝可有限重试投递，UUID 保持不变；网络断开、5xx、发送途中崩溃或缺失回执保留 unknown，不重跑 Agent、不自动再次发送。UUID 不代表永久 exactly-once 保证。未知投递和未知执行的数量都会显示在状态卡片中。

## 排障与数据保留

- `403`：来源凭据、app、管理员或卡片绑定校验失败；检查配置，不在日志中输出凭据/原始事件。
- `400` / `413`：事件格式错误或请求体超限；默认上限 1 MiB。
- `503`：队列已满、持久存储不可用或 worker 已停止；平台可重试，已接收事件仍会去重。普通队列满时另保留 32 个控制请求位置。
- webhook 不存在：检查插件是否启用、官方 webServer 是否已提供、插件加载错误和工作目录；未配置或缺少服务时不会建立另一个 listener。
- `channel_database_already_active`：停止另一个使用相同数据库的实例，或在崩溃后等待租约过期，不删除数据库规避。

数据包含对话和消息，需按业务数据管理访问权限。备份使用 SQLite backup，或停止 profile 后一起备份数据目录及对应 Harness session 数据；只拷贝正在写入的主 sqlite 文件不构成一致备份。Windows 依赖目录 ACL，不能把 chmod 当作 ACL 隔离。卸载插件保留数据库和 Harness 会话，代码回退不等于支持数据降级。

## P2 控制、附件和提醒

`/model` 查看目录，`/model provider/model` 切换；`/cd <绝对目录>` 只允许配置 workspace 内的真实路径。`/new`、`/reset` 创建新原生会话并保留旧历史；`/permission` 查看，参数只接受 `read-only`、`workspace-write` 两种预设，不能通过渠道提升到全盘权限。`/setting` 查看当前会话，不修改全局配置。原生命令许可表目前只有 `compact`，且宿主必须装配该命令。未知命令不交给模型。

普通正文默认同 actor/会话 3 秒合并，最长 10 秒；可配置 `mergeMs`、`maxMergeWaitMs`。`streamProgress=true` 时正文进度最多 5 条、间隔至少 1 秒，不含 reasoning。审批/问题通过官方公开服务，由卡片或 `/answer <token> <回答>` 处理，5 分钟有效、一次消费、重启失效；需要官方 ask-user 工具时用业务 patch 插入。

先发附件，再发要求；也可引用本人同 chat/thread 的附件消息。暂存 24 小时，不自动入 KB。单图 20 MiB、最多 8 张、合计/单文件/出站 30 MiB，文本最多 120,000 字符；实际格式能力由官方附件 provider 和所选模型决定。`/send <绝对文件路径>` 从当前 workspace 发文件，拒绝敏感路径及越界/junction。Codex 本轮产物自动进入相同出站管道，上传前复核冻结哈希。

普通提醒示例：`/remind create {"text":"检查演练结果","after_seconds":60}`，以及 `/remind list`、`/remind delete <id>`。其他时间字段沿用官方 schedule 的公开请求格式；没有另建扫描时钟。提醒只回传原 chat/thread，不获得 Shell/Codex/业务工具。list 分开显示 scheduleState、occurrence、本地投递及 outbox 状态；`delivered_to_channel` 不是远端发送成功。完整任务使用 automation 的 telebot_schedule 或 /schedule，创建与执行授权分开，详见 schedule-comparison.md。

## P3 用户授权与飞书资源

统一 `telebot-feishu` 沿用主配置的管理员、App ID 和 Secret。全部业务使用应用身份及 FEISHU_APP_TENANT，应用 token 由现有 App ID/Secret 自动获取与更新。资源以 business.resources 数组保存。用户 OAuth、scopes 配置、owner 和用户 token 不再使用；权限用户已确认开通，仍需授予应用实际资源访问权。

```json
[
  {"alias":"test-table","kind":"bitable","id":"base-token","tableId":"table-id","identity":"tenant","tokenRef":"FEISHU_APP_TENANT","expiresAt":0,"capabilities":["bitable.read","bitable.create","bitable.update","bitable.delete"]},
  {"alias":"test-calendar","kind":"calendar","id":"calendar-id","identity":"tenant","tokenRef":"FEISHU_APP_TENANT","expiresAt":0,"capabilities":["calendar.read","calendar.create","calendar.update","calendar.delete"]}
]
```

以上都是占位元数据，无 token。资源 identity 固定 tenant、tokenRef 固定 FEISHU_APP_TENANT；expiresAt=0 在请求前由运行时应用凭据更新。文档可设置 format=docx/wiki/sheets/drive，Sheets 另提供精确 ranges；任务 id 是 tasklist GUID。不填写 owner 或用户凭据，不执行用户授权。

`/bitable whoami`、`/bitable list [页码]`（每页 5 个已配置资源），或 `/feishu <操作 JSON>`。统一操作格式：

```text
/bitable {"resource":"test-table","action":"inspect","params":{}}
/bitable {"resource":"test-table","action":"query","params":{}}
/bitable {"resource":"test-table","action":"update","params":{"records":[{"record_id":"rec_test","fields":{"状态":"完成"}}]}}
/bitable confirm <token>
/bitable supplement <token> {"备注":"补充说明"}
/bitable cancel <token>
/bitable status <token>
/bitable reconcile <token>
```

模型有 Bitable 五类工具（含旧名称别名）以及 `feishu_documents/calendar/tasks`，写入只生成冻结预览。确认绑定原 user/chat/session/cwd 与 15 分钟期限，相关字段冲突或配置目标变化拒绝；补充只限单条记录字段，原期限不延长且旧 token 废弃。唯一待确认项可直接回复“确认/取消”，多个时需指定 token。`confirmAll` 默认 true；false 也不会让模型自行确认。批次最多 500 条，unknown 不盲重试；`reconcile` 只重试回读，无回执时需人工 query/inspect 核对。

文档 params：create 使用 `title`，update 使用 `text`（追加文本，最多 50 行/块），query 在 Sheets 使用已配置 `range`。云来源导入/刷新与 KB 不在此插件阶段。日历 query 指定 Unix 秒 `start_time/end_time`，inspect 指定 `event_id`（省略则列可访问日历）；写入的 `event` 含起止时间与 timezone，`notify` 必须明确；重复操作/删除指定 `recurrence_scope=single|all`，single 使用具体 occurrence ID。任务 inspect/delete 指定 `task_guid`，create 的 `task` 明确 summary、members、due；update 以 task 内提供的字段生成 update_fields，完成传毫秒时间戳字符串 `completed_at`。

业务账本位于 `$DSH_HOME/plugins/telebot-feishu/<appId>/business/operations.sqlite`，单写者，运行中崩溃转 unknown；配置及 credentials 另行备份。API 原点默认官方 Feishu，Lark 可显式设官方 Lark 原点；loopback 仅供测试。真实 scope/ACL、通知、回调和资源效果均尚未验收。

## P4 Codex 和 adviser

`telebot-codex` 配置绝对 `cli`、可选独立 `codexHome`、允许目录 `allowedRoots`、`adminUserIds` 与唯一 `historyOwnerUserId`。只支持当前实测原生 CLI `0.160.0` 的公开 app-server 协议和实验 named permission profiles；其他版本拒绝，需走兼容升级流程。Windows 本机证据使用隔离 MXC，真实部署先确认同等沙箱可用，不可用则失败关闭。插件不会修改用户全局 CLI 配置。

命令为 `/codex inspect|plan <要求>`（只读）、`/codex apply <要求>`（预览）、`/codex confirm|cancel <token>`、`/codex stop`。授权冻结 15 分钟、仅当前 actor/chat/session/cwd、消费后不重派；执行预算 20 分钟。产物核对当前目录、运行时间、大小、哈希，文件上传/发送单独有回执。账本默认 `$DSH_HOME/plugins/telebot-codex/coding.sqlite`。

原生历史仅 `historyOwnerUserId` 本人私聊：`/codex history [cursor] [archived]`、`/codex read <thread-id>`、`/codex resume <thread-id> <要求>` 后 `/codex resume-confirm <token>`。读取原生 CLI 历史，不能拿 Harness Session 冒充；归档只读，续聊再次检查目录，确认后仅当前 cwd 可写且关闭网络/隐式 tmp 写根，原历史宽权限不继承；unknown 先 read，不重发。

`telebot-adviser` 配置绝对 OpenCLI `cli`、`adminUserIds`、`secretRefs`，默认 `timeoutMs=180000`、每会话 `cacheEntries=16`，最多 64 会话。`/adviser <问题>` 或模型 `ask_adviser`；固定无状态 CLI 参数，不使用 shell，输入/输出脱敏，超时终止所属进程，失败返回 unavailable 由主会话继续。密钥 ref 的解析值也加入脱敏词表。自动化只验证替身 CLI；真实 OpenCLI 账户和实际模型能力需部署演练。


## 发布和任务

/report 只新建报告并回传核验产物；/publish publish 冻结原文与目标文件，确认后新建；/publish git 冻结指定文件、HEAD/分支/远程，单独确认后经官方 Shell 提交推送。/publish files 提供替换/移动/隔离删除预览，确认后保留恢复副本。Git for Windows 的本地 file 传输可能因官方沙箱 MSYS IPC 限制失败；保留 unknown/提交回执，不自动重推，不关闭沙箱。HTTP 路径用合成本地服务验收，真实远程凭据不由插件配置。

/schedule create/update/delete/batch 先提案，confirm/cancel 仅变更任务。auto 还需 authorize + 单独 confirm；confirm 模式到点后 execute/reject 执行 ID。list/get/history/status 查看任务/执行/逐文件回执；stop 取消当前执行；retry-delivery 只恢复待投递或已明确失败出站，不重做业务，unknown 保留待对账。官方 schedule 是唯一时钟，唤醒 Session 使用持久受限 preset，不获得业务执行权限。
