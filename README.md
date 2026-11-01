# Telebot Harness Plugins

面向官方 DeepSeek Harness 的独立插件集：core、desktop、feishu、codex、adviser、publishing、automation。知识库直接使用外部 dsh-knowledge，不内嵌宿主或另建 Agent 循环。

原生对话按需安装 `core + desktop + 业务包`；飞书入口安装 `core + feishu + 所需业务包`。两个入口的权限独立。

| 目的 | 主要文档 |
|---|---|
| 安装、配置入口、本机目录 | [安装](docs/installation.md) |
| 对话、授权、备份和排障 | [使用与维护](docs/operations.md) |
| 飞书应用、资源及公网回调 | [飞书配置](docs/feishu.md) |
| 当前交付与未验证边界 | [状态](docs/status.md) |
| 开发范围、约束和验收 | [开发计划](HARNESS_DEVELOPMENT_PLAN.md)、[AGENTS.md](AGENTS.md) |
| 按模块验证与真实服务检查 | [测试入口](tests/README.md) |

接续开发先读状态，再读相关计划和测试组。版本/依赖以 [compatibility.json](tooling/compatibility.json) 和各包 package.json 为准；证据及归档哈希以 [latest.json](tracking_outputs/latest.json) 为准。
