# 统一飞书插件的配置与 UI

更新：2026-10-30。`@telebot/feishu` 已同时包含渠道与飞书业务，原 `@telebot/feishu-tools` 不再发布或单独安装。内部仍按 channel/business/setup 分层；停用 `businessEnabled` 保留渠道，停用整个飞书包则同时卸载二者。Codex/adviser 保持独立。

插件没有独立的聊天模型或 Agent 循环。页面的 provider/model 是为飞书关联的官方 Harness Session 设置可选模型覆盖；同时留空即使用宿主默认选择，`/model` 也只修改当前 Harness 会话。它不配置知识库 embedding、Codex 或 adviser 模型。

## 首次使用

1. 在受支持的官方 Harness `0.2.1-rc.5` 的 Web profile 中先安装 core，再安装 feishu。该 profile 必须提供官方 webServer、tools、Agent/Session 和 credentials；桌面需启用 dedicatedIngress 隔离官方 webServer，使配置端口独立于桌面临时根端口；详见安装指南。
2. 飞书 row 默认启用。首次加载会创建配置目录；缺 App ID、管理员、工作目录、Secret 或来源校验时，宿主继续运行，webhook 不准入，并在启动提示中提供本机配置 UI 链接。
3. 用本机浏览器打开提示中的 `/telebot/feishu/setup#…`，手动填写配置。链接片段是本机配置访问令牌；没有片段时可从配置目录的 `setup.token` 输入。不要分享访问令牌或把配置目录暴露到公网。
4. 点击“保存并启用”。普通字段和资源元数据写入配置文件，Secret/Verification Token/Encrypt Key 写入官方 credentials，仅引用进入 JSON。空密码框保留已配置凭据，页面只显示是否配置，不回显值。
5. 任务空闲时立即生效，无需重启整个 Harness。有排队输入、执行或待投递内容时拒绝修改；初始化和保存串行，新消息准入在切换时暂停，新配置提交后才启动处理。保存失败恢复之前的运行态，旧 revision 的保存拒绝覆盖。

验证范围见 [兼容记录](compatibility.md)；本页只说明正式配置。

## 目录与文件

默认根目录是 `$DSH_HOME/plugins/telebot-feishu/config/`，可以在启动配置的 `configDirectory` 中指定另一个绝对目录；该项只用于定位配置目录，不能通过 UI 将它改到任意路径。

| 文件 | 内容 |
|---|---|
| `config.json` | version=1、revision 和非秘密配置；资源保存为 `config.business.resources` 数组 |
| `config.example.json` | 首次使用的空值模板 |
| `endpoint.json` | 当前本机 origin/route，无令牌 |
| `setup.token` | 本机配置访问令牌，每次加载旋转 |
| `writer.lock` | 当前配置写者的 PID/随机所有者；拒绝同目录多实例写入 |
| `README.txt` | 本地维护说明 |

参考结构见 [config.example.json](../examples/feishu-config/config.example.json)。生效配置文件优先于启动 patch；首次尚无文件时接收已有 profile 配置。手工编辑时先停止对应 profile，保留版本结构，再启动验证。不要在 JSON 中填写实际 token、Secret 或扩展的明文凭据字段；资源元数据字段及 credential ref 有校验。

渠道数据默认是 `$DSH_HOME/plugins/telebot-feishu/<appId>/`，包含 channel、attachments、reminders 数据库，业务账本在其 `business/operations.sqlite`。配置、官方 credentials 和业务数据一起备份；运行中的 SQLite 应使用一致性备份。卸载保留这些目录。Windows 访问隔离以目录 ACL 为准。

## 应用身份与权限

2026-10-31 起所有业务统一使用应用身份；删除 env 导入 UI/API、用户 OAuth、scopes 输入和用户 token 流程。后台权限用户已确认开通，页面不再编辑或选择授权 scopes。App ID、Secret、来源凭据及管理员/目录由本人手动填写。

应用 API 权限、资源访问角色和本插件资源白名单共同生效。日历无需 OAuth 回调，但应用必须对目标日历有相应读写权限。仅支持用户身份的接口不接入。详细步骤见 [应用权限与资源配置](feishu-permissions.md)。

旧业务配置含 oauthRedirectUri/oauthScopes/allowTenant 或用户资源时，停机备份后手工删除这些字段并明确应用资源；不自动放宽旧确认。原凭据/业务数据保留。

## 添加多维表格

先保存应用凭据，再在独立链接区粘贴 Base 链接，点击“添加链接并读取数据表”，最后保存。每个链接可单独移除，也可用管理员会话自然语言增删；移除只解除接入，撤销旧写入预览，不删除远端记录。不会扫描全租户或修改表结构。

生成的资源使用 `identity=tenant`、`tokenRef=FEISHU_APP_TENANT`，在操作前按当前应用获取/更新 tenant token，不将 token 写入资源 JSON；不支持用户身份资源或任意凭据引用。写入默认仍需冻结预览和本人确认。

资源真实验收状态见 [联调清单](feishu-live-checklist.md)。

## 高级接入限制与公网回调

正式 UI 只保留应用/会话、凭据/来源校验、消息回调与资源白名单，不展示测试会话或验收开关。高级文件选项 `allowedChatIds` 留空时不额外限制管理员会话；已有白名单仍在接收前执行，页面保存保留该值。需修改时停止 profile 并编辑 config.json，不会因移除 UI 输入而放宽已配置限制。

UI 只接受 loopback、匹配的 Host/Origin 和访问令牌，拒绝反向代理转发的配置请求；无 CORS。页面使用 CSP、禁止嵌套/缓存，写操作要求 JSON 和同源。webhook 不能占用 setup 路径，不注册用户授权路由。

公网地址只作为配置元数据，保存 UI 不会替用户修改飞书后台、DNS、隧道或反向代理。旧项目记录的公网地址是 `https://feishu.windborne.cc/feishu/webhook`；上次 GET 可达性检查返回 502，尚未证明已接到新插件。远程维护可使用同端口的 SSH 本地转发访问 loopback 页面，不把配置页面开放给公网。

## 从旧两包安装升级

若曾独立安装 `@telebot/feishu-tools`，先停止 profile，备份配置/凭据/数据库，再通过官方 manager 移除该包并安装新版统一 feishu，最后配置或核对文件。不能同时启用旧两包与新统一包，否则会重复注册业务功能。

旧业务账本不会自动搬移或删除。若确有需保留的旧数据，可显式将 `business.dataDirectory` 指向旧账本目录，在备份副本上验证；旧待确认计划如不能满足新冻结摘要，应重新预览确认，不能盲重放。此次重构没有迁移生产数据或切换旧服务。
