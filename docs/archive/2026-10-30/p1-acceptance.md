# P1 验收记录

> 历史记录说明：下文保留 2026-10-29 与 10-30 03:19 的验收事实；当前 P0–P4 进度以 [本轮验收](p0-p4-acceptance.md) 为准，最终 P1 回归见文末补记。原文“P0 未关闭、P2 未实现”等是当时状态。

日期：2026-10-29。分支：`codex/telebot-plugin-rebuild`。P1“webhook 最小闭环”已满足开发计划的退出标准；P0 的其他高风险原型和 P2–P7 尚未完成。

进度复核：2026-10-30 03:19:22。本轮构建、30项本地测试、带fixtures打包及产物检查通过，基础宿主11项、飞书ACP/Web各13项完整回归通过（原P1各11项，加P2模型/目录两项）。P1范围不因新增P2能力扩大；P0仍有schedule门禁未关闭。详见[本轮记录](p2-p4-local-progress.md)。

## 本轮实施前源码核对（历史快照）

2026-10-30 03:05（Asia/Shanghai）：`channel.ts` 仍只实现 P1 状态/停止和文字运行，`events.ts` 的文件/图片仅返回不支持通知；卡片只供状态查看。`pnpm build` 和 `pnpm test` 本轮已执行，16/16 本地测试通过。以下 22 项宿主组合场景来自历史记录，本轮尚未重跑。P2/P3/P4 的本地推进与真实平台联调分别记录，不能用 mock 冒充租户权限验收。

## 交付及环境

- `@telebot/core@0.1.0`：公共服务身份、官方 Agent 创建/恢复/输入/取消/turn 回执适配，不实现模型循环。
- `@telebot/feishu@0.1.0`：官方 webServer 路由、来源/管理员校验、SQLite inbox/outbox、chat/thread 映射、文字/post、最终回复、状态卡片与 `/status`、`/stop`、`/help`。
- 官方 `@deepseek-ai/dsh@0.2.2-alpha.2` 独立 npm 安装，Windows、Node 24.19.0、官方 ACP SDK 1.4.0。未修改宿主源码，没有内嵌宿主或依赖旧 Python。
- 打包后使用官方 plugin manager 安装，安装源 tarball 移走后启动；ACP profile + 官方 webServer 和 Web profile + 官方 ACP 两种装配均通过。

## 结果

本轮构建、30项本地检查、发布包/SHA/peer检查、11项宿主基础组合检查通过。以下11项P1场景保持通过；ACP/Web各再验证2项P2，共26项：

| 场景 | 已验证结果 |
|---|---|
| 安装和启动 | 仓库外官方 profile 加载发布包，未配置插件默认停用，native webServer 负责监听 |
| 来源与身份 | challenge/错误 token/app/管理员拒绝、机器人忽略，签名/AES 输入及错误签名拒绝 |
| 文字工具闭环与去重 | 飞书文字触发实际 Harness 工具，结果进入模型和最终回复；同 event 或同 message 不重复执行 |
| 并发与队列 | 模型保持等待时已持久接收并返回；同会话串行，其他 chat/thread 可执行，线程回复路由正确 |
| 状态/卡片/取消 | 状态不等待模型；拒绝其他管理员或伪造 chat/message；一次性回调去重；`/stop` 取消真实 Agent |
| 执行与投递分账 | 429 重试同一 UUID；网络断连置 unknown，没有重新执行 Agent |
| 正常重启 | 相同 session ID、工具历史与接收去重保持，未知发送不自动重发 |
| 执行途中崩溃 | 杀死真实宿主进程后等待租约，待处理输入恢复；运行中请求只核对原生记录，不重交模型 |
| 发送途中崩溃 | sending 恢复为 unknown，平台发送次数保持一次 |
| 停用 | webhook 路由和 worker 退出，官方 HTTP carrier 仍在 |
| 卸载 | 通过官方 manager 移除全部测试 bundle，业务数据库及映射保留 |
| P2 模型切换 | /model 不调用模型，切换后下一次实际HTTP模型请求采用所选模型 |
| P2 目录与重启 | /cd创建独立原生Session，实际Shell cwd改变；允许根越界被拒绝；重启后模型/cwd保持 |

机器可读证据（忽略、不提交）：`.runtime/verify-latest.json`、`.runtime/verify-feishu-latest.json`、`.runtime/verify-feishu-web-latest.json`。复现命令见 [testing.md](../../testing.md)。

## 边界及下一阶段

飞书 API 和模型均为 loopback mock，真实 Harness Agent/Session/Loader/HTTP carrier 参与执行。没有调用真实飞书、付费模型、向真实用户发送测试消息或迁移生产数据。真实租户权限、平台渲染、反向代理和 Linux/macOS 原生环境未验收。

参考原项目的 app/webhook 参数和变量名，未复制真实凭据；原配置缺少来源 token/EncryptKey，新插件不会因此放宽来源校验。进程内导入与运行方式见 [operations.md](../../operations.md)。

2026-10-30 用户已选择先完成代码与本地演练工具，准备好配置后再继续真实飞书联调。当前仍缺非空来源校验、专用测试会话和已确认的公网 HTTPS 回调；不切换旧系统生产入口。配置示例和参考变量导入器已经存在，专用部署演练工具尚未新增交付。此项属于部署待验收，不把模型/目录切换、附件、消息合并或流式进度重新归入 P1。

P2原生命令桥、/model、/cd已接通；审批/提问、文件和普通提醒仍待实现。消息合并和流式进度尚未实现；P1卡片为状态查看，不能当作业务写入审批或Bitable冻结确认。P3/P4已有本地域实现与行为测试，尚无独立bundle接线；P5–P7未据此宣称完成。

## 2026-10-30 最终回归补记

2026-10-30 11:49:50（Asia/Shanghai）：P1 原有行为在基础 ACP/Web 各 13/13 项及全业务组合各 19/19 项中回归通过。已新增 P2 附件/审批提问/普通提醒，以及三个可选业务包；本文早期范围是历史记录。真实租户未联调，见 [当前验收](p0-p4-acceptance.md) 和 [待联调清单](../../feishu-live-checklist.md)。
