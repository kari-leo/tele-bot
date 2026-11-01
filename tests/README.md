# 验证入口

唯一命令为 `pnpm verify`，模块/测试/门禁目录仅在 [verification-catalog.mjs](../tooling/verification-catalog.mjs)。只验证改动模块，共享契约追加实际消费者；仅文档改动检查链接和事实，不重跑业务套件。

## 模块与安装层

```powershell
. ./examples/use-d-drive-runtime.ps1
pnpm verify --list
pnpm verify --module feishu --local
pnpm verify --module core --module automation --local
```

--local 用于领域/客户端变化，--installed 用于宿主接线，省略两者执行两层；多模块自动过滤和去重。模块包括 core、settings、feishu、feishu-business、codex、adviser、publishing、automation、desktop、knowledge，具体测试与 gate 由 --list 查询。

```powershell
$env:TELEBOT_DSH_INSTALL = 'D:/absolute/official-rc5-prefix'
pnpm verify --module feishu-business --installed
pnpm verify --module automation
# 仅显式发行或兼容升级：
pnpm verify --release
```

安装层使用仓库外官方发行，不用源码 workspace。缺运行时时在 D 盘创建固定版本 npm prefix，版本见 [compatibility.json](../tooling/compatibility.json)。优先复用现有安装/缓存，结束后核实无引用和进程再清理临时安装，不删正式 profile、release 或数据。

检查构建/打包一次、带 --fixtures 验证归档闭包，再由官方 manager 安装；使用隔离 home、ACP v1、loopback 模型/平台，不继承生产密钥。ACP 不替代命令注册表、所有者 RPC 或业务授权。settings 覆盖普通/桌面端口生命周期，feishu 选择 ACP/Web 渠道。

完整业务可选 --gate composition / composition-web；真实 Codex 后端改动追加 --gate codex-native，并由 TELEBOT_CODEX_CLI 指定固定 CLI。发行默认不执行真实平台/账户检查，页面视觉验收由用户完成。

## 构建与归档

需要 Node.js >=24.9、pnpm 11.28.5 和 tar。先执行 D 盘脚本及 `pnpm install --frozen-lockfile`。build、pack:plugins、check:artifacts 可独立运行；组合验证前以 --fixtures 打包，生产只装七个非 fixture 包。

## 真实服务检查

```powershell
pnpm verify --live preflight --reference 'D:/external/local.env' --workspace 'D:/test-workspace'
pnpm verify --live bitable --reference 'D:/external/local.env' --base 'https://tenant.feishu.cn/base/EXPLICIT_BASE'
pnpm verify --live chat --reference 'D:/external/local.env' --chat 'oc_EXPLICIT_CHAT'
```

preflight 不联网，未就绪退出 2；Base 默认只读，chat 默认只查资料。明确授权后才加 --write / --send：写入只处理本轮标记记录，不改旧记录/表结构；已发送项仅回读，sending/unknown 先对账。--callback 只检查 GET 可达，不修改后台或证明入站。真实检查不混入默认模块/发行流程，不记录秘密或敏感正文。范围见[真实验收](../docs/feishu.md#真实验收)。

本机报告在忽略的 .runtime；成功隔离 home 自动清理，失败保留诊断。唯一可跟踪摘要是 [latest.json](../tracking_outputs/latest.json)，区分当前归档和沿用证据，不增并列进度报告。
