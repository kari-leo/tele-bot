# 验证操作手册

选测规则与模块列表见 [tests/README.md](../tests/README.md)，当前事实与报告哈希只维护在 [latest.json](../tracking_outputs/latest.json)。普通修改不重复完整验收。

## 模块检查

```powershell
. ./examples/use-d-drive-runtime.ps1
pnpm verify --module feishu --local
pnpm verify --module settings --module feishu-business --local
```

本地按模块过滤并构建所需依赖；只改文档无需业务验证。宿主接线/生命周期变化才执行安装层，先指定仓库外官方 rc.5 安装 prefix（不能用源码依赖充当宿主）：

```powershell
$env:TELEBOT_DSH_INSTALL = 'D:/absolute/official-rc5-prefix'
pnpm verify --module feishu --installed
pnpm verify --module settings
```

安装层只构建/打包一次，打包包含 fixture 并检查归档闭包；真实官方 manager、隔离 home、环境白名单、loopback 模型/平台，无付费调用。ACP v1 不替代原生命令发现或业务授权。settings 的 desktop 变体模拟根端口 0 和独立飞书监听，不代表 Electron 视觉验收。

额外宿主风险选 --gate p0，完整业务接线选 --gate composition[-web]；真实 Codex 后端修改选 --gate codex-native 并设置 TELEBOT_CODEX_CLI，沿用 0.160.0 与隔离 home/MXC 策略。外部 KB 安装/版本变化选 knowledge。完整发行/宿主升级显式运行 `pnpm verify --release`，不包含真实平台或真实 Codex 账户检查。

缺临时运行时才在 D 盘准备；结束后核对无使用进程再清理该临时安装，不删正式桌面、profile、用户数据或被引用的 release：

```powershell
$verifyRuntime = Join-Path $env:TELEBOT_RUNTIME_ROOT 'official/rc5-module-verify'
npm install --prefix $verifyRuntime --save-exact --offline --ignore-scripts '@deepseek-ai/dsh@0.2.1-rc.5'
$env:TELEBOT_DSH_INSTALL = $verifyRuntime
```

## 真实飞书（显式选择）

预检、Base 与 chat 合并，保留范围限制和发送幂等账本：

```powershell
pnpm verify --live preflight --reference 'D:/external/local.env' --workspace 'D:/test-workspace'
pnpm verify --live bitable --reference 'D:/external/local.env' --base 'https://tenant.feishu.cn/base/EXPLICIT_BASE'
pnpm verify --live chat --reference 'D:/external/local.env' --chat 'oc_EXPLICIT_CHAT'
```

预检不联网，未就绪退出 2。Base 默认只读；明确授权才加 --write，只增删本轮标记记录，不改旧记录/表结构。chat 默认只查专用群资料；明确授权三条固定消息后才加 --send，成功项仅回读，sending/unknown 先对账。历史发送许可不自动延续。--callback 仅检查 HTTPS GET 可达，不修改后台或证明事件入站。真实选项禁止与模块/发行检查混用，不自动导入 env，不记录秘密、原始事件或文件正文。

## 报告

内部检查仍写 .runtime/verify-latest.json、verify-p0-latest.json、verify-feishu[-web]-latest.json、verify-features[-web]-latest.json、verify-feishu-setup-latest.json、verify-desktop-setup-latest.json、verify-codex-latest.json、verify-desktop-latest.json、verify-knowledge-latest.json 和 feishu-live-{bitable,chat}-latest.json，保留已有证据引用。成功的隔离 home 自动清理，失败保留有界诊断；旧报告不冒充新归档验证。
