# P0–P4 开发与本地验收

> 统一飞书更新说明：此文保留本轮合并前的阶段证据；当前四包、配置 UI 和实际 Base/群测试以 [统一飞书验收](feishu-unified-acceptance.md) 为准。

核对日期：2026-10-30（Asia/Shanghai），分支 `codex/telebot-plugin-rebuild`，以当前工作树为准，尚未提交 Git。总范围仍由 [唯一开发计划](../../../HARNESS_DEVELOPMENT_PLAN.md) 管理；本文只记录交付和证据，取代 03:19 的阶段状态，旧记录保留为历史。

本轮完成 P0–P4 的代码、五个可独立安装的生产 bundle、配置示例和本地演练工具。按用户要求跳过真实飞书租户联调。因此是“本地开发完成，真实环境待验收”，不能据此宣布完整迁移或正式上线。

| 阶段 | 本轮完成内容 | 当前判定 |
|---|---|---|
| P0 | 外部官方安装、版本/peer/服务身份、命令生命周期、Shell 隔离/后台取消；schedule 结构化关联、故障后观察、旧规则/补跑/防重原型；原生 Codex 后端决定 | 本地原型门禁通过；自动化生产等价实现仍属 P6 |
| P1 | 可信 webhook、管理员、持久 inbox/outbox、chat/thread 会话、去重、正常及强制终止恢复、unknown、停用卸载 | Windows 官方 ACP/Web 与模拟飞书通过；真实租户待验收 |
| P2 | 宿主命令/模型/目录/权限、新会话、审批与提问、3 秒/最长 10 秒合并、有界正文进度、附件双向/24 小时暂存、普通提醒及出站账本 | 本地实现与组合验收通过；真实媒体、卡片、平台展示及跨平台待验收 |
| P3 | 独立 feishu-tools、OAuth 用户归属/刷新、四类资源逻辑权限与分页、Bitable 五工具及冻结账本、文档读取/创建/追加、日历/任务 CRUD | 本地 HTTP/领域与官方组合通过；真实 scopes、ACL、通知及资源效果待验收 |
| P4 | 独立 codex/adviser；Codex 三模式、一次性授权、原生历史/续聊、权限重置/取消/产物；adviser 无状态调用、脱敏、缓存、超时和降级 | 本地实现与组合验收通过；原生 Codex 使用实际 CLI + loopback；OpenCLI 使用进程替身 |
| P5–P7 | KB、搜索/发布/其余旧业务、完整 auto/confirm 定时、跨平台/升级/数据迁移和切流 | 未开发/未验收，未包含在本次完成范围 |

## 交付形态

默认打包 `@telebot/core`、`@telebot/feishu`、`@telebot/feishu-tools`、`@telebot/codex`、`@telebot/adviser`，版本均为 `0.1.0`，精确 `engines.dsh=0.2.2-alpha.2`。diagnostics 仅由 `--fixtures` 加入，不用于业务部署。外部官方 Harness 及其 Agent、Session、commands、Shell、schedule、附件和 credentials 服务均未修改。

实际构建输出和运行数据保持忽略；文档、源码和配置示例保持可跟踪。已存在的旧 Python 删除不回滚；没有导入旧数据库或凭据。

## 验证结果

最终计数以 [兼容记录](../../compatibility.md)、`tooling/compatibility.json` 和 `tracking_outputs/2026-10-30-p0-p4-final.json` 为准。机器原始报告在忽略的 `.runtime/verify-*-latest.json`；最终摘要保存每项说明、状态和时间，便于审计，不依赖临时目录永久存在。

必跑命令为 `pnpm build`、`pnpm test`、`pnpm pack:plugins --fixtures`、`pnpm check:artifacts`、`pnpm verify:harness`、`pnpm verify:p0`、`pnpm verify:feishu`、`pnpm verify:feishu --web`、`pnpm verify:features`、`pnpm verify:features --web` 和 `pnpm verify:codex`。复现方式见 [testing.md](../../testing.md)。全部模型和平台 API 均为 loopback；验收子进程环境不含真实模型密钥。

P0 先前失败是故障注入误伤用于观察的 ACP 回合。现在只在实际定时 inbox 已入场后注入，并由独立会话观察任务状态/删除审计，整轮通过。不能将历史失败解释为已修复官方宿主缺陷。

原生 Codex 固定 `0.160.0`，使用公开 app-server JSONL 与实验权限 profile `:read-only` / `:workspace`，启动时核对版本和能力。Windows 本机验证采用独立 home 的 MXC 沙箱配置；在该配置下实际执行无害读取、拒绝只读写入、授权生成文件并核对 SHA-256，另测取消、历史/归档/恢复。原先仅设置 legacy sandbox 时本机拒绝了包括读取在内的命令，不能把“所有命令都被拒绝”视为只读可用。没有启用全盘权限，也未修改用户全局 Codex 配置。

官方 provider 在当前版本/参考实现中没有等价的原生历史、续聊和输出 schema 入口，因此本轮选择专用原生连接器；同一请求只交给一个执行方，Telebot 会话中的替代 Codex 入口受 guard 限制。后续升级 CLI 的实验 API 必须重新验收。

## 业务边界与使用规则

- 普通群聊共享 chat 会话，话题按 thread 隔离；附件、审批、确认令牌绑定可信 user/chat/session/cwd。更换目录或会话不能继承旧授权。
- 附件 24 小时暂存，先文件后需求及本人引用可用，引用旧文件不受最近 8 份选择窗口影响。单图 20 MiB、最多 8 图、合计/出站 30 MiB（31,457,280 字节），文本正文最多 120,000 字符。原生附件 provider/实际模型需要支持对应格式；真实图片、Office/PDF 兼容效果留在真实环境检查，未自建 Office 转换器。
- 最终回复与进度分开，进度只包含正文、不含 reasoning，最多 5 次且至少间隔 1 秒；未知进度发送不阻塞最终回执。默认的 3 秒合并只合并同 actor/会话连续正文，命令/附件为屏障。
- 审批/提问复用官方服务；5 分钟、单次回答、重启失效。按钮回调和 `/answer` 都通过可信身份检查，未回答不授权。普通提醒由官方 schedule 唯一计时，本地仅持有映射、发生记录及出站引用；`delivered_to_channel` 不代表飞书已收到。
- Bitable 写入冻结目标、字段、记录 ID、相关字段基线、actor 和 15 分钟期限。`supplement` 只补单记录字段，保留原期限并废弃旧令牌；多记录或改变目标必须新预览。配置目标变化及乐观冲突拒绝确认。默认所有写入需用户确认；`confirmAll=false` 只允许可信直接命令的单记录 create/update 自动确认，模型工具仍只能预览。
- `unknown` 不自动重复写入。`reconcile` 仅对已取得 API 回执的操作重试只读核验；缺回执时需 inspect/query 对账后再决定是否新提案。文档追加每次最多 50 个文本块；Sheets 只读已配置的精确 range，Drive 是可见目录，不能声称全租户发现或 KB 导入。Drive 异步删除回执标注 asynchronous，需真实环境继续核实完成状态。
- 日历要求明确时区/通知，重复操作必须指明 single/all，single 需要 occurrence ID；删除也要求明确范围和通知。任务支持列表/详情/创建/修改/完成/删除，创建要求负责人和截止时间，完成可单独更新 `completed_at`。Harness todo/schedule 与飞书资源相互独立。
- Codex apply/续聊授权持久化、一次性消费；原生历史仅指定管理员本人私聊可读，归档不可续聊；恢复重新检查目录并重置只读。本轮产物验证目录、时间、大小和 SHA-256，再进入独立飞书出站记录；上传时哈希变化拒绝，不重跑 Codex。执行中崩溃标记 unknown。业务数据库单写者检查防止多实例消费。
- adviser 只读第二意见，实际参数固定为 `chatgpt ask --new -f plain`，不经过 shell；question/context/excerpts 在缓存哈希及发送前脱敏，回答再次脱敏。有界缓存/并发，默认 180 秒，超时终止所属 CLI；不可用时主对话继续。自动规则不能识别所有可能的秘密，用户不应主动传入凭据。

## 待真实验收

见 [真实飞书联调任务清单](../../feishu-live-checklist.md)。本轮没有向真实租户发消息、调用付费模型、迁移数据、切换生产 webhook 或验证 Linux/macOS。OpenCLI 真实账户可用性及实际选用模型的图片/文档能力也需部署验收；它们不是已通过的本地测试结论。
