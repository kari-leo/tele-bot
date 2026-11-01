# 官方桌面安装、启动与重装

2026-11-01 已按最新授权安装到官方 **0.2.1-rc.5 desktop profile**：七个自有包 0.1.0 + dsh-knowledge@0.5.2；adviser 停用。正式桌面已启动并生成六个配置入口、四类业务数据库。未发送付费模型请求或真实飞书消息；正式 UI 交互因浏览器自动审批服务额度不足尚未复验。以下步骤保留用于重装和其他机器，当前机器无需重复安装。

日常从 D 盘交付目录启动（先退出已运行桌面）：

```powershell
& 'D:\files_data\windborne\dsh-runtime\releases\telebot-0.1.0-20261101\start-desktop.ps1'
```

脚本与配套环境脚本已复制到该目录，独立于源码；它只准备当前进程的 D 盘缓存/临时目录、稳定 PowerShell 7.6.9 和 Git 路径，然后启动官方程序。原桌面快捷方式仍能启动，但不保证继承这些环境变量。

安装前备份：`D:/files_data/windborne/dsh-runtime/backups/desktop-2026-10-31T16-37-41-929Z`（18 个文件，327371 字节，已核对 SHA-256，不含依赖/缓存）；目录布局调整前的 profile 另保留在 `C:/Users/41547/.dsh/backups/desktop-before-profile-junction-20261101`。备份含原凭据，只在本机保留。验收结论见[状态](status.md)。不要混用 alpha.2 或 rc.3。

## 1. 核对版本并退出桌面

当前无需为本批再升级。若从未启动过桌面、尚无 profiles/desktop/package.json，先启动一次官方桌面完成 profile 初始化。安装前退出桌面和托盘进程，在 PowerShell 再次检查桌面自带 CLI：

```powershell
$dsh = 'D:\daily_app\Deepseek Harness\resources\runtime\cli\bin\dsh.cmd'
& $dsh --version
```

输出必须恰好为 `0.2.1-rc.5`；其他版本先停止安装并重新验证兼容性。使用这个包装器管理 `desktop` profile，不换用全局另一版本 dsh。

沿用桌面原 DSH_HOME（未自定义时 `$env:USERPROFILE\.dsh`）；保留原 C→D 联接。停机备份配置、官方凭据、会话和插件业务数据库，排除 node_modules、缓存及临时验收目录。代码回退不能代替数据恢复。

## D 盘依赖与构建许可

本机已将**整个 desktop profile** 放到 D 盘：

- 实体：`D:/files_data/windborne/dsh-runtime/profiles/desktop`，包含 profile 配置和 node_modules。
- 兼容入口：`C:/Users/41547/.dsh/profiles/desktop` → 上述 D 盘目录的 Junction。
- 宿主凭据、会话和业务数据仍沿用原 DSH_HOME。

不要只联接 node_modules：rc.5 的运行时解析按 profile 的真实路径识别宿主 peer。单独联接依赖目录会让插件导入报 ERR_MODULE_NOT_FOUND；整个 profile 联接后六个启用插件均已实际导入、初始化。

其他机器初装时可先在 D 盘创建空 profile 目录，再在原 profiles 下建立相应 Junction，之后启动桌面初始化。已有 profile 时先退出宿主、备份，将小型 profile 文件和已有依赖放到同一 D 盘 profile 下，逐文件校验后保留原目录并建立联接；不要覆盖用户配置或把凭据移进仓库。本机已经完成，不需再迁移。

在同一 profile 的 pnpm-workspace.yaml 中合并以下字段（保留其他内容），让官方 manager 的内容缓存使用 D 盘，并按固定知识库版本声明必要构建许可：

```yaml
storeDir: D:/files_data/windborne/dsh-runtime/cache/pnpm/store
allowBuilds:
  esbuild: true
  onnxruntime-node: true
  protobufjs: true
  sharp: true
  tesseract.js: false
```

这些构建许可对应 dsh-knowledge@0.5.2 安装依赖，不启用 embedding 或下载用户选用的模型权重。先运行下面的 D 盘环境脚本，再使用桌面自带 CLI 安装。

## 2. 按入口选择归档包

以下命令在仓库根目录运行；也可把 dist 复制到交付目录后修改 `$release`。SHA-256 在 `dist/install-manifest.json`。正式安装不装 diagnostics。

```powershell
Set-Location 'D:\files_data\windborne\coding\tele-bot'
. .\examples\use-d-drive-runtime.ps1
$env:PATH = 'D:\files_data\windborne\dsh-runtime\prerequisites\PowerShell-7.6.9;' + $env:PATH
$release = (Resolve-Path '.\dist').Path
# 双入口示例；纯桌面去掉 feishu，纯飞书去掉 desktop；业务包按需选。
foreach ($name in @('core','desktop','feishu','codex','publishing','automation')) {
  $archive = Join-Path $release "telebot-$name-0.1.0.tgz"
  if (-not (Test-Path -LiteralPath $archive)) { throw "缺少 $archive" }
  & $dsh plugin --profile desktop add ('file:' + $archive.Replace('\','/'))
  if ($LASTEXITCODE -ne 0) { throw "安装 $name 失败，先检查原因" }
}
```

使用官方插件管理器，core 先装，所有包共用其服务身份。首次解析宿主 peer 需要包源；安装后不依赖源码或开发 node_modules。adviser 安装后可保持停用，不需 OpenCLI 账户。

知识库使用外部 `dsh-knowledge@0.5.2`；同一 desktop profile 已安装时跳过：

```powershell
& $dsh plugin --profile desktop add 'dsh-knowledge@0.5.2' --save-exact
if ($LASTEXITCODE -ne 0) { throw '知识库安装失败' }
```

其他 profile 的安装不会自动出现在 desktop。默认关键词检索无需 embedding/模型下载；不迁旧知识库或做飞书空间绑定。

## 3. 启用并固定飞书端口

桌面退出时，在同一 home 的 `profiles/desktop/cordis.patch.yml` 合并 [desktop.patch.example.yml](../examples/desktop.patch.example.yml)，只合并已安装包对应的条目：纯桌面省略 feishu，纯飞书省略 desktop，未装 adviser 就省略它。保留已有条目；同一 row 只留一个最终覆盖项，不覆盖整个文件。不安装旧 feishu-tools。

桌面根 HTTP 地址由官方分配临时端口。`dedicatedIngress: true` 在同一 Harness 中隔离官方 webServer 服务，使飞书监听 `127.0.0.1:3000`；仅承载飞书路由。Agent、模型、计时仍共用原宿主，其他插件 UI 仍在桌面根监听器。

已有飞书 config.json 时文件优先于 patch：停机后在其 `config` 中设 `dedicatedIngress: true` 和 `deploymentPort: 3000`，或通过原 UI 保存后重启。首次无文件时采用示例 bootstrap。修改模式/端口需重启；其他普通字段空闲时直接应用。

从保留上述 D 盘环境的进程启动 `D:\daily_app\Deepseek Harness\DeepSeek Harness.exe`。Shell 需要 PowerShell 7 和 Git；本轮使用 D 盘 PowerShell 7.6.9 和现有 Git，不依赖 Codex 缓存内的可执行文件。启动失败先查看本机错误，不同时启动第二个写者。

## 4. 手填正式配置

原生入口另见 [desktop 说明](native-desktop.md)。各插件目录是 `$DSH_HOME/plugins/telebot-名称/config/`，含配置/模板、`setup.token`、`endpoint.json` 和写者锁。endpoint 仅记录地址。打开插件 UI：

```powershell
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
$name = 'feishu' # 或 desktop、core、codex、publishing、automation；adviser 需先启用
$configDir = Join-Path $dshHome "plugins/telebot-$name/config"
$endpoint = Get-Content -Raw -LiteralPath (Join-Path $configDir 'endpoint.json') | ConvertFrom-Json
$token = (Get-Content -Raw -LiteralPath (Join-Path $configDir 'setup.token')).Trim()
Start-Process ($endpoint.origin + $endpoint.route + '#' + $token)
```

不要分享令牌链接。

- 飞书：手填 App ID、Secret、Verification Token、管理员、工作目录和资源；参考旧 env 由本人查阅，无导入/OAuth/scopes UI。凭据由官方 credentials 保存。provider/model 同时留空使用宿主默认模型。
- desktop：默认完整权限，可在 UI 关闭并填写受限目录；inspect/plan 始终只读。
- Codex：既有 CLI 路径必填；纯原生可不填飞书管理员/根目录；飞书另填允许目录、管理员和本人历史 ID。沿用既有登录，Skill 来源和名称可留空。
- 发布：文章目录、Git 仓库/分支按需填写；飞书另填管理员/根目录。
- 自动化：时区与模型按需填写；飞书仍需管理员/目录和单独 auto 预授权。原生完整权限下 auto 创建即授权；显式 confirm 仍逐次确认。
- core：仅能力诊断。adviser：本轮无需配置。

多维表格在独立链接区增删，或管理员说“添加多维表格链接 URL”“移除多维表格链接 URL”；移除不删远端数据。文档/日历/任务另设应用资源白名单，写入仍需确认。

## 5. 原隧道与真实验收

保留 Cloudflare 上游 `http://localhost:3000`，先确认本机监听，再在飞书验证 `https://feishu.windborne.cc/feishu/webhook`。反代仅公开 webhook 路径，不能公开整个桌面/知识库管理接口；改端口时同步改隧道上游。见[回调指南](feishu-callback.md)。

切换前核对已有任务和 unknown 账本，停用旧 Telebot 对应入口，确保唯一回调消费者/任务写者。真实输入、通知、资源写入按[联调清单](feishu-live-checklist.md)验收。本机已正式安装并完成启动检查；这不代表公网已接通。飞书仍缺手填应用配置，当前不监听 3000。

卸载：退出桌面，依次 automation、publishing、adviser、codex、feishu、desktop、core 执行 `& $dsh plugin --profile desktop remove '@telebot/名称'`，移除对应 patch 条目。保留配置/凭据/会话/业务数据；停用使用 `disabled: true`。

## 最小安装组合与调用

| 场景 | 安装组合 | 前置/使用方式 |
|---|---|---|
| 原生 Codex | core + desktop + codex | 已有本地 Codex 0.160.0；配置 CLI；原生会话自然语言选择 telebot_codex |
| 原生顾问 | core + desktop + adviser | 可选；已有可用 OpenCLI 时才启用，本轮不配置账户 |
| 原生报告/发布 | core + desktop + publishing | 报告无需其他业务包；发布填文章目录，Git 另需仓库/登录/分支 |
| 原生提醒/研究 | core + desktop + automation | 官方 schedule；提醒可独立运行，报告增加 publishing，codex_apply 增加 codex |
| 飞书入口 | core + feishu + 所需业务包 | 飞书资源 CRUD 已在统一 feishu 包；普通渠道无需全部可选包 |
| 双入口 | core + desktop + feishu + 所需业务包 | 同一 profile 共用业务服务；原生完整权限不扩大飞书权限；独立 webhook 端口隔离 |
| 知识库 | dsh-knowledge@0.5.2 | 独立上游 bundle，不依赖本项目 core/feishu；使用原生工具与面板 |

先装 core，再装入口和所需业务包，启用对应行。同一个 profile 是共享服务边界；另一个 profile 的安装不会自动出现在 desktop。自然语言调用依赖当前模型和 preset 暴露的工具目录；结果返回实际执行状态。原生历史、任务列表、停止/续聊由 telebot_desktop 控制工具衔接。

## 全部安装方式及条件

| 方式 | 当前证据 | 操作与边界 |
|---|---|---|
| 官方 CLI + 本地 tgz | 隔离验收及本机正式 Electron 安装/启动通过；本批首选 | 本文命令；desktop 用桌面自带 CLI；可把 dist 移到 D 盘交付目录后用绝对路径 |
| 官方原生 UI + 同一 tgz | rc.5 官方支持；已核对隔离 Web UI 的归档输入，正式 Electron 安装未代执行 | 侧栏“插件”→“添加插件/安装第三方插件”；逐包输入 file:D:/绝对路径/telebot-core-0.1.0.tgz 等，core 先装；安装后启用包及 telebot-desktop 行。D 盘准备先按本文完成 |
| 源码构建后安装 tgz | 本轮构建、打包及归档安装已验证 | Node >=24.9、pnpm 11.28.5；按 README 构建并运行 pnpm pack:plugins，随后用以上 CLI/UI 安装 dist 归档；不直接加载源码 |
| 官方 CLI/UI + 解压后的发布目录 | 官方支持本地绝对目录；本批未单独验收 | 解压 tgz 的完整 package 目录，含 package.json、lib 和 patch；使用 file:D:/发布目录/package，长期保留目录。不能指向项目根、源码包或 workspace/link 路径 |
| 独立官方 Web profile | 本轮隔离验证的载体 | 同版本 dsh --profile 名称 --from-default-profile web --dump-config 初始化；安装相同 tgz 后启动。独立 home 可放 D，不共享 desktop 的已加载实例 |
| npm 包名、HTTPS tgz、Git/市场 | 官方支持；自有插件当前尚未发布这些入口 | 暂不能输入 @telebot/codex 或整个 monorepo Git URL完成安装；发布独立包/归档后需验证。上游知识库已可用 dsh-knowledge@0.5.2 包名安装 |

UI 与 CLI 使用同一官方管理器；无需另一个加载器。当前没有统一的安装前 D 盘目录/联接迁移向导，业务配置已有各自本机 UI。空机器完全离线安装未承诺，只有固定 peer/依赖已缓存齐全时才可能。插件配置页面没有导入/OAuth/scopes/测试字段。远程修复后不断线更新仅留愿景，本轮没有实现；本指南是手动停机安装。
