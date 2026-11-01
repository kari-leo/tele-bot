# 文档入口

接续开发默认只读 [状态页](status.md) 和仓库 [AGENTS.md](../AGENTS.md)，再按本次改动查下表；无需通读历史报告。

| 目的 | 唯一入口 |
|---|---|
| 当前实现、未完成与下一步 | [status.md](status.md) |
| 范围、阶段、退出标准与后续配置 UI | [总开发计划](../HARNESS_DEVELOPMENT_PLAN.md) |
| 具体领域规格 | [详细规格附录](reference/requirements.md)，只读对应小节 |
| 你需要准备的部署/配置 | [user-adaptation.md](user-adaptation.md) |
| 安装、参数与恢复 | [installation.md](installation.md)、[operations.md](operations.md) |
| 飞书正式配置 UI / 文件 | [feishu-configuration.md](feishu-configuration.md) |
| 应用身份权限、资源分享与配置步骤 | [feishu-permissions.md](feishu-permissions.md) |
| Codex 会话权限与旧实现差异 | [codex-permissions.md](codex-permissions.md) |
| 接通公网回调、排查当前 502 | [feishu-callback.md](feishu-callback.md) |
| 上游知识库安装与 D 盘目录 | [knowledge.md](knowledge.md) |
| 官方与旧 Telebot 定时区别 | [schedule-comparison.md](schedule-comparison.md) |
| 按改动选测试、复现命令 | [tests/README.md](../tests/README.md)、[testing.md](testing.md) |
| 实际验证矩阵、旧功能差距 | [compatibility.md](compatibility.md)、[feature-parity.md](feature-parity.md) |
| 尚待真实飞书联调 | [feishu-live-checklist.md](feishu-live-checklist.md) |
| 历史证据（默认不读） | [archive](archive/README.md) |

维护规则：新进展只更新状态页和机器摘要；范围/排期变动改总计划；操作说明只写在对应指南。不要再增加并列的“最新进度/最终验收”长文。原始日志留在 .runtime，需留存的脱敏证据归档后从摘要引用。

- [原生桌面对话入口](native-desktop.md)：默认完整权限、四种无飞书最小组合及原生投递。
