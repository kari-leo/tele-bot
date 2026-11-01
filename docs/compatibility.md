# 官方兼容与验证边界

目标固定官方 **0.2.1-rc.5**，Cordis 4.0.5、schemastery 3.18.5；七个正式包和 diagnostics 均声明精确版本。当前分支为 codex/compat-desktop-0.2.1-rc.5。完整数量/报告哈希以 [latest.json](../tracking_outputs/latest.json) 为准，不在多处重复维护数字。

Windows 验收使用仓库外原版发行、官方 plugin manager、ACP v1 SDK、隔离 home 和 loopback 模型/飞书；没有付费模型、真实用户历史或新增租户消息。包括编译/本地测试、归档内容/哈希、干净安装/停用/卸载、原生 Shell、命令/模型/目录、崩溃 unknown、防重、配置凭据分离/Origin/令牌/忙碌/回滚、四类应用业务、发布与 auto/confirm 任务。受限研究单独装配官方网页工具，不挂载宽 preset，并等待限制策略加载；真实外部 web provider 凭据及质量另验。

rc.5 适配的关键差异：Agent.model 不存在，改用公开 installModelSelection；没有 schedule/resolve-agent，改用持久 receipt preset + 官方 SessionController。冷恢复专项验证其仍无 Shell/业务能力且不调用模型。schedule inbox/task 两次提交的崩溃窗口仍由独立 occurrence/unknown 账本处理。

桌面端口模拟使用实际官方 Web/ACP 组合：根 webServer 端口 0，飞书隔离官方 webServer 使用指定端口，测试保存/重启及根配置页面同时可用。这不等于启动并验收了 Electron 桌面。2026-11-01 已通过随附 rc.5 CLI 安装正式 desktop profile，六个启用插件生成配置入口、四类业务数据库完成初始化。整个 profile 位于 D 盘并联接回原路径；仅联接 node_modules 的解析失败已修复。正式 UI/实际账户业务另行验收，见[安装方式](installation.md)。

Git 提交/推送经官方 Shell 和本地合成 Smart HTTP/bare 仓库验证。Windows 本地 file 传输会在 MSYS sh 创建信号管道时失败，这是原版沙箱限制；保留已提交/未确认推送回执，不自动重做，不关闭沙箱。真实 HTTPS/SSH 远程及既有认证需另验，未推送用户仓库。

原生 Codex 0.160.0 本轮 app-server 隔离检查 16 项，含原有 MXC 只读/工作区限制、CSV/XLSX/Skill，以及原生所有者完整权限的受控 cwd 外写入、回环 HTTP、历史/续聊与去重。组合门禁另用进程替身检查接线与授权，二者不混算。

开发 workspace 设置 autoInstallPeers=false，刻意只安装编译所需宿主 peers；额外 `pnpm peers check` 会报告上游传递 dev peers 未闭合，不作为正式宿主安装的完整性声明。正式解析/服务身份由外部官方发行组合门禁验证；不向源码仓库引入一份完整 Harness 来消除该开发提示。

指定 Base 9 项、获批三条群发送/自身回读/微型文件哈希是历史真实证据；本轮未新增发送或读取其他历史。公网此前 502，本轮未证明新插件真实入站。真实按钮、媒体、资源 ACL/通知/任务结果和切流按[联调清单](feishu-live-checklist.md)验收。Linux/macOS、真实 adviser 账户按用户决定本轮不验。

P8 以真实官方 Web Connection cookie/RPC 验证四种不装飞书包的最小组合，以及非所有者/ACP/后台回执拒绝、完整权限官方 Shell Git/定时文件、冷恢复和模拟双入口。配置页默认完整权限及保存/刷新与实际文件一致，官方安装 UI 已检查归档输入；正式安装通过随附 CLI 完成，未使用 Electron UI 安装。包身份、工具和 Agent 均来自原版公共 API，未修改官方源码。
