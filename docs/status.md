# 开发状态与接续入口

核对：2026-11-01；分支 codex/compat-desktop-0.2.1-rc.5。唯一计划为 HARNESS_DEVELOPMENT_PLAN.md；提交状态见 git log。

## 已交付

- P0–P8 已实现并完成 Windows 隔离验收。七个自有包：core、desktop、统一 feishu、codex、adviser、publishing、automation；外部 KB 为 dsh-knowledge@0.5.2。精确兼容官方 0.2.1-rc.5、Cordis 4.0.5、schemastery 3.18.5。
- 配置已迁入官方桌面 **设置 → Telebot 插件**：使用 dsh.client/settings.section 注册 React 页面，复用宿主主题、设置导航和官方 Connection 所有者认证；没有独立 localhost 页面、iframe 或配置访问令牌。六个启用插件有配置页；adviser 保持停用，启用后出现；知识库使用上游页面。
- 飞书手填应用配置，端口可修改，Base 链接独立增删；无导入、OAuth/scopes 或测试字段。保存仍保留原子文件、凭据分离、单写者/revision、忙碌保护和失败回滚；旧 endpoint.json/setup.token 在升级启动时移除，配置/凭据/业务数据保留。
- 原生入口默认完整权限，身份来自官方所有者 RPC；inspect/plan 始终只读，非所有者、ACP、后台回执不能取得原生权限；飞书管理员/目录/冻结确认隔离。Codex 沿用本机 0.160.0 账户和配置，保留历史、Skill 副本、产物哈希与一次性确认。发布保持原文/新文件/指定 Git 文件规则；自动化复用官方唯一时钟和独立投递账本，unknown 不重做业务。
- 正式 desktop profile 已更新并启动。整个 profile/依赖在 D 盘，原 C 盘入口为 Junction；不要只联接 node_modules。正式原生配置 API 和客户端模块注入通过 8 项检查。安装包位于 `D:/files_data/windborne/dsh-runtime/releases/telebot-0.1.0-desktop-ui-20261101-r3`，启动/备份见[安装指南](installation.md)。

## 验证与剩余边界

本地 77（含原生配置组件交互 3）；官方基础 11、P0 18、飞书 ACP/Web 13/13、完整业务 25/25、普通配置/桌面端口模拟 14/14、原生入口 16。真实 Codex 16、上游 KB 6 为后端未变的保留证据。报告位置、SHA-256 与最终归档绑定只维护在 [latest.json](../tracking_outputs/latest.json)。

用户明确自行做视觉验收，本轮不执行页面点击/截图验收；旧独立页面视觉报告仅作历史，不能作为新原生页面的证据。当前验证覆盖实际桌面启动/官方客户端模块、所有者配置 API、秘密分离保存、Base 增删、未保存修改/版本冲突保护、重启和卸载保留。没有付费模型请求或新增真实飞书发送。

飞书仍需本人在桌面页手填应用凭据、管理员、工作目录及资源；端口 3000 已作为配置保留，未切公网回调。入站/按钮/复杂媒体/ACL/通知/产物/切流按[真实清单](feishu-live-checklist.md)验收；此前指定 Base 与三条群测试仅为历史证据。Linux/macOS 待验；真实 OpenCLI 账户按用户决定不开展。真实搜索 provider/Git 认证另验；不通过关闭官方沙箱绕过 Windows Git file 传输的 MSYS IPC 限制。

## 接续只读

先本页和 AGENTS.md，再读计划相关小节与[测试分组](../tests/README.md)，不默认读 archive。使用官方管理器停机安装；D 盘缓存/依赖/临时目录要求继续，环境脚本不修改 ACL。没有统一安装前迁移向导。远程单插件不断线更新仅留愿景、不实施。仅更新本页与 latest.json，不另增并列进度长文。
