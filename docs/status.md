# 开发状态与接续入口

核对：2026-11-01；分支 codex/compat-desktop-0.2.1-rc.5；本次安装记录与重建代码一并提交（提交号见 git log）。唯一计划为 HARNESS_DEVELOPMENT_PLAN.md。

## 已交付

- P0–P6 代码及 Windows 隔离验收完成；P7 的 Windows 归档安装/启停/卸载通过；P8 原生入口实现及公开原生 RPC 验收完成。七个自有包：core、desktop、统一 feishu、codex、adviser、publishing、automation；外部 KB 为 dsh-knowledge@0.5.2。
- 精确兼容官方 0.2.1-rc.5、Cordis 4.0.5、schemastery 3.18.5。2026-11-01 已用桌面随附 rc.5 CLI 正式安装七个自有包及 dsh-knowledge@0.5.2；adviser 保持停用。整个 desktop profile 与依赖位于 D 盘，原路径用目录联接兼容；只联接 node_modules 会导致官方宿主模块解析失败，已改正。安装前已备份配置/凭据/会话，详见[安装与启动](installation.md)。
- 原生入口：core + desktop + 所需业务包即可，无飞书凭据；从官方所有者 RPC 取得身份，默认完整权限，inspect/plan 保持只读。支持实际写入/发布/Git/任务、文件附件、冷恢复；非所有者、ACP、后台回执不能取得原生权限。UI/独立配置目录已验证。见[原生入口](native-desktop.md)。
- 飞书：原管理员/目录/冻结确认继续生效，双入口已验隔离。端口和独立 webhook 服务、Base 链接 UI/可信自然语言管理保留；无导入、OAuth/scopes、测试字段。模型和计时复用官方宿主。
- Codex：原生 0.160.0，保留账户/配置；飞书/受限模式限定工作区、关闭网络/临时写根；原生完整权限使用官方 full access，已实测受控 cwd 外写入、回环 HTTP、历史和续聊。冻结/去重/unknown、Skill 副本、CSV/XLSX、产物哈希保留；已拒绝旧 apply-token 确认别名，模型不能消费真实用户提案。
- 发布/自动化：新建报告、原文保真、指定文件 Git、恢复副本和独立投递账本；官方唯一时钟。飞书创建与 auto 授权仍分开，原生完整权限 auto 创建即授权，显式 confirm 按次确认。未知结果不重做业务。

## 验证与剩余边界

本地 74；官方基础 11、P0 18、飞书 ACP/Web 13/13、完整业务 25/25、普通配置/桌面端口模拟 14/14；原生入口 16、真实 Codex 16、新原生页面 QA 3。上游 KB 6、较早页面 QA 4 保留为此前证据。报告位置、SHA-256 与当前归档绑定只维护在 [latest.json](../tracking_outputs/latest.json)。

原生入口覆盖四种**完全不安装飞书包**的最小组合、真实官方 Web 所有者 RPC、非所有者/ACP/回执拒绝、官方 Shell Git 回环推送、任务附件和冷恢复；另有双入口模拟飞书检查。它使用桌面共用的 SessionController。另已完成正式 Electron 启动落盘检查 5 项：七包哈希/版本、KB 精确版本、D 盘 profile、六个存活配置写者及四类业务数据库。未发模型请求；正式 UI 和实际账户端到端验收仍不能由这些检查替代。浏览器检查因自动审批服务额度不足受阻。

真实飞书入站/按钮/复杂媒体/资源 ACL/通知/产物/切流仍按[联调清单](feishu-live-checklist.md)关闭；此前指定 Base 9 项与获批三条群发送是历史证据，本轮无新增真实发送或公网切换。Linux/macOS、真实 OpenCLI 账户按用户决定待验/不开展。真实搜索 provider 与真实 Git 认证需用户环境核对；Windows Git file 传输的 MSYS 沙箱限制仍保留，不通过关闭沙箱解决。

## 接续只读

先本页与 AGENTS.md，再读计划相关小节和[测试分组](../tests/README.md)。不默认读 archive。正式安装已执行；日常用 D 盘交付目录中的 start-desktop.ps1 启动，确保 PowerShell/Git 与缓存环境稳定。飞书配置 UI 已生成，仍待本人手填应用凭据/管理员/工作目录及资源，当前没有监听 3000、没有公网切流。D 盘缓存/依赖/临时目录要求继续，环境脚本不修改 ACL。各插件业务 UI 已有，统一安装前迁移向导未实现。远程单插件不断线更新仅留愿景，不实施、不作为门禁。仅更新本页与 latest.json，不新增并列进度长文。
