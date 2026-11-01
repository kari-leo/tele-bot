# 模块验证入口

统一命令 `pnpm verify`，唯一清单为 [verification-catalog.mjs](../tooling/verification-catalog.mjs)。普通修改只选受影响模块，共享契约变更追加实际消费者；构建/打包依赖闭包不等于验证所有业务。

```powershell
. ./examples/use-d-drive-runtime.ps1
pnpm verify --list
pnpm verify --module feishu --local
pnpm verify --module feishu-business --module settings --local
# 宿主接线变更，指定仓库外官方运行时后执行安装层：
pnpm verify --module feishu --installed
# 两层都执行：
pnpm verify --module settings
```

| 模块 | 本地范围 | 默认安装层 |
|---|---|---|
| core | 宿主适配、流进度、归档/隔离规则 | 官方基础 |
| settings | 文件、凭据、原生页面、Base 链接 | 普通/桌面端口两种配置生命周期 |
| feishu | webhook、会话、进度、队列、命令、收发、附件 | ACP/Web 渠道；progress/questions 为同载体的短路径 |
| feishu-business | 四类应用资源、表名、CRUD、冻结确认 | ACP/Web 资源发现短路径，不重复渠道故障测试 |
| codex / adviser | 各自权限、进程、历史/Skill 或缓存/超时 | 真 Codex 后端改动追加 codex-native |
| publishing / automation | 各自发布/调度规则、账本与恢复 | automation 选 questions 短路径；完整业务接线另选 composition[-web] |
| desktop | 原生身份、完整权限和隔离 | 不依赖飞书的原生 RPC |
| knowledge | 上游插件 | 上游安装/关键词/重启 |

内部检查归入 tests/integration/，按载体复用，不再各自提供顶层命令。基础/P0 共用 harness；渠道/完整业务共用 feishu；普通/桌面配置共用 settings；真实 Base/chat/预检共用 feishu-live。混合历史测试文件按测试名称过滤，多模块选中同一文件时合并过滤条件，只执行一次。catalog 回归保证没有遗漏断言。

```powershell
pnpm verify --gate p0
pnpm verify --gate codex-native
pnpm verify --gate composition-web
# 仅正式发行/宿主升级，不自动包含真实平台或真实 Codex：
pnpm verify --release
```

`pnpm test` 保留全部本地检查的手动入口；`pnpm test --module <模块>` 和旧 --suite 参数仍可选测。真实平台绝不进入默认检查，命令与报告见 [testing.md](../docs/testing.md)。页面视觉验收由用户完成。
