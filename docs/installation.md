# 安装与配置

使用独立安装的官方 Harness，精确兼容版本见 [compatibility.json](../tooling/compatibility.json)。本机为 0.2.1-rc.5，不混用其他版本。安装和更新使用官方插件管理器。

## 本机启动与目录

插件已安装；先退出已有桌面，再从当前交付目录启动：

```powershell
& 'D:\files_data\windborne\dsh-runtime\releases\telebot-0.1.0-semantic-confirmation-20261101\start-desktop.ps1'
```

| 内容 | 位置 |
|---|---|
| 官方桌面 | `D:/daily_app/Deepseek Harness` |
| desktop profile 实体 | `D:/files_data/windborne/dsh-runtime/profiles/desktop` |
| 兼容入口 | `C:/Users/41547/.dsh/profiles/desktop` → 完整 profile 的 Junction |
| 配置、凭据、会话与业务数据 | 原 DSH_HOME，默认 `C:/Users/41547/.dsh` |
| pnpm 内容缓存 | `D:/files_data/windborne/dsh-runtime/cache/pnpm/store/v11` |
| 验证临时目录 | `D:/files_data/windborne/dsh-runtime/tmp` |
| 本次更新前备份 | `D:/files_data/windborne/dsh-runtime/backups/semantic-confirmation-before-2026-11-01T15-31-15-256Z` |

保留整个 profile 的 C→D 联接；只联接 node_modules 会破坏宿主 peer 解析。新依赖、缓存和临时目录用 D 盘，原用户数据保留。启动脚本准备 D 盘环境、PowerShell 7.6.9 和现有 Git，不依赖源码目录。

## 从归档安装或更新

先退出对应 profile，备份配置、凭据和业务数据。桌面首次使用时先启动一次完成 desktop profile 初始化。

```powershell
. ./examples/use-d-drive-runtime.ps1
$dsh = 'D:/daily_app/Deepseek Harness/resources/runtime/cli/bin/dsh.cmd'
& $dsh --version
$release = (Resolve-Path './dist').Path
foreach ($name in @('core','desktop','feishu','codex','publishing','automation')) {
  & $dsh plugin --profile desktop add ('file:' + ($release + '/telebot-' + $name + '-0.1.0.tgz').Replace('\','/'))
  if ($LASTEXITCODE -ne 0) { throw ('安装失败：' + $name) }
}
```

只安装需要的包，core 先装；adviser 可选，本机停用。归档版本、顺序和 SHA-256 以 dist/install-manifest.json 为准，生产环境不装 diagnostics。首次解析可能需要包源，已有依赖可复用缓存。

| 使用方式 | 最小组合 |
|---|---|
| 原生 Codex / 顾问 / 发布 / 自动化 | core + desktop + 对应业务包 |
| 飞书对话与飞书资源 | core + feishu |
| 飞书调用其他业务 | core + feishu + 对应业务包 |
| 双入口 | core + desktop + feishu + 所需业务包 |
| 知识库 | 独立 dsh-knowledge |

官方 UI 也可添加同一 `file:D:/...tgz`；完整解压 package 目录可用 file: 安装，须长期保留。不要指向仓库根或源码 workspace。自有包尚未提供 npm/市场/Git 发布入口。

通过官方插件页启用对应行；patch 示例见 [desktop](../examples/desktop.patch.example.yml)、[feishu](../examples/feishu.patch.example.yml)。合并已有条目，不能整体覆盖用户 profile 或丢失 row.config 必要字段。停机更新后重启，不提供不停机远程更新。

## 配置入口

在 Harness“设置 → Telebot 插件”选择插件、保存并应用；未启用插件没有配置页。默认目录为 `$DSH_HOME/plugins/telebot-名称/config/`，非秘密配置和凭据引用存 config.json，秘密由官方 credentials 管理。首次保存后文件优先于启动 patch；无客户端时可停机编辑同一文件。

| 插件 | 主要配置 |
|---|---|
| feishu | 应用、管理员、目录、来源校验和资源，见[飞书配置](feishu.md) |
| desktop | 默认完整权限，可切为受限模式并配置允许目录 |
| codex | 既有 CLI 绝对路径；飞书另配管理员、允许目录及本人历史 ID |
| publishing | 文章目录、Git 仓库/分支；飞书另配管理员和允许目录 |
| automation | 时区、模型、执行限额与管理员，执行授权见[使用与维护](operations.md) |
| adviser | 可选既有 OpenCLI 路径、管理员与脱敏项 |
| core | 能力诊断，无需另配聊天模型 |

Codex 复用已登录的本机 0.160.0，不要求新账户。飞书 provider/model 同时留空时沿用宿主默认模型；知识库 embedding 是独立配置。

## 外部知识库与卸载

本机已有 dsh-knowledge 0.5.2，使用上游面板。其他安装可运行 `& $dsh plugin --profile desktop add 'dsh-knowledge@0.5.2' --save-exact`；必要时用 `pnpm install:knowledge --home <DSH_HOME> --profile desktop --runtime-root <D盘目录>` 合并 D 盘缓存和构建许可。

默认 embeddingProvider=none 可关键词检索；向量、重排或 OCR 在上游按需配置，不自动下载权重。web 与 desktop 是独立 profile。旧知识库迁移和飞书知识空间绑定已取消，原数据保留。

卸载按依赖逆序执行官方 `plugin --profile desktop remove '@telebot/名称'`，并移除对应启用项；数据按[维护说明](operations.md#备份与恢复)处理。
