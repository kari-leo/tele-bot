# P2–P4 本轮源码与验收记录

> 历史快照说明：本文仅记录 2026-10-30 03:19 的状态，已由 [本轮 P0–P4 验收](p0-p4-acceptance.md) 替代。不能将其中两包、30 项测试、未接线或故障未修正解读为当前状态。

核对结束：2026-10-30 03:19:22，Asia/Shanghai。计划触发时间为 2026-10-29T19:00:00+00:00（本地 03:00）。本文件记录本轮事实；范围及退出标准仍由 HARNESS_DEVELOPMENT_PLAN.md 唯一规定。

## 实际工作树

`git branch --show-current` 为 `codex/telebot-plugin-rebuild`；`git log -8 --oneline` 的最新提交 `303f152` 是旧 Python 系统，不能代表重建进度。实施前 `git status --short` 已有旧源码删除、README/.gitignore 修改及新插件树未跟踪，本轮保留这些状态，没有提交、回滚或恢复旧代码。实施前 SHA-256 快照见 `tracking_outputs/2026-10-30-baseline.json`；本轮新增/修改按该快照计算，不能把 Git 中全部未跟踪文件算成本轮新增。

## P2

| 子项 | 本轮完成及证据 | 未完成/边界 |
|---|---|---|
| CommandAdapter | `plugins/core/src/harness/commands.ts` 使用官方 list/find/execute；本地 DTO 在 contracts.ts；`domain/commands.ts` 分流，未知命令不送模型 | 原生命令兼容许可表当前仅 compact；真实宿主中的 compact 行为尚未专项验收，其余命令未开放 |
| /model | 宿主目录校验、Agent.model.select、原生 flush 后更新 SQLite 映射；不同 chat 隔离 | 活动会话拒绝切换；不修改全局默认值；无图像能力切换验收 |
| /cd | `adapters/workspaces.ts` 检查绝对路径、realpath/junction、目录和允许根；先准备新原生 Session，再 CAS 更新映射，旧会话保留 | 默认允许根仅配置的 workspace，切到其子目录；有活动/排队请求拒绝；不迁移旧审批授权 |
| 原生 Shell | 复用官方 provider；两种 profile 都核对切换后真实 Shell cwd；基础验证仍覆盖 PowerShell 提权拒绝 | 没有自建 Shell；复杂进程树清理、生产业务写入限制和 Linux/macOS 未验收 |
| 状态与恢复 | 状态显示模型/cwd；中断的变更命令恢复为 unknown、不自动重放；单元测试覆盖准备失败、忙碌拒绝、重启持久化 | 跨宿主与渠道数据库无法宣称原子提交，失败保持 unknown 并阻断依赖执行 |
| 审批/提问、附件双向、普通提醒 | 本轮未实现 | 这些仍可继续本地开发，不能归因为必须等待真实飞书；附件解析/24h 暂存/30 MiB 边界、回执及 schedule 业务出站/恢复待补 |

## P3

`plugins/feishu-tools/src/domain/authorization.ts` 已有 documents、bitable、calendar、tasks 各 read/create/update/delete 的逻辑能力矩阵（16 组）。必须有可信管理员、匹配的用户凭据归属、有效期限、对应能力和明确资源；tenant 身份须显式允许，不能静默回退。Web 无可信 Actor 时拒绝。分页必须完成至 hasMore=false；缺 cursor、cursor 循环及预算耗尽均失败，不能将截断结果标成完整。

`tests/acceptance/local-business.test.mjs` 覆盖上述权限与分页拒绝。`tests/acceptance/feishu.test.mjs` 补现有消息 FeishuApi 的 HTTP/平台拒绝、429/token 过期重试、5xx/断连/损坏响应/缺回执 unknown、并发 token 合并及鉴权失败不写入。这里的消息 API 测试不代表四类业务 API 已实现。

未完成：feishu-tools bundle/工具接线、真实 HTTP/SDK CRUD、OAuth/user token 生命周期、实际 scope 码与资源 ACL、Bitable 冻结预览/确认/乐观校验/unknown 回读和操作账本。逻辑能力名不是 Feishu OAuth scope 码；尚未查询或猜测真实 scope。

## P4

| 子项 | 本地完成 | 未完成 |
|---|---|---|
| Codex 三种模式 | `plugins/codex/src/domain/coding.ts`：inspect/plan 要求后端声明强制只读；apply 绑定可信用户/chat/session/cwd/冻结内容 SHA-256，派发前消费授权；unknown 不重试；传递取消及 20 分钟截止信号 | 真正的 executor 必须实现并验证只读和强制超时终止；当前只是端口策略，不能将后端声明当作真实写入拒绝证明 |
| Codex 历史 | 本地策略限制管理员本人私聊、归档只读、续聊目录重新检查 | 生产历史适配器、一次性续聊令牌、原生 provider 对照、进度/取消/结构化结果内容校验、本轮产物哈希与投递未实现 |
| 原生 Codex 原型 | `pnpm verify:codex` 本轮使用 CLI 0.160.0、隔离 home/workspace 和 loopback，4 项通过：只读/never 回合、原生历史、重启 resume、归档读取 | 与旧超时原因未做因果诊断；不因此宣称生产连接器或完整只读/取消/产物已验收 |
| Adviser | `plugins/adviser/src/domain/adviser.ts`：question/context/excerpts 和返回值先脱敏；脱敏后哈希；每会话 LRU 默认16，最多64会话；默认180秒；取消/错误降级，不合作后端仍占并发槽直到结束 | 只是规则过滤，非完整敏感信息检测；无 OpenCLI/其他实际 provider、宿主工具注册及独立启停；后台不合作进程需未来 adapter 终止 |

P3/P4 三个目录含实际领域源码、tsconfig 和行为测试，`pnpm build` 编译；尚无 package.json/patch/index 宿主接线，也未加入 `tooling/common.mjs` 的发布包清单。不能安装、不能宣称独立插件已交付。实际生产 tarball 仍仅 core/feishu，diagnostics 为夹具。

## 本轮实测

| 命令 | 最终实际结果 | 证据 |
|---|---|---|
| pnpm build | 六个编译目标通过 | tracking_outputs/2026-10-30-build.log；pnpm test 也重新构建最终源码 |
| pnpm test | 30/30，通过（实施前为16） | tracking_outputs/2026-10-30-tests.log |
| pnpm pack:plugins --fixtures | core/feishu/diagnostics 三个包通过 | tracking_outputs/2026-10-30-pack.log |
| pnpm check:artifacts | tarball 内容、SHA、peer、路径通过 | tracking_outputs/2026-10-30-artifacts.log |
| pnpm verify:harness | 11/11，通过 | .runtime/verify-latest.json |
| pnpm verify:feishu | 13/13，通过 | .runtime/verify-feishu-latest.json |
| pnpm verify:feishu --web | 13/13，通过 | .runtime/verify-feishu-web-latest.json |
| pnpm verify:codex | 4/4，通过 | .runtime/verify-codex-latest.json |
| pnpm verify:p0 | 本轮未运行 | 旧 schedule 故障注入失败继续待处理，不能算通过 |

外部官方宿主 `TELEBOT_DSH_INSTALL=C:/Users/41547/AppData/Local/Temp/telebot-official-0.2.2-alpha.2-clean`，由 official-host.mjs 再核对版本/解析位置；没有修改官方源码。所有模型与飞书 API 均 loopback。最终机器可读副本保存在 tracking_outputs，无需依赖会被覆盖的 latest 文件。

首轮 ACP/Web 各在 /model 断言失败；诊断日志确认 `cannot get property "sessions" without inject`。回归测试先失败，再把 CommandAdapter 的直接服务属性访问改为与既有 Conversations 一致的公开 ctx.get 查找；最终 30 项本地及两种完整组合通过，临时 DEBUG 日志已删除。首轮失败日志和 red 测试记录保留，最终通过不抹去初始失败事实。

## 跳过的真实联调

遵照本任务要求，未调用真实飞书 API、付费模型，未导入真实密钥，未向用户/租户发送消息，也未切换生产 webhook。待专用测试应用、来源鉴权、回调及测试资源准备好后，另做 webhook/卡片渲染、审批/提问身份、附件真实上传下载及限制、四类业务 CRUD/用户 OAuth/实际 scope/资源 ACL/通知影响/分页/回读，以及代理/公网部署。真实 Codex/adviser 模型质量与付费后端执行也未测试；不能用 loopback 报告代替。Linux/macOS 与生产数据迁移同样未执行。
