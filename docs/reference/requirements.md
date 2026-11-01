# 开发计划的详细规格附录

本文件由 [唯一开发计划](../../HARNESS_DEVELOPMENT_PLAN.md) 纳入，保留目录、领域边界、R1–R5 和逐项验收规格；不是独立排期或当前状态记录。仅修改对应能力时阅读有关小节。规格中的“必须”表示目标要求，不表示已经完成。阶段与状态见总计划及 [状态页](../status.md)。

## 1. 目标、基线与约束

最终交付物是一组可本地安装到官方 DeepSeek Harness 的插件包。用户先独立安装官方 Harness，再通过其原生插件管理命令安装本项目构建的包，配置飞书及各能力后，在官方 Harness 中使用全部目标功能。项目不交付自带 Harness 的应用发行版，也不要求用户克隆、构建或运行本仓库中的 Harness 源码。

官方 Harness 可运行在本地或服务器；飞书沿用 webhook 发出指令、接收进度和结果、传输文件，并使用宿主的命令、模型、会话、工具和 Shell。Codex、adviser、知识库分别作为可独立启停的插件；定时任务复用官方计时机制，保留旧系统的授权、补跑、执行和投递边界。

**理想完成状态：**在一台只安装了受支持官方 Harness 的干净机器上，复制本项目的发布目录即可完成本地插件安装；从仓库外启动官方 Harness，在飞书上完成 R1–R5；移走插件源码和开发环境后仍可使用；卸载插件不改变官方 Harness 源码、不删除业务数据。基础安装可以依赖正常的包源下载，完全离线分发是另行明确的交付项，不能将本地安装等同于离线安装。

本计划以当前仓库为实施位置，参考以下本地源码快照，不假设它们与网络最新版一致：

| 来源 | 本次核对结果 | 使用方式 |
|---|---|---|
| `D:/files_data/windborne/tele_bot` | Python 飞书 webhook、附件、KB、Bitable、Codex、adviser、定时任务及测试 | 业务行为基线；提取规则和测试预期，不移植 LangGraph 主循环 |
| `D:/files_data/windborne/coding/dsh-lark-main` | 包版本 0.2.6；WebSocket 渠道、会话恢复、设置与凭据集成 | 参考窄渠道实现和官方服务接入方式 |
| `D:/files_data/windborne/coding/harness-lark-master` | 包版本 0.1.22；卡片、媒体、审批、命令、OAuth、文档/表格/日历/任务工具 | 按模块借鉴；重新验证 API、授权、分页和生命周期 |
| 当前仓库 | 七个生产插件、统一飞书及各插件配置/UI、诊断夹具、官方发行组合、受限真实 Base/群测试 | P0–P6 Windows 本地/官方组合完成范围见状态页；真实飞书/其他平台/生产切流待验收，不依赖旧运行时 |
| 本次核对的官方源码快照 | 0.2.2-alpha.2，commit `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | API 调研和首轮兼容候选；最终以同版本官方发行安装的验收结果为准 |

执行纪律：

1. 官方 Harness 是外部宿主，不纳入本项目 Git 子模块、源码树或交付 tarball；不替用户维护另一份 CLI/Web/Desktop。只通过官方 `dsh` profile、插件管理器及公开扩展点运行，不编写第二套 Agent 循环、插件加载器或通用 Shell 执行器。
2. 根据用户最新要求，本分支删除旧 Python 和旧脚手架，不保留 legacy，也不建立临时 Python 桥接。`D:/files_data/windborne/tele_bot` 仅作为仓库外行为参考，不修改它；用户凭据和业务数据不作为废弃代码删除，未来导入须另做备份与校验。
3. 领域使用本地 DTO/端口，不 import Cordis 或 `@deepseek-ai/*`；宿主 API 依赖集中在插件的 `harness/` 与公共适配包。diagnostics 是测试夹具，不是业务领域的分层模板。
4. 确定复用宿主启动、插件生命周期、Agent/Session、模型、命令、通用工具、Shell 和官方 schedule 计时；业务授权、KB、Bitable、发布、原生 Codex 历史与回执由插件持有。附件/Office/搜索/Codex 等同类实现按行为对照选择，不因包名相同就强迁。
5. 每个外部插件 bundle 声明精确测试过的 `engines.dsh`，实际 import 的宿主包声明匹配 peer；以官方发行安装验证解析一致性，不把 `link:../../vendor/...` 作为发布依赖或默认开发前提。`0.2.2-alpha.2` 已完成 core/diagnostics 的 Windows 基础组合验收；其他包/平台逐项验证，不扩张兼容结论。
6. 升级在兼容升级分支进行，固定官方发行版本及锁文件，检查 API/权限/数据差异并重跑安装与业务验收。若存在公开 API 缺口，提交最小公共扩展方案，等官方可安装版本满足后再宣称相应能力完成；不能以私有 fork 替代目标宿主。
7. 运行数据按用户官方 Harness 的实际 home/插件配置保存，不依赖本仓库 cwd；测试使用隔离 home。凭据、数据库、node_modules、lib、dist 和测试运行目录不提交。

本文是 HARNESS_DEVELOPMENT_PLAN.md 的需求与验收附录，吸收并取代旧解耦、迁移、模块审计、Codex 编排、Bitable 需求和内嵌 Harness 方案。旧行为以仓库外参考项目的源码/测试为基线，未实现的旧设想不自动成为新增承诺。旧组合实验不作为新分支官方发行兼容证据。

## 2. 需求到交付项

| 编号 | 需求 | 交付内容 | 验收标准 | 阶段 |
|---|---|---|---|---|
| R1 | 飞书连接本地/服务器 Harness，保留 webhook | 原 `/feishu/webhook` 路径、消息/卡片回调、会话恢复、文件双向传输 | 本地和 Linux 服务器均能收消息、执行工具、回传本轮文件；重试和重启不重复执行业务 | P1、P2、P7 |
| R2 | 使用 Harness 命令和工具，支持模型/目录切换及飞书平台能力 | 命令桥、模型目录、工作区切换、审批/提问、文档/Bitable/日历/任务 | 命令直接调用宿主服务；目录和模型实际生效；四类飞书业务有真实 API 回执 | P2、P3、P7 |
| R3 | Codex、adviser、知识库插件化 | 三个可单独本地安装的功能包、配置及依赖诊断 | 安装到外部官方 Harness；可独立启停，源码移走后仍运行；保留业务语义 | P0、P4、P5、P7 |
| R4 | 复用 Harness 定时插件，覆盖现有边界 | 官方 schedule + 本地任务策略、授权、运行/产物/投递账本 | remind/auto/confirm、周期、补跑、重启、去重和投递重试均通过对照测试 | P0、P2、P6 |
| R5 | 复用 Shell，适配多个操作系统 | 官方 Bash/PowerShell provider 与工具的 profile 装配 | Windows、Linux、macOS 的路径、编码、退出码、取消和权限行为经原生环境验证 | P2、P7 |

首个可用版本的范围为：webhook 文字闭环、会话恢复、命令、模型与目录切换、原生 Shell、文件上传下载、普通提醒。该版本用于内测，不能据此宣布完整迁移；正式切换必须满足 R1–R5 和旧业务清单。

## 3. 参考项目可复用内容与缺口

### 3.1 渠道与宿主连接

`dsh-lark-main/src/harness.ts` 有现存 Agent 复用、持久会话恢复、preset 挂载、workspace 关联等实现，适合作为最小桥接参考。其主要入口为 WebSocket，不能直接满足 webhook；其读取 Session 事件及总结 turn 的代码也需对照固定版本重新验证。

`harness-lark-master/src/channel/monitor.ts` 的实际接收路径调用 `startWS()`。虽然配置声明 `connectionMode: webhook`，不能据该字段认定 webhook 已实现。计划直接实现 webhook transport，复用解析和出站设计；WebSocket 只作为后续可选 transport，不成为首版前置条件。

参考项目的 `agent/commands.ts` 提供 `/model`、`/cd`、`/new` 等交互设计，但当前 Harness 已有 `dsh-commands` 服务。优先经其 `list/find/execute` 接入原生命令，缺少的渠道命令再注册薄适配；不复制一套与宿主分离的命令执行框架。

### 3.2 飞书平台能力

参考 `harness-lark-master` 的 `tools/doc.ts`、`tools/base-sheets-calendar-task.ts`、`messaging/outbound/media.ts`、审批和提问适配。复用时保留 MIT 声明和必要来源记录，并将业务规则放回本地领域服务。

旧 Python 已有云来源导入/读取与完整 Bitable 记录操作；本次静态核对未找到独立的日历事件、飞书任务 CRUD 实现。日历和任务仍是本次明确要求的交付项，归类为“补齐能力”；P0 再核对实际使用案例，不能标成已迁移。Harness 内部 todo、schedule 与飞书任务、飞书日历是不同资源。

不能整包照搬的差异包括：旧版 peer 依赖、默认开放私聊、用户 token 缺失时自动回退机器人身份、表格分页不完整以及缺少 Telebot 冻结预览/确认/幂等规则。以真实 API 和旧业务测试为准，不以 README 工具数量作为等价证明。

### 3.3 Harness 原生服务

| 能力 | 选定方案 | 仍需本地负责 |
|---|---|---|
| Agent/Session、模型目录与选择、preset、插件生命周期 | 直接复用官方服务 | 飞书身份、会话映射、命令授权、事件渲染 |
| 命令 | 复用 `dsh-commands`，暴露适合飞书的命令集 | 原生命令兼容清单、附件转换、未知命令反馈 |
| 通用工具和上下文管理 | 复用官方工具/preset/压缩/Skill 机制 | 旧能力覆盖、业务结果核验、受控工具暴露 |
| Shell | Windows 用官方 PowerShell，POSIX 用官方 Bash；每个执行上下文仅一个 provider | 部署探测、工作区/权限策略、飞书输出与取消反馈 |
| 定时 | 官方 schedule 为唯一计时权威 | 授权、业务 occurrence、补跑策略、执行/产物/出站账本 |
| 通用审批和用户问题 | 优先复用公开服务，增加飞书 answerer | 实际用户、回调绑定、超时、取消和重启失效规则 |
| Codex | 后端对照后选择，暂不强制官方 provider | inspect/plan/apply、原生历史续聊、工作区与产物核验 |
| 知识库/adviser | 本地功能插件，通过适配器注册 | RAG、预算、来源/证据、脱敏、降级和业务数据 |

## 4. 以可安装插件为中心的目录结构

调用关系：飞书 webhook → 可信事件与持久接收队列 → 渠道解析/合并/命令分流 → Harness 适配器 → 官方 Agent 或命令服务 → 官方工具/本地业务端口 → 业务回执与产物 → 飞书出站队列。模型处理只发生在 Harness 内部。

仓库以插件产品划分，而不是应用入口和技术层分别建大量包。目标根目录不包含 `apps/` 或上游 `vendor/`；官方 Harness 的安装目录、home、profile 属于使用方环境。目录按交付创建，未实现能力不生成空壳插件：

```text
tele-bot/
  README.md                         安装入口、当前可用能力与开发说明
  AGENTS.md                         唯一实施规则入口
  HARNESS_DEVELOPMENT_PLAN.md        唯一总开发计划
  package.json                      private workspace；只提供构建/测试/打包命令
  pnpm-workspace.yaml               plugins/*、internal/*、测试夹具
  pnpm-lock.yaml
  plugins/                          每个直接子目录对应一个可安装 bundle
    core/                           @telebot/core：宿主适配、公共服务定义/引用
    feishu/                         @telebot/feishu：setup、渠道与 business 业务模块
    codex/                          @telebot/codex：执行策略、原生历史/续聊与产物
    adviser/                        @telebot/adviser：第二意见 provider 与工具
    # knowledge 由外部 dsh-knowledge 提供；本仓库不创建重复 bundle
    automation/                     @telebot/automation：官方 schedule 上的业务策略
    publishing/                     @telebot/publishing：报告、原文发布与 Git 交付
  internal/                         private 构建期模块；不直接安装到宿主
    domain-kit/                     确有多消费者的最小 DTO/值校验；无宿主 import
    feishu-client/                  无业务规则的 SDK/HTTP 封装；不注册共享服务
  tooling/
    build.mjs                       编译各插件及其内部依赖
    pack.mjs                        生成 tgz、清单、校验和与本地安装说明
    verify-harness.mjs              对外部官方发行安装做 keyless 真实组合验收
    check-artifacts.mjs             检查包内容、运行时依赖与禁止的源码路径引用
    compatibility.json              已测试的官方版本、宿主 peer 与平台矩阵
  tests/
    acceptance/                     安装、启停、卸载、功能链路、重启和恢复
    fixtures/diagnostics/           迁入现有 diagnostics；不作为生产插件交付
    support/                        mock 模型/飞书/时钟与隔离 home 工具
  examples/
    feishu.settings.example.yaml    设置示例；不含凭据和绝对开发路径
    profile.patch.example.yml       官方 profile 的可选覆盖，不复制官方基座
    deployment/                     Windows/Linux/macOS 服务及反向代理示例
  docs/                             安装/操作/能力明细及测试报告；不另立竞争总计划
    installation.md
    feature-parity.md
    operations.md
    compatibility.md
  dist/                             忽略：插件 tgz、安装顺序、校验和、版本清单
  .runtime/                         忽略：测试临时 home、模型 mock 与证据
```

统一飞书包在 `plugins/feishu/src/` 内使用 setup、domain、adapters、harness 和 business 子模块；business 内保留独立 DTO/端口/适配器，不将业务规则混进渠道。首次/缺项配置由 Harness 桌面原生设置页引导并原子保存，密钥属于官方 credentials，普通文件只存引用。`plugins/core/src/` 继续持有公共宿主接缝与服务身份，不持有飞书业务规则。

本地接口按用例建立，例如 AgentPort、CommandPort、InteractionPort、FileAccessPort、ArtifactDeliveryPort、CodingExecutorPort、CodexHistoryPort、SchedulePort、AdviserPort、EmbeddingPort。上游类型留在 `harness/`；飞书业务由统一包内的独立模块承载；可停用 business 保留渠道，停用整包会卸载二者。Web/测试通过可信端口调用，不伪造 Actor。飞书云 API 身份由可信调用上下文和明确配置解析，Web 来源没有飞书用户授权时不能伪造 Actor。

跨插件服务的定义只从已安装的 `@telebot/core` 导入，core 作为 peer 外置，避免把同一服务类/注册身份分别打进多个包。内部无服务身份的领域辅助代码和客户端可随各插件构建内联；凭据存储和业务状态仍有明确所有者，不因复用客户端出现两套共享 token 状态库。

### 4.1 单个插件包与依赖规范

每个 `plugins/<feature>/` 包含 `package.json`、`cordis.patch.yml`、`src/index.ts`、功能源码、`tests/`、README、必要资源和构建配置。`lib/` 由构建生成。包需具备 `type: module`、有效 `exports`/`types`、精确 `engines.dsh`、`dsh.bundle.patch` 和最小 `files`；函数插件遵守官方导出形式，注册与资源由官方生命周期回收。

纯领域与 repository 端口可在包内独立测试。`internal/*` 不发布、不注册运行时服务，构建时将所需实现和类型闭包带入产物；发布包不得留有无法解析的 `workspace:`、本机 `link:`、指向源码的相对 import 或隐藏编译前置条件。宿主 Cordis/DSH 包和 core peer 必须外置；普通 SDK、解析库和数据库依赖精确锁定并按发行包实际安装验证。

### 4.2 安装单元和依赖关系

| 安装包 | 对宿主的贡献 | 依赖与独立性 |
|---|---|---|
| `@telebot/core` | 公共服务/DTO 引用、宿主端口实现与能力诊断 | 仅依赖公开 Harness 服务；不重复加载官方基座，不覆盖默认模型或全局 cwd |
| `@telebot/feishu` | 文件配置/本机 setup UI、webhook/会话/审批/附件/提醒及应用身份文档/Bitable/日历/任务 | core；一个安装与生命周期，business 可配置关闭，整个插件停用时二者一起停止 |
| `@telebot/codex` | inspect/plan/apply、历史/续聊、产物与取消 | core；可选原生 CLI/已验证 provider，不依赖 KB/adviser |
| `@telebot/adviser` | ask_adviser 及 provider 配置/状态 | core；外部顾问不可用不影响其他插件 |
| 外部 `dsh-knowledge` | 上游 KB 引擎、工具、配置和管理面板 | 官方宿主公共服务；直接安装 npm 包，不依赖 Telebot core 或消息网关 |
| `@telebot/automation` | schedule 上的任务管理、授权、执行与回执 | core + 官方 schedule；Codex/KB/出站是按任务验证的可选能力 |
| `@telebot/publishing` | 报告、保真发布、受控 Git 操作和产物 | core；不依赖飞书网关，飞书发送经可选交付端口完成 |

当前交付四个自有包，后续 automation/publishing 按实际能力创建；知识库改用外部 npm 包，不计入本仓库 tarball 集。最小飞书安装集仍是 core + feishu。不另建捆绑全部代码的 suite；dist/install-manifest.json 记录自有产物，tooling/external-plugins.json 记录上游版本。安装器只调用官方管理器，不参与加载/启停；中途失败保留备份并报告状态，不假装跨包原子回滚。

### 4.3 构建、分发与官方宿主安装

当前已有 `pnpm build`、`pnpm test`、`pnpm pack:plugins`、`pnpm check:artifacts` 和 `pnpm verify:harness`。真实组合检查使用外部官方发行宿主、官方 ACP v1 SDK、独立临时 home/用户目录和本机 mock 模型；检查真实工具、模型、取消、恢复及生命周期。默认开发使用固定官方包和 peer，不依赖源码 checkout。复现见 [ACP 测试](../testing.md)。

使用方安装不需要本仓库的 Node workspace。完整交付后，操作形式如下；版本号与文件名以实际 dist 清单为准，下面不是当前已有业务包的安装声明：

```text
# 独立安装受支持版本的官方 Harness（或使用已有的受支持安装）
# 桌面部署使用桌面自带 rc.5 CLI，完整步骤见 docs/installation.md

# 在使用方选择的官方 profile 中本地安装；其余功能包按清单继续 add
dsh plugin --profile web add file:/absolute/path/to/dist/telebot-core-<version>.tgz
dsh plugin --profile web add file:/absolute/path/to/dist/telebot-feishu-<version>.tgz

# 设置凭据与插件配置后，仍由官方 CLI 启动原 profile
dsh --profile web
```

CLI 的本地 tgz 安装、跨插件 peer、默认 profile 组合及平台原生依赖必须在 P0 以官方发行安装验证；若发布渠道没有候选版本，先确定实际可安装版本并重新核对 API，而不是继续依赖内嵌源码冒充发行版。Windows 示例使用绝对 Windows 路径和正确引号，配置文档分别给出三平台命令。

安装 bundle 使用独有 row ID/命名空间，只贡献本项目组件；不覆盖官方 Web/Desktop、默认模型配置、全局工作目录或无关插件。新增服务按实际 profile 声明依赖，缺少官方 capability 时给出明确启用方式；必要 patch 保存在 examples，不要求替换用户整个 profile。

### 4.4 干净分支与当前工程状态

根据用户要求，已删除本仓库旧 Python 源码/测试/入口、旧 requirements、旧 scripts/packages/harness/vendor、`.gitmodules` 及 submodule gitlink。没有保留 Python 兼容入口、旧启动器或 legacy 目录。diagnostics 的有用验证语义以新夹具重新实现；旧实验报告和日志不作为新验收依赖。

当前 workspace 使用 plugins/internal/fixtures，docs 可跟踪，运行数据/lib/dist/node_modules 保持忽略。宿主 peer 使用官方包精确版本，没有上游源码 link。core/diagnostics 已编译、打包并在外部官方发行版加载/调用/卸载；独立性与产物检查已建立，业务功能验收仍待逐项加入。

本分支后续只按当前结构开发；internal/examples 等目录有实际内容时再建立。既有 `.runtime/harness-home` 含凭据，作为忽略的用户数据保留且新工程不读取；其存在不代表仍保留旧应用。新验收使用全新临时 home。

### 4.5 独立功能插件要求

| 插件 | 必须拥有的职责 | 启停与故障边界 |
|---|---|---|
| `@telebot/codex` | 代码请求、只读/写入策略、计划确认、原生历史/续聊、结果/产物验证 | 未安装 CLI 或后端不兼容时只禁用本能力；停用取消本插件持有的任务/进程，并撤销相关待确认操作 |
| `@telebot/adviser` | 无状态第二意见、输入脱敏、缓存、超时/取消、结构化降级 | 顾问不可用时主任务可继续；不把降级标识或原始错误发给用户，不声称已获取意见 |
| 外部 `dsh-knowledge` | 上游库/文档/检索/管理 UI；旧身份/来源/预算差异另记 | 停用保留用户库；重启/关键词检索通过后再声明本机可用；旧语义不自动视为继承 |

每个 bundle 提供 manifest、配置 schema、依赖诊断、版本门禁和停用清理。生产 profile 默认不装 diagnostics。这三个功能包可按需安装；关闭其中一个不能造成重复注册或其他业务加载失败。持久数据删除另设显式管理流程，不与插件卸载混同。

## 5. 飞书接入、会话与文件

### 5.1 webhook 和身份

- 保留 `/feishu/webhook`，同时处理 URL challenge、`im.message.receive_v1` 和 `card.action.trigger`。按飞书 SDK/协议实现加密载荷与已配置的来源校验；令牌兼容规则单独测试，管理员配置为空时仍不授予业务权限。
- 将业务身份与 transport 校验分开。Actor 来源只允许经验证的事件/operator 字段，模型参数、附件文字、引用内容不得替换身份。沿用 `FEISHU_ADMIN_USER_IDS` 的统一管理员规则，不默认采用参考项目的 open 模式。
- 回调完成验证和持久接收后快速应答；模型、文件解析、外部 API 写入转后台队列。持久接收失败不返回“已接收成功”；重复 event/message/callback 关联同一处理记录。
- 接收队列记录 pending/running/completed/unknown；崩溃恢复不能把“尚未完成”简单当作“尚未执行”。外部写入由领域幂等账本决定是否可重试。
- webhook 监听或路由由插件挂接宿主公开 HTTP 能力；没有可用路由服务的 profile 给出依赖诊断，不启动另一套应用宿主。本地通过已有反向代理/隧道将回调转到本机；服务器经 HTTPS 反向代理转到 loopback 服务。管理页面与 webhook 的认证和路由分别配置，不暴露带认证 token 的 Harness 管理地址。

### 5.2 会话和进度

稳定映射键至少包含部署实例/应用、chat、thread 或 scope；用户待确认操作、待处理附件和短期意图再绑定 Actor。2026-10-30 用户已确认沿用旧规则：普通群聊共享 chat 会话，话题按 thread 隔离；附件与待确认操作仍绑定用户，不能因共享会话而跨用户复用授权。

恢复前先查询活跃 Agent，再通过公开 API 恢复持久 Session；仅明确不存在时创建。恢复失败不能悄悄新建空会话。会话绑定持久化，不从显示标题或文本反推归属。

保留同会话串行处理、普通消息 3 秒合并/最长 10 秒、命令和按钮即时分流、附件与文字先后发送合并等行为。`/stop` 和审批回答必须能越过正在等待模型完成的普通消息队列；否则长任务无法停止或回答确认。

观察官方 turn、文本、工具、交互和产物事件，在飞书更新状态卡片。默认最终回复保留旧富文本格式，进度卡片可折叠；卡片限频、超长拆分、失败降级为普通消息。推理内容默认不外发，避免照搬参考项目的完整 reasoning 面板。

### 5.3 本地文件操作和双向传输

“本地文件”指 Harness 所在机器的文件系统：本机部署对应本机路径，服务器部署对应服务器路径。远程 Harness 不自动获得用户电脑目录；电脑文件通过飞书上传到服务器，或未来另做显式受控节点连接。两种部署都必须在回复中标明实际执行环境和目录。

入站：消息资源 → 验证当前 chat 与资源权限 → 下载至本部署受控暂存区 → 持久附件引用 → 用户需求绑定 → 官方文件/图像接口或已验证解析器。出站：本轮产物引用 → 文件存在/路径/类型/大小/归属校验 → 飞书上传 → 发回原 chat/thread → 保存实际发送回执。

保留以下行为：

1. 单独文件先暂存，不自动分析或入库；24 小时内可随需求消费，重启后保留绑定；过期或已消费附件可经同 chat 引用重新下载。
2. 支持 TXT、Markdown、PDF、DOCX、CSV、XLSX；图片沿用 PNG/JPEG/WebP/GIF/BMP/TIFF 兼容目标。上游图像存储不支持的格式经明确转换或解析器适配，不能把所有附件当作图片。
3. 文档输入保留旧 30 MiB、单次正文 120,000 字符的产品限制；图片保留单张 20 MiB、单消息最多 8 张/合计 30 MiB 的目标。2026-10-30 用户已确认附件上限使用二进制定义：30 MiB = 31,457,280 字节，出站同此定义；此前的 30 MB 表述据此纠正。边界测试随 P2 附件实现补齐；实际平台/provider 限制更小时按实际限制报错。
4. 文件读/列举/搜索优先复用宿主能力；创建、覆盖、移动、隔离删除按旧规则验收。覆盖与删除保留摘要和原用户确认，删除默认隔离；允许根目录、敏感路径、符号链接/junction 越界在实际 provider/操作处检查。
5. 分页读取保留续读位置与截断标记；表格必须记录范围和覆盖率，不能把截断正文当全表。只有明确入库指令才进入 KB 确认流程。
6. 产物逐份记录待上传/已上传/已发送/失败/未知状态。上传或发送失败只重试投递，不重新执行已成功的 Codex、发布或定时业务；发送结果不明先对账，避免承诺外部 API 无条件 exactly-once。

`present`、turn 结束或模型说“文件已发”均不算飞书发送成功；验收以实际文件与发送回执为准。

## 6. Harness 命令、模型、目录与 Shell

### 6.1 命令分流

已知 Harness 命令通过 CommandAdapter 调用原生注册服务；业务命令调用对应领域服务并通过薄壳登记。飞书无需付费模型即可查看状态、帮助、历史、停止或回答确认。未知斜杠命令返回帮助，不回退为模型提示。

| 命令/交互 | 预期行为 |
|---|---|
| `/help`、`/status` | 列出当前会话实际可用命令/插件，展示部署、模型、实际 cwd、运行/排队状态 |
| `/model`、`/model <provider/model>` | 从 Harness 模型目录读取并校验选择；下一轮生效并持久化当前会话选择，不修改其他会话 |
| `/cd`、`/cd <绝对路径>` | 查询实际 cwd；验证部署路径和允许根后创建/切换目标工作区会话，完成后才回复生效 |
| `/new`、`/reset` | 在当前工作区创建新上下文并保留旧会话；不是删除历史或业务数据 |
| `/stop` | 调用原生取消并反馈结果；明确本轮与排队消息的处理规则，不把请求取消当作外部操作已撤销 |
| `/permission`、`/setting` | 只暴露部署允许的预设/设置；当前会话与新会话默认值分开，全局设置仅允许相应管理员修改 |
| `/compact` 等已挂载原生命令 | 经兼容清单验证后直接桥接；依赖 Web 专属 UI 的命令提供飞书替代交互或明确不可用 |
| `/kb`、`/bitable`、`/codex`、`/schedule` | 保留旧语法与自然语言入口；确认直接执行冻结计划，不让模型重新生成操作 |
| 审批/提问卡片 | 调用宿主公开回答接口；绑定原用户、chat、请求及过期时间，重复、伪造或重启后失效回调不能执行 |

模型切换期间不更改已提交请求；当前 turn 使用开始时的快照。`/model` 只改变主 Agent，Codex/adviser/Embedding 的模型由各插件配置和已批准的后端决定。模型不支持图像或工具时给出明确能力反馈，不静默切换到未选择的付费服务。

### 6.2 工作目录切换

固定版本 Session header 的 cwd 不作为可随意修改字段。`/cd` 默认切换/创建目标工作区会话；原会话保留可恢复历史，不把旧执行授权、待确认计划或目录范围带入新工作区。知识库选择由上游 dsh-knowledge 管理，本项目不再实现飞书知识空间绑定。

活动任务期间拒绝立即切换，要求先停止或等待；不得仅修改显示变量，声称当前 Agent 已切换。切换流程保存目标会话、映射与实际 cwd 后再确认，失败保留原映射。默认不复制旧对话正文进入新工作区，需要延续时通过显式摘要/恢复流程。

旧定时任务继续使用创建时冻结的 workspace/model/scope，不跟随聊天 `/cd` 或 `/model` 悄悄改权限；需要变更则重新提案与确认。

### 6.3 跨平台 Shell 与终端

直接使用上游 `shell`、`bash-sandbox`/`pwsh-sandbox`、`tool-bash`/`tool-pwsh`、sandbox 与 jobs 的现有组合。需要持续终端时选用上游 terminal 及 persistent tool；不新增本地 BashTool、ShellSandboxTool 或命令翻译器。

| 环境 | Shell 装配 | 必测事项 |
|---|---|---|
| Windows 原生 | 官方 PowerShell provider/tool；检测 `pwsh` 和沙箱依赖 | 盘符、中文/空格路径、UTF-8、`.cmd` CLI、进程终止、junction 越界 |
| Linux 本地/服务器/容器 | 官方 Bash provider/tool + 对应 sandbox | 非 root 服务账户、权限/信号、子进程清理、挂载根与实际运行架构 |
| macOS | 官方 Bash provider/tool + 对应 sandbox | Apple Silicon/x64 实际目标、路径大小写/符号链接、原生权限与取消 |

同一执行上下文只挂一个 Shell provider，按 profile 的平台条件选择；缺少 shell、原生依赖或约束能力时准确报错，不自动退到无限制 provider。WSL/容器按 Harness 实际运行 OS 解释路径，不自动接受宿主 Windows 盘符。

统一核对 cwd、超时、输出上限/溢出文件、退出码、取消与后台任务归属。工具等待超时可能意味着任务仍在后台，必须依据 jobs/执行回执报告；`/stop`、插件停用与进程退出分别测试，不假定停止模型必然杀掉后台进程。

飞书入口安装官方通用 Shell 的同时，业务操作仍需维护旧边界。旧受控文件操作不能用 Shell 绕过确认，定时受限执行不得获得通用 Shell/MCP。P0 验证原生工具作用域与权限策略能否隔离通用交互、只读、写入和定时执行；不能保证时先不在对应受限 preset 暴露该工具。

## 7. 飞书云文档、多维表格、日历与任务

聊天渠道和飞书业务在同一安装包内分模块；统一 API client 可共享凭据解析、限流、错误映射，但不拥有领域授权。缺少某项 scope 只使相应能力不可用，消息收发仍可使用。

| 能力 | 本期范围 | 关键边界 |
|---|---|---|
| 云文档/Wiki/Drive/Sheets | 保留 docx/wiki/sheets/base 云来源 inspect、导入、范围选择和手动刷新；补齐云文档创建、读取、追加更新与必要目录发现 | 追加与覆盖明确区分；分页/块/单元格覆盖可追踪；原文引用可定位；目录清单不冒充全部授权资源 |
| Bitable | 保留 inspect/query/create/update/delete 记录、链接发现、名称消歧、列表/查询分页与回读核验 | 原有五工具的业务规则优先；记录操作与表结构管理分开，结构编辑不默认纳入本期 |
| 日历 | 查询可访问日历；事件列表、详情、创建、修改、删除 | 明确日历、时区、起止、全天事件、重复事件操作范围及参与人；涉及邀请/通知在预览中显示；复杂重复编辑不静默降级 |
| 飞书任务 | 列表/详情、创建、修改、完成；删除能力纳入 API 可行性验证并落地或记录明确阻塞项 | 明确负责人/执行者、截止时间、清单与通知；不能以 Harness todo 替代飞书任务 |

2026-10-31 用户决定：所有飞书业务只使用应用身份，不保留用户 OAuth、授权回调、scopes 配置、用户 token 和身份回退规则。App ID/Secret 通过官方 credentials 获取应用 token；资源 API 权限、访问角色、管理员、白名单、确认与账本继续校验。应用不支持的个人接口明确不可用，不用用户授权兜底。

Bitable 需要完整保留：批量和删除确认、可配置 confirm_all、同用户/chat 令牌绑定、补充信息的有效期与范围冻结、相关字段乐观校验、重复消息幂等、写入结果 unknown、回读 verification，以及 known_resources_only 的覆盖说明。不能用参考项目的一次 SDK 调用替代这些规则。

文档/日历/任务采用相同的可信 Actor 和操作账本原则，根据动作复用已有用户授权或展示具体预览；预览必须给出资源、变更和通知影响。远端结果未知时先查询对账，不自动重建事件、任务或文档。完整 API scope、token 类型和资源权限清单在 P3 以 SDK/真实测试核实，不在本计划中猜测接口权限码。

## 8. 三项功能插件的迁移范围

### 8.1 Codex

保留 `/codex inspect/plan/apply`、目录消歧、计划与令牌绑定、20 分钟执行上限、短期上下文、UTF-8 stdin、结构化结果与本轮产物核验。inspect/plan 必须由后端强制只读；apply 按旧即时命令及定时授权规则处理，模型不能自行签发令牌。

保留原生历史查询、分页、详情和一次性续聊：只允许管理员本人私聊；来自 CLI/桌面/编辑器的原生历史与 Harness Session 分开；归档历史只读；恢复前重新检查目录和执行权限，不继承原历史全盘权限。续聊结果未知时先查看原生线程，不自动重试。

官方 `subagent-codex` 当前为 ephemeral 单次任务，缺少旧原生历史/续聊、细粒度进度和强制输出 schema 的等价入口，`permissionMode=never` 也不等于只读。P0/P4 对照两种后端：公开 Harness provider 与保留语义的专用原生 Codex 连接器。通过只读、取消、schema、权限和产物验收后再决定采用范围；允许新任务与历史采用不同后端，但同一请求只派给一个执行方。

原生连接器只承担协议和专用缺口，可使用适当的上游进程管理；不复制通用 Agent/子代理框架，也不移植旧 Codex 自动多轮决策主循环。任务续聊调用公开线程接口，是否进一步执行仍由官方 Agent 与用户授权决定。插件安装后检查并约束默认全局 `subagent_codex` 等替代入口，不能让受控 wrapper 被旁路调用绕过；对用户既有配置不能满足的冲突明确诊断并给出局部修复，不静默重写整个 profile。

### 8.2 Adviser

沿用 `ask_adviser` 的第二意见语义：无状态请求、相同问题/上下文的会话内有界 LRU 缓存、超时、进度和失败降级。首期可保留 OpenCLI 后端，后续通过 AdviserPort 替换；不因 Harness 有主模型就默认改变顾问来源。

外发前同时检查 question、context 和显式附件摘录中的敏感内容，再建立缓存键、请求和日志；不机械复制旧实现只处理 context 的范围。默认使用旧 180 秒超时和 16 条缓存作为可配置起点，禁止无界排队/递归调用。只提供建议，不持有写文件、发布或授权执行能力。

### 8.3 知识库（2026-10-31 用户决定替换方案）

采用 [Soren-ABT/dsh-knowledge](https://github.com/Soren-ABT/dsh-knowledge)，固定 npm 发布版 0.5.2，经官方插件管理器直接安装，使用其知识库、文档解析/分块、检索与管理 UI。不再实现 @telebot/knowledge，不复制或修改上游引擎；用户此决定取代此前自建 KB 的实施路线。

安装默认 embeddingProvider=none，关键词检索即可工作。向量/OCR/重排和云模型按上游就绪检查启用，不自动下载权重或沿用聊天模型凭据。版本、许可证、依赖构建许可和官方宿主实际安装证据单独记录；不把仓库源码 devDependencies 中的 link 路径当成部署前提。

2026-10-31 用户明确取消旧知识库数据迁移和飞书 chat→知识空间绑定；从后续待办及验收门禁移除，不删除原有数据。统一管理员/显式附件入库、飞书云来源和每日预算等旧规则不因换包而自动继承；其他适配只在有明确需求时安排，不重写上游引擎。不要扫描用户目录、扩大模型可用知识范围或自行回退到全部库。

### 8.4 其他旧能力的保留

搜索/网页提取、研究报告、Skill 按需加载、原文博客发布、指定文件/批量 Git 推送和文件覆盖/隔离删除加入 feature-parity。通用能力优先使用宿主，百炼专有联网能力放在 provider/适配器；原文保真、哈希去重、整批完成后才推送、只提交指定文件及真实用户授权等业务规则继续保留。它们不是第二套 Agent 分支。

## 9. 定时任务：复用官方计时，保留旧业务边界

### 9.1 旧功能必须逐项覆盖

| 旧边界 | 新系统责任与验收 |
|---|---|
| 自然语言及 `/schedule` CRUD、list/get/history，批量创建/修改 | 本地解析/冻结提案调用 ScheduleAdapter；所有 ID 可追踪；批量部分失败可恢复，不宣称跨库原子提交 |
| once/daily/weekly、interval、count、until、带时区首次时间 | 适配官方规则；ISO 星期转换单测覆盖：旧 0=周一，新 1=周一；unsupported 不能悄悄忽略 |
| 创建/修改/删除确认与 auto 预授权，15 分钟期限 | 保留本地令牌、真实 Actor、范围和版本；创建确认不等于执行授权 |
| remind/auto/confirm 三种模式 | 提醒只投递；auto 仅执行已预授权范围；confirm 每次生成 execution ID，再授权或拒绝 |
| 默认迟到 5 分钟跳过，skip/run_once/ask | 关机遗漏逐次标记，最多补执行/申请授权一次；由明确的策略计算和恢复流程验证 |
| 工具/目录白名单与原操作限制 | 保留 web_search/web_extractor、文件列表/读取、仅新建 write_report、单工作区 codex_apply；不自动增加 Shell、任意 MCP 或 Bitable 写入 |
| 旧串行执行和多进程单调度者 | 首期业务执行并发维持 1；一个 home 只运行一个官方计时 Host，重复启动明确失败；不复制旧扫描线程 |
| 1200 秒执行上限、入队/开始/完成通知 | 有界执行、取消、状态和产物回执；进程退出不遗留无主业务任务 |
| 本轮 Codex Excel/CSV 等真实产物及 Skill 运行副本状态 | 核对文件范围、类型、mtime/哈希和任务要求；保留技能配置/去重状态，不改原 `.codex` |
| 执行与投递分离，逐附件重试、重启续投 | 一份 run 关联多个投递项；发送失败不重新运行已完成业务；历史和审计在删除后仍可查询 |

### 9.2 官方能力与缺口处理

官方 schedule 负责唯一时钟、规则、到期、原 Session inbox 投递和持久化。本地任务策略只持有业务 task ID、可信授权、workspace/model 快照、occurrence、运行/产物/出站状态和映射；不建立可独立编辑的第二份时间目录。

当前已知差异：`schedule/changed` 是无参数的变更通知；公开 API 没有专用 task/occurrence due 回调；投递回执仅证明 inbox 持久化；Session flush 与任务记录写入有崩溃窗口；原生 history 删除后随任务消失；旧 interval/count/until、完整遗漏历史、自动执行与补跑不能只凭原生提醒接口假定等价。

P0 必须完成公开 API 原型，验证以下方案：用结构化 catalog/history 与持久 ID 映射确认 occurrence；变更通知只触发重新读取；启动对账处理未完成映射。不能解析中英文提醒正文获得执行授权，也不能靠模型输出断定发生时间和任务归属。

对 interval/count/until 等原生不能完整表达的规则，优先验证由官方计时的一次性任务逐次推进：本地只计算下一次发生时间，仍交由官方 schedule 唤醒，不增加独立扫描计时器。该方案还需验证创建/结束/删除、崩溃窗口、未来触发不丢失，以及 Web 编辑与本地规则的一致性；未通过前只是候选。

受控自动执行只能有一个业务派发方。原生 inbox 唤醒与本地执行器不可同时发起同一写操作；在工具作用域和执行端核验 task/occurrence 授权。本地执行记录至少按 task ID + planned occurrence + 授权版本防重，外部操作还须有操作级幂等/回读；不能宣称跨服务绝对 exactly-once。

任务变更走一个应用服务；官方 Web 如不能表达 remind/auto/confirm、旧补跑等策略，则本地业务任务先只展示/导航，不能让两个编辑入口独立修改。取消/删除前保留本地必要审计与回执，确认历史保留策略不会留下无法关联的执行。

**阶段门禁：**若结构化关联、防重、补跑或规则不能通过公开接口可靠实现，记录最小公共扩展需求，单独安排官方扩展/升级评审。本分支不恢复旧调度代码填补缺口；门禁未过仅交付已验证的普通提醒或隔离测试能力，不能宣称 R4 完成。用户在仓库外继续运行旧系统时，同一生产任务不能同时导入新系统；不得用私有存储访问或复制计时器掩盖差距。

## 11. 验证设计和完成标准

### 11.1 分层检查

- L1 领域：不启动 Harness，用本地端口替身验证授权、冻结计划、预算、幂等、范围与结果核验；不为复刻实现而写断言。
- L2 适配：固定输入和时钟，测试公开 API 映射、错误分类、模型/目录选择、原生 ID 与本地引用、星期与时区转换。
- L3 发行组合：仓库外官方 `dsh` + 本地 tgz + loopback mock 模型/外部服务 + 临时 home。通过官方 ACP v1 测工具、模型、审批、取消、恢复和生命周期；原生命令不在 ACP 面内，另经公开服务适配验证。多插件 peer/服务身份、启停和依赖缺失均需测试；源码 checkout 不能替代发行验收。
- L4 授权联调：在专用飞书应用/测试资源、目标 OS 和部署环境验证 API 与文件结果；写测试只操作明确测试数据并清理。离线与 loopback 测试不调用付费模型，不自动读取生产凭据。

实施后使用 `pnpm build`、`pnpm pack:plugins`、`pnpm verify:harness` 作为基础门禁。产物检查必须读取解包后的实际 package，而非只检查源码 manifest；禁止源码路径和 workspace/link 泄漏，验证 declarations、patch 和必要资源齐全。验收进程只获得安装产物与测试夹具，不配置开发 NODE_PATH、不读取仓库 node_modules 或子模块构建目录。保持临时 home、mock 模型和 keyless；真实飞书/真实 Codex/目标平台检查单独记录。

### 11.2 必须通过的场景

| 场景 | 判定依据 |
|---|---|
| 官方 Harness 本地安装与源码隔离 | 普通官方发行安装 + tgz 在仓库外运行；安装完成后移走 dist/源码仍能加载；无需 clone/submodule/本项目启动脚本 |
| 多插件组合与依赖 | core 服务身份唯一；可选插件独立启停；缺失 peer/不兼容版本明确拒绝；不替换用户无关 profile 配置 |
| 同 webhook 重发、卡片连点、进程在接收/执行/回执之间崩溃 | 同一操作不重复写；无法判断时进入 unknown 并可对账，重启不丢已持久接收项 |
| 主模型不可用 | `/help`、状态、停止、确认、原生历史等直接控制仍可工作；失败准确归因 |
| 模型切换与目录切换 | 读取实际请求/Session cwd/文件位置核验；不同 chat 不串配置，切换失败保留旧映射 |
| 附件先发/后发、重启、过期、跨 chat 引用 | 正确绑定与拒绝；不自动入库，不误读其他会话附件 |
| 直接文件/Shell/Codex 替代入口 | 无法绕过管理员、目录、只读和业务操作授权；敏感 preset 不获得多余工具 |
| 原生 Shell 多平台 | 原生机器验证中文/空格路径、编码、退出码、超时/后台区别、终止和权限拒绝 |
| 飞书四类业务 | 测试资源真实变化与回读；分页覆盖、用户 token 失效、通知影响和未知结果处理正确 |
| KB 和表格 | 哈希去重、预算暂停、来源更新、限定空间检索、表格范围/全量标记与证据正确 |
| 三功能插件启停 | 工具注册随启停变化；进程/待确认清理；数据保留；其他插件继续使用 |
| Codex 历史/续聊与产物 | 原生 thread 状态、只读拒绝、权限重置、本轮路径/哈希及实际附件；不以助手回答判完成 |
| 定时错过、多 occurrence、补跑/逐次确认、到点删除 | 一个官方时钟，一个业务派发方；次数/截止/时区正确，旧规则和历史逐项一致 |
| 执行成功但上传/发送失败 | 只恢复出站；每文件有回执；不会再次执行有副作用的业务 |
| 插件/运行时升级与数据恢复 | 精确版本门禁；备份副本验证；旧代码恢复旧数据快照，而非强读新版格式 |

正式完成标准：官方受支持版本可本地安装全部交付包；源码隔离后运行；R1–R5 有实际证据；旧能力清单没有未经同意的取消项；高风险公共接口缺口已解决；Windows/Linux/macOS 声明支持的目标均经原生验证；卸载/升级/恢复经过演练。不能仅在本仓库运行成功或某平台编译通过就宣布完成；尚无测试环境或需私有源码改动的项保持待验收。

## 12. 数据迁移、部署与回退

范围修订：用户已取消旧知识库迁移和飞书空间绑定，下面通用备份/迁移流程不再要求迁入旧 KB。原库保留；新 dsh-knowledge 的备份按其元数据/SQLite/raw 格式执行。其他需保留的渠道、配置、任务与审计数据仍独立验收。

数据所有权：用户的官方 Harness 保存 Session/模型/官方 schedule/插件设置；Telebot 插件保存知识、Bitable 操作、授权、任务业务回执、附件绑定与投递；Codex 保存原生历史。只通过公开接口互相引用，不直接读写 Harness 私有存储。插件经公开 home/storage 服务或显式配置选择数据根，各插件使用独有命名空间/子目录；禁止默认写 repo、插件安装目录或用户当前 cwd。

迁移步骤：

1. 停写或使用 SQLite 在线备份，妥善处理 WAL；保存 Python 业务库、文件、向量元数据和现有配置的安全副本。另行备份 Harness home，不把凭据放进普通报告。
2. 在隔离目录迁移副本，核对文档/块/向量/任务/记录数、ID、关联、权限、待确认与 unknown 状态；有效期/身份无法可靠迁移的确认令牌失效并重新提案。
3. LangGraph 检查点保留只读查询；需要延续的对话导入经过校验的摘要和引用，不把旧库伪装成 Harness Session。完整历史转换另行设计，不阻塞摘要迁移方案。
4. 使用方独立安装清单中支持的官方 Harness，再安装插件 tgz、配置凭据和数据根；开发期隔离 home 不作为必须迁入的运行时。显式配置 Windows 到服务器的根目录映射；原生依赖在目标 OS 安装，不跨 OS 复制 node_modules。Windows 服务与 Linux systemd/容器都直接启动官方 dsh profile，不启动本项目应用入口。
5. 先用测试应用/chat 或重放事件验证，避免同一生产 webhook 同时发给两套执行器。正式切换时停止旧入口/旧调度，迁移已确认任务并核对数量，再启用新入口；未迁移任务只能由旧服务单独承担，按任务清单排除重叠。
6. 新系统观察至少覆盖一轮日常业务、周期触发和恢复场景；验收失败按切流清单回退。代码、数据与外部变更分别处理，已发送消息/外部写入不能靠 Git 回滚撤销。

备份与恢复演练作为 P7 必交付内容；禁止直接在生产副本上试升级后再用旧运行时启动。宿主升级始终执行“兼容升级分支 → 固定官方发行版本 → 备份副本 → 重建插件 → 干净安装/业务验证 → 更新精确版本与矩阵”，不在部署时自动追踪最新版，也不要求使用者切换本项目子模块。

## 13. 风险、决策与证据索引

| 风险 | 提前处理与交付要求 |
|---|---|
| 开发 workspace 可运行但发布包无法安装 | P0 先用官方发行宿主装 tgz；检查 internal 代码、peer、补丁/资源和声明闭包；P7 移走源码再验收 |
| 参考插件依赖旧 API 或配置宣称超过实现 | P0 对照候选官方发行 API 和固定源码快照；模块级借鉴，记录来源和许可证，不整包直接装生产 |
| schedule 无法满足完整旧语义 | P0 原型先行；公共扩展单独评审；门禁未过不迁 auto/confirm、不宣布定时等价 |
| 通用 Shell/全局 Codex/原生 schedule 工具绕过业务规则 | 测真实工具目录和直接调用；限制 preset/执行能力，授权在实际操作处校验 |
| 日历/任务应用资源权不足 | P3 仅应用身份，核对 API scope 与目标资源角色；缺失明确报错，不接入用户 OAuth |
| 工作目录显示与执行错位 | `/cd` 完成真实会话切换再反馈；状态读取实际 cwd；不继承旧目录授权 |
| 重试导致重复外部写入、重复定时运行 | 接收去重 + 业务账本 + occurrence/操作幂等 + unknown 对账，执行和出站分开恢复 |
| 跨 OS 沙箱/原生依赖差异 | P2 先做本机，P7 原生矩阵；缺依赖明确诊断，不偷偷降级权限 |
| 迁移范围扩散/包过多 | 以完整垂直用例推进，按需要建包；首个内测版与正式全量切换分开验收 |

源码证据及后续开发入口：

- 本仓库约束：[AGENTS.md](../../AGENTS.md)、[当前安装与验证说明](../installation.md)。已删除的旧脚手架报告不作为本分支证据。
- 官方源码依据：上文固定 commit 中的 `packages/boot/plugin-manager`（本地路径/tarball 安装、bundle 启用与版本检查）、`packages/interaction/commands`、`packages/core/agent`、`packages/shell`、`packages/bundle/base/cordis.patch.yml`、`packages/schedule/schedule`、`packages/subagent/subagent-codex`。这些是上游相对路径，不要求仓库包含其源码；发行兼容结论写入目标 `tooling/compatibility.json`，不把过渡期内嵌路径作为长期文档链接。
- Python 行为：`D:/files_data/windborne/tele_bot/README.md`，以及其 `tele_bot/channels`、`tools/adviser.py`、`router/codex*_service.py`、`knowledge`、`bitable`、`scheduled` 与 `tests/{feishu,codex,knowledge,bitable,schedule,tools}`。
- 飞书参考：`D:/files_data/windborne/coding/dsh-lark-main/src/{harness,channel,settings-api,config}.ts`；`D:/files_data/windborne/coding/harness-lark-master/src/{channel/monitor,agent/commands,agent/bridge,tools/doc,tools/base-sheets-calendar-task,approval/feishu-approval,interaction/ask-user}.ts`。

参考条目属于静态源码依据。当前验证范围以状态页和兼容记录为准；未执行的设计不计为完成。
