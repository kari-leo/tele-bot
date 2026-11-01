# 自动化验收

测试分为本地、官方宿主/配置组合与明确授权的真实 API。54 项本地测试、宿主 11 项、P0 17 项、飞书 ACP/Web 各 13/13 项、全业务 ACP/Web 各 19/19 项、配置引导 11 项通过；Codex 保持此前 9 项实际 CLI 证据。当前是四个生产包和一个夹具，模型自动化只使用 loopback。真实发送与模拟报告分开记录。

| 检查 | 通过数/结论 |
|---|---:|
| 本地测试（先构建五个目标） | 54/54 |
| 官方宿主 / P0 | 11 / 17 |
| 飞书基础 ACP / Web | 13 / 13 |
| 全业务 ACP / Web | 19 / 19 |
| 实际官方配置引导 | 11 |
| 实际原生 Codex（沿用未改动后端证据） | 9 |
| 四生产包 + diagnostics 打包/检查 | 5 包 |
| 真实指定 Base | 9，清理完成 |
| 明确批准的三条真实群测试 | 3，发送/回读/文件哈希通过 |
| 公网回调 / 群资料查询 | 502 / 权限不足，待验收 |

## 建立外部宿主

在仓库外选择一个全新的安装目录，单独安装 `@deepseek-ai/dsh@0.2.2-alpha.2`。不使用本项目 node_modules 充当宿主，也不从已中断且依赖不完整的安装继续作兼容结论。

```powershell
$harnessInstall = Join-Path $env:TEMP 'telebot-official-host'
npm install --prefix $harnessInstall --save-exact @deepseek-ai/dsh@0.2.2-alpha.2
$env:TELEBOT_DSH_INSTALL = $harnessInstall
pnpm build
pnpm pack:plugins --fixtures
pnpm verify:harness
pnpm verify:feishu
pnpm verify:feishu --web
```

macOS/Linux 同样设置 `TELEBOT_DSH_INSTALL` 为仓库外 npm 安装 prefix 的绝对路径。脚本核对实际解析位置与版本，拒绝仓库目录及指向仓库的别名。

## ACP 验收内容

- 官方 plugin manager 安装 tgz，校验精确版本拒绝，确认实际模块位于仓库外。
- 暂时移走安装源 tarball 后，从官方 ACP profile 初始化并创建两个独立 cwd 会话。
- mock 模型实际调用已安装的 echo 工具；检查工具结果和 ACP 完成事件，以及 core 的共享服务身份。
- 官方 Shell 在空格/中文 cwd 执行；请求更宽权限后由 ACP 拒绝，确认临时工作区外的写入没有发生。请求值来自官方工具目录，不改变会话权限预设。
- 从官方模型目录选择第二个 mock 模型，核对下一次实际 HTTP 请求采用新模型。
- 一个会话等待模型时另一个仍可执行；通过 `session/cancel` 取消前者并核对 stopReason。
- close、进程退出、重启、list、resume，检查历史及模型选择持久化、不重放旧回复。
- 停用 fixture 后工具从模型目录消失，最后通过官方 manager 卸载两个 bundle。

验收结束写入外部临时目录的 `result.json`，并将结果复制到忽略的 `.runtime/verify-latest.json`；失败也保留已通过项、错误和有限的测试请求摘要。仅成功运行才可登记平台兼容。

ACP 进程使用临时 DSH_HOME、用户 home 和环境白名单，不继承真实模型密钥、NODE_PATH 或 NODE_OPTIONS，所有模型请求发往 loopback。需要网络的是官方包和插件 peer 的安装，不是模型调用。测试审批请求默认拒绝；不能为跑通测试切换为全盘权限。

## P1 飞书真实组合

`pnpm verify:feishu` 从官方 ACP profile 派生，并追加官方 webServer；`pnpm verify:feishu --web` 从官方 Web profile 派生，仅追加官方 ACP 作为测试入口，并关闭 URL 打印/自动打开浏览器。后者实际加载随附的 feishu.patch.example.yml，覆盖环境变量求值和原生 standard preset 挂载。两种组合都经官方 manager 安装当前四个生产 tarball 与 diagnostics（基础回归只启用 core/feishu/diagnostics），移走安装源后运行同样13项场景（P1十一项、P2两项）：

- 通过实际 HTTP 路由验证 challenge、来源 token、签名/AES、app、管理员及机器人忽略。
- Feishu 输入触发真实原生工具，检查实际工具回执进入模型与出站消息；event/message 双重去重。
- 模型请求保持未结束时 webhook 仍返回接收结果；同会话排队、不同 chat/thread 并发。
- 状态命令、绑定操作人/chat/消息的一次性卡片和 `/stop`；拒绝伪造字段。
- 确认限流只重试同一 UUID 投递；断连未知发送不会重跑 Agent。
- 正常重启保持映射、历史、去重与 unknown；强制杀死真实测试进程后，待处理输入恢复、已运行输入不重交。
- 发送中强制退出保持 unknown，不自动重发；停用移除路由/worker；官方卸载保留数据。
- /model无模型辅助解析，目录校验后改变下一次实际HTTP模型请求；不同chat不串选择。
- /cd准备独立原生Session，核对真实Shell cwd与允许根拒绝，并在重启后保持cwd/model。

结果分别写入 `.runtime/verify-feishu-latest.json` 和 `.runtime/verify-feishu-web-latest.json`，完整测试 home 和日志保留在外部临时目录。报告仅包含假身份与计数，不导入原项目配置。`node:sqlite` 属于 Node 内置模块，需 Node >=24.9。故障重启测试会等待默认 10 秒租约过期，单次调用和测试均有超时。

飞书模型入口直接调用 core 的公开 Harness 适配；ACP 不作为另一个模型循环或生产 webhook 协议。loopback 飞书 API 检查了应用 token、reply 路由、线程标志与 UUID，但不证明真实平台的权限、内容渲染或网络代理已正确配置。部署前需另做测试应用联调。

## ACP 的覆盖范围

本次固定版本使用稳定 ACP v1，不使用实验 v2。ACP 提供 Agent 会话/提示/事件/取消/恢复和模型配置，未提供原生命令、终端 UI、飞书回调或业务授权的等价接口。那些能力分别通过公开服务适配、渠道回放及业务契约验证，不能因 ACP 测试通过就宣布完成。

协议依据：[Harness ACP](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/acp/acp/README.md)、[ACP 官方 TypeScript SDK](https://github.com/agentclientprotocol/typescript-sdk)。实际验收以安装版本随包的公开文档与行为为准。

## P0–P4 业务验收

在上述外部官方安装与带 fixtures 打包之后，额外执行：

```powershell
pnpm verify:p0
pnpm verify:features
pnpm verify:features --web
$env:TELEBOT_CODEX_CLI = 'C:/absolute/path/to/@openai/codex/bin/codex.js'
pnpm verify:codex
```

P0 完整 17 项包括原生命令/作用域/Shell jobs/schedule 结构化回执及故障观察，并保留基础恢复/启停卸载。旧周期、星期转换、补跑和 occurrence 授权策略另由本地 schedule-prototype 测试覆盖，未冒充 P6 生产自动化。

features 两种 profile 各 19/19 项，包括基础 13 项回归以及已安装的 Codex/adviser、一次性写入授权/产物文件回传、Bitable 冻结确认/写入回读、实际宿主用户问题/拒绝伪造回答、附件上传下载、官方普通提醒、业务停用后主聊天仍可用。Codex/OpenCLI 在该组合中使用外部临时协议/进程替身；单独 verify:codex 才使用真实原生 CLI。报告为 .runtime/verify-features[-web]-latest.json。

原生 CLI 固定 0.160.0，测试启动和生产连接器校验版本及公开实验 named permission profiles。测试 home 的 Windows 配置为：

```toml
[features]
prefer_mxc = true
[windows]
sandbox = "mxc"
```

仅修改测试隔离 home，不修改用户全局配置。9 项核对只读/never 回合、原生历史分页/读取/重启恢复/归档、生产 schema、无害读、实际写入拒绝、取消，以及真实授权文件与 SHA-256/重复确认不重执行。没有通过 full-access 规避沙箱；缺 MXC/不匹配版本应报告失败而非算通过。其他平台和 CLI 版本另验收。报告为 .runtime/verify-codex-latest.json。

## 配置和真实环境验收

```powershell
pnpm verify:feishu:setup
pnpm preflight:feishu --reference 'D:/external/local.env' --workspace 'D:/test-workspace'
pnpm verify:bitable:live --reference 'D:/external/local.env' --base 'https://tenant.feishu.cn/base/EXPLICIT_BASE'
# 在人已明确授权的 Base 上才加 --write：仅写独立标记记录并清理。
pnpm verify:bitable:live --reference 'D:/external/local.env' --base 'https://tenant.feishu.cn/base/EXPLICIT_BASE' --write
# 在人已明确授权的专用 chat 上才加 --send：三个小型固定类型测试。
pnpm verify:feishu:live --reference 'D:/external/local.env' --chat 'oc_EXPLICIT_TEST_CHAT' --send
```

setup 使用实际官方 manager/进程、隔离 home 和假凭据，验证 11 项配置生命周期。--preview 可停在假配置页面供浏览器 QA，按 Enter 后完成卸载保留测试；不用于真实应用环境。

真实 Base 工具默认只读，--write 只触碰本轮创建的独立记录、禁止表结构/旧记录变更，清理失败列剩余 ID。真实 chat 工具默认仅检查资料，--send 明确启用三条获批内容，持久记录 UUID/消息 ID；重跑只回读成功项，未知发送先对账。--callback 仅做不带密钥的 GET 可达性检查，不更改后台或证明平台入站。群资料读取与发送权限分开记录，不能用更高权限绕过拒绝。

本轮预检确认 Verification Token 已补齐；指定 Base 9 项、专用群 3 项通过。公网当前 502，剩余按 [真实清单](../../feishu-live-checklist.md) 继续。运行报告不含密钥或原始聊天历史；真实数据和模拟假数据报告分开。
