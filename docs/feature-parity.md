# 功能对照与交付状态

更新：2026-10-30。此表只维护旧能力映射与剩余差距；当前阶段和验证摘要见 [状态页](status.md)，不重复维护通过计数。

| 功能 | 参考入口/模块 | 新插件/宿主能力 | 必须保留的行为 | 当前状态 |
|---|---|---|---|---|
| 官方本地安装 | Harness 原生 plugin manager | core 与各功能 bundle | tgz 安装、精确版本、单一服务身份、启停/卸载与源码独立 | 七个生产包 + diagnostics 在外部官方 Windows ACP/Web 安装、启停、卸载通过；默认不打包 fixture |
| 首次配置/UI | 原 env 配置 | 统一 feishu setup + 官方 credentials | 文件持久化/来源凭据/本机访问/热变更与回滚 | 本地及实际官方宿主引导通过；正式页面支持 Base 链接，已移除测试专用区，保存保留文件中的接入限制；其他插件的重要参数 UI、独立配置文件/目录及缺项引导已实现并验证 |
| Agent/会话 | Python workflows/agents 仅作行为参考 | 官方 Agent/Session，经 core 接入 | 持久恢复、串行输入、错误与取消，不复制循环 | P1 完成：app/chat/thread 映射、串行/并发、取消、正常及崩溃恢复，未知执行不重交 |
| webhook | channels/feishu.py、app.py | feishu | challenge、来源校验、可信身份、消息/卡片去重、快速接收与后台执行 | P1 完成：token/原始体签名/AES、管理员、SQLite inbox/outbox、卡片归属绑定；真实租户待联调 |
| 消息合并 | channels/feishu_batching.py | feishu | 同用户/会话 3 秒合并、最长 10 秒；命令/确认即时分流 | 已实现并本地测试：同 actor/会话 3 秒、最长 10 秒，命令/附件屏障及控制旁路 |
| 回复与进度 | agents/feishu_streaming.py、channels/feishu.py | feishu | 富文本/卡片、限频、超长拆分、失败降级；默认不外发推理 | 最终 post/卡片、6000 字符拆分、正文进度最多 5 次/至少 1 秒、不外发 reasoning；unknown 进度不阻塞最终回复 |
| 命令/模型 | router 命令；参考飞书插件 | core + 官方 commands/模型目录 | 命令不进模型；模型选择真实生效、持久恢复且不污染其他会话 | 生产 CommandAdapter；compact 许可表；/model 实际请求及恢复通过；/new、/reset、/permission、/setting 已接线，更多命令按兼容表增加 |
| 工作目录 | tools/workspace_policy.py、workspace_resolver.py | core + 官方 Workspace/Session | 目录消歧、允许根、实际 cwd 一致；切目录不继承旧授权 | /cd已接通：新Session、realpath/junction允许根、忙碌拒绝、实际Shell cwd及重启保持通过；仅限配置workspace内部 |
| 文件操作 | tools/file_system.py、file_mutations.py | 官方 fs/Shell + 本地策略 | 读根/敏感路径、分页续读、覆盖确认和隔离删除 | 使用宿主文件工具/Shell 与原生审批；本地目录/junction 与敏感出站路径拒绝已测。完整旧隔离删除/分页工具等价仍随 P5–P7 对照，未宣称所有旧文件工具已迁移 |
| 附件收发 | channels/feishu.py、persistence/attachment_store.py | feishu + 官方附件/文件接口 | 24 小时暂存、先文件后需求/引用、重启恢复；显式入库、本轮产物及逐文件回执 | 已实现：24 小时用户绑定暂存、先文件后需求/本人引用/重启、原生文件/图片块、双向实际 HTTP mock、哈希投递；30 MiB 限制。真实格式及 provider 兼容待验收 |
| Shell/后台任务 | tools/shell_sandbox.py 仅作边界参考 | 官方 Bash/PowerShell、sandbox/jobs | 不复制执行器；cwd、超时/后台区别、退出码、取消和限制 | Windows cwd/提权拒绝已通过；P0 新增超时转后台、任务归属/取消和受限工具直接调用拒绝通过；复杂进程、业务限制和其他 OS 待验收 |
| 云文档与来源 | knowledge/feishu_docs.py、cloud_sources.py | 统一 feishu / knowledge | docx/wiki/sheets/base 范围、分页、导入/刷新及证据；补齐创建/追加 | P3 docx/wiki/Drive/Sheets 读取、创建/追加已实现；分页及精确 range/覆盖说明。KB 云来源导入/刷新仍为 P5，真实 API 效果待验收 |
| Bitable | bitable/、router/bitable_service.py | 统一 feishu business | 五工具、名称消歧、冻结预览、确认/补充期限、unknown、幂等、相关字段核验与回读 | 统一飞书 business 模块和五工具/旧名别名；冻结目标与字段/15 分钟/补充原期限/取消/冲突/unknown/回读对账/单写者已测；官方组合实际 mock 写入通过；指定 Base 的 9 项真实检查通过且已清理；大批次/其他资源仍待验收 |
| 日历 | harness-lark 的 calendar 工具参考 | 统一 feishu business | 明确资源/时区/重复范围/通知；CRUD 与身份策略 | 可访问日历、事件列表/详情/CRUD 已实现；显式时区/通知/single-all 与 occurrence ID；本地 HTTP 通过，真实时区/重复/通知待验收 |
| 飞书任务 | harness-lark 的 task 工具参考 | 统一 feishu business | 列表/详情、创建/修改/完成/删除；负责人、截止时间与通知 | 列表/详情/创建/修改/完成/删除已实现；负责人/截止/清单显式，完成可单独 patch；本地 HTTP 通过，真实资源与通知待验收 |
| Codex 执行 | tools/codex_runner.py、router/codex_service.py | codex | inspect/plan 强制只读、apply 授权、20 分钟预算、真实结果与本轮产物 | 独立 bundle；实际 CLI 0.160.0 + MXC/loopback 验证 schema、无害读/写拒绝/授权文件哈希、取消；15 分钟持久授权及 20 分钟预算，产物独立投递 |
| 原生 Codex 历史/续聊 | tools/codex_history.py、router/codex_history_service.py | codex 原生连接器 | 管理员私聊、分页/原生线程、归档只读、恢复权限重置与未知状态对账 | 原生分页/详情/重启/归档读取通过；生产私聊 owner/归档只读/一次性续聊/目录重检及权限重置已接线；不继承历史宽权限 |
| Adviser | tools/adviser.py | adviser | 无状态第二意见、脱敏 question/context、180 秒可配置超时、有界缓存、失败降级 | 独立 bundle、无 shell 的 OpenCLI 无状态进程适配；脱敏/LRU/超时/取消/降级通过，官方启停通过；用户本轮不配置/验收 OpenCLI 账户，不作为 Codex 前置条件 |
| 知识库 | knowledge/、persistence/kb_store.py | 外部 dsh-knowledge@0.5.2 | 以该项目替换自建方案；用户取消旧库数据迁移和飞书空间绑定 | 已通过官方 manager 安装与关键词检索/持久化验证，自带管理 UI；两项取消内容不再列为缺口，其他旧入库/权限/预算语义不自动继承 |
| 联网搜索/提取 | tools/bailian_web.py | 官方 web provider 或独立适配 | 实际来源与调用回执、截断说明、原有任务范围不扩大 | 复用官方 web_search/web_fetch，受限研究单独装配网页工具；真实 provider 凭据/外网质量按官方配置验收 |
| 报告/发布 | tools/write_report.py、blog_publish.py、git_push.py | publishing | 原文保真、哈希去重、完整批次、指定文件提交/真实授权、产物交付 | 已实现；报告/原文保真/指定文件 Git 经实际官方 Shell + loopback HTTP 组合验证，真实远程/飞书回传待验收 |
| Skill | skills/、scheduled/codex_artifacts.py | 官方 Skill + 插件策略 | 按需加载、原配置只读、运行副本与持久去重状态 | 已实现；选定 Skill 源码哈希、受控副本和持久状态，经真实 Codex 沙箱验证，原目录保留 |
| 定时管理 | scheduled/service.py、proposals.py、store.py | automation + 官方 schedule | 自然语言/显式 CRUD、once/daily/weekly/interval/count/until、带时区确认、审计 | automation 已实现，规则/提案/创建与执行分离/审计，实际官方计时组合通过，真实飞书待验收 |
| 定时执行/恢复 | scheduled/worker.py、execution.py、delivery.py | automation + 官方计时/执行能力 | remind/auto/confirm、skip/run_once/ask、一个时钟/派发方、occurrence 防重、范围与出站重试 | auto/confirm、补跑/变更/停止/unknown 与逐文件投递已实现，实际宿主组合通过，真实任务闭环待验收 |
| 跨平台与数据 | 原 paths/workspace 策略及旧库格式 | 各插件 + 官方宿主 | Windows/Linux/macOS 原生验证、独立数据根、备份/副本导入与恢复 | 未验收；不默认读取旧数据 |

ACP 用于自动化控制官方进程和验证原生 Agent。P1 的 webhook、管理员、卡片身份和收发账本另经真实 HTTP 入口回放验证；平台边界为 loopback mock，不代表已验收真实租户入站。原生命令和业务授权另测，见 [测试入口](../tests/README.md)。

P3 全部使用应用身份，删除用户 OAuth。逻辑能力不等于应用 API 权限；用户已确认后台权限开通，资源 ACL、消息回调、通知及复杂文件展示仍待验收。指定 Base 与三条群发送的覆盖单独记录，见 [飞书清单](feishu-live-checklist.md)。
