# 验证操作手册

选测规则见 [测试入口](../tests/README.md)，通过数/实际范围见 [兼容记录](compatibility.md)。这里仅维护复现命令与报告位置。

## 本地与官方发行组合

需要 Node >=24.9、pnpm 11.28.5、tar。先独立安装官方宿主，再指定仓库外 npm prefix；不用项目 node_modules 或源码 checkout 充当宿主。

```powershell
$harnessInstall = Join-Path $env:TEMP 'telebot-official-host'
npm install --prefix $harnessInstall --save-exact @deepseek-ai/dsh@0.2.1-rc.5
$env:TELEBOT_DSH_INSTALL = $harnessInstall
pnpm build
pnpm test
pnpm pack:plugins --fixtures
pnpm check:artifacts
pnpm verify:harness
pnpm verify:feishu
pnpm verify:feishu --web
pnpm verify:feishu:setup
# 按变更范围追加：
pnpm verify:p0
pnpm verify:features
pnpm verify:features --web
pnpm verify:desktop
```

真实官方 manager 安装当前 tgz，使用隔离 DSH_HOME/用户 home、环境白名单、loopback 模型和平台，不继承生产密钥或开发 NODE_PATH；基础验收用 ACP v1。Web 变体从官方 Web profile 派生，ACP 仅为验收入口。ACP 不暴露原生命令注册表，不替代业务身份/授权和真实平台检查。P0 只证明公开 API/故障边界；P6 另在 features 组合验证 auto/confirm、受限 Agent 与产物投递。ACP 本身不提供 Web 的 SessionController/preset，测试显式装配官方公共组件。

## 原生 Codex 与页面 QA

```powershell
$env:TELEBOT_CODEX_CLI = 'C:/absolute/path/to/@openai/codex/bin/codex.js'
pnpm verify:codex
pnpm verify:feishu:setup --preview
```

Codex 固定 0.160.0，Windows 测试隔离 home 使用 prefer_mxc=true / windows.sandbox="mxc"。检查只读/外部/tmp 写入拒绝、确认 apply/可写续聊、CSV/XLSX 产物、Skill 副本、取消/历史，飞书受限检查不能以 full-access 绕过失败；P8 另测原生完整权限的受控 cwd 外写入和回环 HTTP；真实 CLI 与 features 的进程替身分别记录。

preview 在假配置页面等待 Enter，URL 位于忽略的 .runtime/setup-preview.json。用于浏览器填写/发现资源/保存并核对文件，结束后 Enter 完成卸载；不用真实应用凭据。不把预览开关加入产品 UI。

## 上游知识库

固定外部版本和安装入口见 [knowledge.md](knowledge.md)。`pnpm verify:knowledge` 单独验证上游 bundle 在实际官方 Web/ACP 组合中的安装、合成文本导入/关键词检索、重启持久化和卸载，不读用户库、不调用聊天/向量模型。设置 TELEBOT_RUNTIME_ROOT 和 TELEBOT_VERIFY_TEMP 到 D 盘以避免把新依赖/临时 home 写回 C 盘；结果为 .runtime/verify-knowledge-latest.json。

## 真实飞书（非默认检查）

```powershell
pnpm preflight:feishu --reference 'D:/external/local.env' --workspace 'D:/test-workspace'
pnpm verify:bitable:live --reference 'D:/external/local.env' --base 'https://tenant.feishu.cn/base/EXPLICIT_BASE'
# 仅在明确授权写范围内加 --write；只操作独立标记记录并清理。
# 明确授权三条固定消息后才使用 --send：
pnpm verify:feishu:live --reference 'D:/external/local.env' --chat 'oc_EXPLICIT_TEST_CHAT' --send
```

预检不联网，仅输出存在性，未就绪退出 2。Base 默认只读；--write 不改旧记录/表结构。chat 默认只查资料，--send 的 UUID/消息 ID 持久记录，重跑成功项仅回读，sending/unknown 先对账。--callback 仅无密钥 GET，不修改后台，不证明事件入站。历史三条发送不代表今后自动授权。准备项见 [用户适配](user-adaptation.md)，待执行见 [真实清单](feishu-live-checklist.md)。

## 结果与维护

本机报告：.runtime/verify-latest.json、verify-p0-latest.json、verify-feishu[-web]-latest.json、verify-features[-web]-latest.json、verify-feishu-setup-latest.json、verify-codex-latest.json、verify-desktop-latest.json、feishu-live-bitable-latest.json、feishu-live-chat-latest.json。详细 home/日志位置由运行报告给出；失败保留错误和已完成项，不登记新平台兼容。

可跟踪摘要只写 [tracking_outputs/latest.json](../tracking_outputs/latest.json)；保留的较早证据在 archive。最新统计在 tooling/compatibility.json，由 docs/compatibility.md 解释覆盖边界。秘密、原始敏感事件/文件不进入报告；不要每次新增重复的长篇阶段进度文档。

配置端口的桌面模拟：`pnpm verify:feishu:setup --desktop` 使用官方 Web/ACP 组合，根 webServer 端口 0，飞书隔离官方服务使用指定端口。它不启动/安装 Electron 桌面；另有本机正式 Electron 安装及启动落盘证据，见状态页；UI 和实际账户业务尚待验收。发布 Git 组合通过本地合成 Smart HTTP/bare 仓库，无真实仓库推送；Windows 本地 file 传输的 MSYS IPC 拒绝保留为官方沙箱限制。
## 复现时临时准备官方发行

本轮自有 alpha.2 与 rc.5 测试运行时已在交付后清理；正式桌面保留。需要重跑上述官方组合时才在 D 盘重新准备，不能将临时运行时作为桌面部署前置：

```powershell
. ./examples/use-d-drive-runtime.ps1
$verifyRuntime = Join-Path $env:TELEBOT_RUNTIME_ROOT 'tmp/telebot-compat-rc5-runtime'
npm install --prefix $verifyRuntime --save-exact --ignore-scripts '@deepseek-ai/dsh@0.2.1-rc.5'
$env:TELEBOT_DSH_INSTALL = $verifyRuntime
$env:PATH = 'D:\files_data\windborne\dsh-runtime\prerequisites\PowerShell-7.6.9;' + $env:PATH
pnpm pack:plugins --fixtures
pnpm verify:features --web
```

隔离 home 成功后自动清理，失败保留供诊断；后续清理仍需核对确属自有临时目录且没有活动进程。报告留在 .runtime，摘要和包 SHA-256 在 tracking_outputs/latest.json。

原生组合：verify:desktop 从官方 Web profile 派生，真实 Connection cookie/RPC 身份，逐个安装 core + desktop + 单业务包，明确不安装 feishu；另测非所有者、ACP、后台回执无执行权限及官方计时/附件/冷恢复。--preview 仅为验收保留隔离页面，不进入产品配置。
