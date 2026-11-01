# 知识库：使用上游 dsh-knowledge

2026-10-31 用户决定：用 [Soren-ABT/dsh-knowledge](https://github.com/Soren-ABT/dsh-knowledge) 替换自建方案，直接安装其 npm 发布版 **0.5.2**。本仓库有七个自有生产包；知识库作为独立外部 bundle，不复制源码、不重新包装。版本/完整性见 [external-plugins.json](../tooling/external-plugins.json)。上游许可证为 AGPL-3.0。

## 安装与使用

使用当前受支持外部官方 Harness 0.2.1-rc.5。安装器先备份现有 profile 文件，按上游要求合并明确的构建许可，再通过官方 manager 安装精确版本，不覆盖其他配置。命令不启动真实模型或扫描旧资料。

```powershell
. ./examples/use-d-drive-runtime.ps1
$env:TELEBOT_DSH_INSTALL = 'D:/path/to/external-official-install'
pnpm install:knowledge --home 'C:/Users/41547/.dsh' --profile web --runtime-root 'D:/files_data/windborne/dsh-runtime'
```

此前已安装在 web profile；2026-11-01 又按最新授权通过桌面自带 CLI 精确安装到 desktop profile。两者是独立实例，详见 installation.md。启动对应 profile 后使用“知识库”面板。默认 embeddingProvider=none，可以先新建库、导入文本/文件并使用关键词检索；向量、重排、OCR 权重需你在上游面板明确选择/下载，安装不代替模型就绪检查。

飞书插件本身不调用独立聊天模型。它创建的 Harness Session 可使用宿主默认模型或可选会话覆盖；知识库 embedding/rerank 是另一类配置，与聊天 provider/model 无关。

## D 盘放置与兼容路径

- 插件依赖实际位置：`D:/files_data/windborne/dsh-runtime/profiles/web/node_modules`；新增 desktop 位于 `D:/files_data/windborne/dsh-runtime/profiles/desktop/node_modules`，整个 desktop profile 联接回原 C 盘入口。
- pnpm 内容缓存：`D:/files_data/windborne/dsh-runtime/cache/pnpm/store/v11`。
- 本轮专用验收/临时目录：`D:/files_data/windborne/dsh-runtime/tmp/`；后续检查设置 TEMP/TMP 或 TELEBOT_VERIFY_TEMP 指向该目录。
- 原 web 的 node_modules/store 联接保留；desktop 必须联接整个 profile，单独联接 node_modules 会破坏 rc.5 的宿主 peer 解析。联接不是第二份依赖。迁移逐文件 SHA-256 校验，功能复验通过后清理原 C 盘备份。
- 现有 DSH_HOME 的小型配置、会话、凭据和业务数据不随依赖迁移而移动。未来模型缓存可在上游设置中指定 D 盘目录；移动依赖不等于已经迁移所有业务存储。

后续安装/验证先 dot-source 上述脚本：它只设置当前 PowerShell 的缓存与临时目录，不修改系统级 TEMP/TMP。D 盘临时目录须有当前用户的 FullControl，以便官方 Windows 沙箱设置每轮工作区 ACL；首次迁移回归曾因此失败，使用方目录权限需单独核对，不以关闭沙箱绕过。

## 验证与边界

```powershell
$env:TELEBOT_RUNTIME_ROOT = 'D:/files_data/windborne/dsh-runtime'
$env:TELEBOT_VERIFY_TEMP = 'D:/files_data/windborne/dsh-runtime/tmp'
pnpm verify:knowledge
```

验证使用独立 home 与合成文本，通过实际官方 Web/ACP 组合检查安装、HTTP 导入/分块、关键词检索、重启持久化、卸载；不修改用户库。结果见 .runtime/verify-knowledge-latest.json 和 [当前状态](status.md)。

上游自带配置与管理 UI，不再计划重写知识库 UI。用户已明确取消旧知识库数据迁移和飞书 chat→知识空间绑定，两项不再待开发/待验收，原数据保留。其他旧飞书入库/权限/预算规则不因安装自动继承，按后续明确需求决定，不作为重复实现引擎的理由。P5 其他能力已由 publishing 与 Codex Skill 运行副本提供，验收边界见状态页。不要删除新库的 knowledge.json 元数据；完整备份需要元数据、chunks SQLite、raw 原文及必要模型配置。
