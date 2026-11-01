# P0 原型与决策

> 统一飞书更新说明：此文保留本轮合并前的阶段证据；当前四包、配置 UI 和实际 Base/群测试以 [统一飞书验收](feishu-unified-acceptance.md) 为准。

核对：2026-10-30 11:49:50（Asia/Shanghai）。P0 本地原型门禁通过，范围由 [总开发计划](../../../HARNESS_DEVELOPMENT_PLAN.md) 管理；P6 生产定时和 P7 跨平台未完成。

| 检查 | 通过数 | 证据 |
|---|---:|---|
| 本地构建/领域/适配/产物隔离 | 44/44 | pnpm test；先构建六个目标 |
| 官方宿主基础 | 11 | verify:harness |
| P0 完整原型组合 | 17 | verify:p0 |
| 飞书基础 ACP / Web | 13 / 13 | verify:feishu / --web |
| 全业务 ACP / Web | 19 / 19 | verify:features / --web |
| 实际原生 Codex | 9 | verify:codex；CLI 0.160.0 |
| 五包 + diagnostics 打包/闭包 | 6 包 | pack:plugins --fixtures；check:artifacts |

命令通过官方 commands 的 list/find/execute 与生命周期；生产许可表 compact。受限 Agent 的直接 Shell/subagent/schedule dispatch 拒绝，普通 Agent 不受影响。Windows 官方 Shell 验证中文/空格 cwd、提权拒绝、等待超时转后台、异会话不能取消/所属会话取消。

schedule 是唯一时钟。公开 catalog/history/resolve-agent/changed 提供结构化 task ID、scheduledAt、message ID；删除原生任务会丢原生历史，业务审计另存。旧周期和补跑以本地策略算下一次官方一次性唤醒，不另建扫描时钟。occurrence 授权版本/单派发方/防重/重启 unknown/删除审计有本地原型证据。

旧故障注入误伤观察用 ACP 回合导致提前失败；本轮修正触发边界，由独立会话观察，verify:p0 全部 17 项通过。不是修改官方 Harness 或宣称上游 bug 被修复。

Codex 选择专用公开 app-server 连接器：当前官方 provider 不满足原生历史/一次性续聊/schema 的等价入口。固定 CLI 0.160.0，实际隔离 MXC 的无害读取、拒绝写入、授权写入/哈希、取消、原生历史等 9 项通过；未访问真实账户/历史/付费 API。实验权限 API 升级必须重新验证。

用户已确认：共享 chat/按 thread 隔离，附件及确认用户绑定，统一 KB 管理员，30 MiB=31,457,280 字节，日历/任务标准 CRUD；对照旧实现但不复制 Agent/计时器。已交付五包、配置示例和无联网 preflight，真实飞书延后。完整事实和边界见 [本轮验收](p0-p4-acceptance.md)；[03:19 记录](p2-p4-local-progress.md) 是历史快照。

仍未宣称完成：生产 auto/confirm 定时等价、复杂进程树清理、Linux/macOS、真实飞书和数据迁移。当前不需要私有 Harness fork。
