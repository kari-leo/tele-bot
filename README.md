# Telebot Harness Plugins

面向官方 DeepSeek Harness 的独立插件集。官方宿主由使用方单独安装；当前发布 core、desktop、统一 feishu、codex、adviser、publishing、automation 七包，不再单独安装 feishu-tools。

知识库采用外部 [dsh-knowledge@0.5.2](https://github.com/Soren-ABT/dsh-knowledge)，直接安装其官方 bundle，不自建重复引擎；见 [安装与 D 盘目录](docs/knowledge.md)。

- 接续开发先读 [AGENTS.md](AGENTS.md) 和 [当前状态](docs/status.md)。
- 唯一范围与排期：[总开发计划](HARNESS_DEVELOPMENT_PLAN.md)。
- 使用方：[你需要做的适配](docs/user-adaptation.md)、[安装](docs/installation.md)、[飞书配置 UI](docs/feishu-configuration.md)。
- 全部指南：[文档入口](docs/README.md)；验证方式：[测试入口](tests/README.md)。

P0–P8 代码已落地，已通过官方 rc.5 的 Windows CLI/Web/原生所有者 RPC 隔离验证及桌面端口模拟；正式 Electron 已安装并通过启动落盘检查；正式 UI 和实际账户业务仍待验收。真实飞书与其他平台、生产切流单独验收。当前事实以状态页为准，历史证据不作为新任务。

## 开发

需要 Node.js >=24.9、pnpm 11.28.5 和系统 tar。

```powershell
. ./examples/use-d-drive-runtime.ps1
pnpm install --frozen-lockfile
pnpm build
pnpm test
pnpm pack:plugins --fixtures
pnpm check:artifacts
```

再设置 TELEBOT_DSH_INSTALL 指向仓库外官方 0.2.1-rc.5，执行 [官方组合门禁](docs/testing.md)。默认不带 --fixtures 的打包只发布七个生产包；dist/install-manifest.json 记录顺序和 SHA-256。运行数据、凭据、node_modules、lib、dist、.runtime 不提交；文档与脱敏机器摘要可跟踪。

原生对话按需安装 core + desktop + 业务包，默认完整权限；飞书权限保持独立。见 [原生入口](docs/native-desktop.md) 和 [全部安装方式](docs/installation.md)。
