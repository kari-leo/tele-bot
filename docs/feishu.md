# 飞书配置

在 Harness“设置 → Telebot 插件 → 飞书配置”手动填写；这是官方原生设置页，没有独立 setup 网页、OAuth 回调或设置令牌。安装与启用见[安装](installation.md)。

## 应用与资源

1. 填 App ID、Secret、管理员 ID 和已存在的绝对工作目录。管理员为空时不接收业务请求。
2. 填 Verification Token；签名/加密事件另填 Encrypt Key。至少具备一种来源校验，两者配置时同时校验；密码框留空保留已有凭据。
3. provider/model 同时留空使用宿主默认模型；allowedChatIds 可限制接入群。
4. 在多维表格链接区增删 Base，或说“添加/移除多维表格链接 URL”。移除不删远端数据，使旧待确认写入失效。
5. 文档、日历、任务另配资源白名单和允许能力，保存并应用。普通配置与秘密分开保存，应用 token 自动获取和更新。

只使用应用身份 tenant_access_token。API 权限、资源访问角色、插件管理员/白名单各自检查；平台 scope 不等于访问所有资源，也不会自动登记 Base。应用无法访问的个人接口明确不可用，不回退用户身份。

文档支持 docx/wiki/Drive/Sheets，Sheets 只读配置的精确 range；表格支持记录 CRUD，不编辑表结构；日历和任务须具备目标资源的对应权限。实际 scope 以后台与官方接口为准，不从逻辑能力字符串推断。

写入冻结预览、本人确认后一次执行并核验，最多 500 条。补充字段不延长期限，目标或相关字段冲突须重新预览。日历明确时区、起止、通知及 single/all 范围，任务明确负责人、截止与清单。未知结果先对账，不重复创建/写入。

旧参考配置可通过 [load-feishu-reference.ps1](../examples/load-feishu-reference.ps1) 导入当前启动进程；不复制凭据到仓库、不导入旧模型/知识库/数据，也不是设置页导入功能。不要回显 env 或把密钥发进聊天。

## 公网回调

本机保留 `127.0.0.1:3000/feishu/webhook`；desktop 使用独立官方 webServer，与根设置页面临时端口不同。配置 dedicatedIngress=true、deploymentPort=3000；改端口时同步隧道上游并重启。

现用地址为 `https://feishu.windborne.cc/feishu/webhook`。只代理 webhook，其他路径返回 404，不能公开 Harness、配置或知识库管理接口。例如 Cloudflare Tunnel：

```yaml
ingress:
  - hostname: feishu.windborne.cc
    path: ^/feishu/webhook$
    service: http://127.0.0.1:3000
  - service: http_status:404
```

后台配置同一 HTTPS 地址及来源校验，完成 challenge，订阅 `im.message.receive_v1`，卡片交互 `card.action.trigger` 使用同一 webhook，按后台要求发布。不配 OAuth 重定向地址。

切换前停旧消费者，保证唯一回调写者。身份只取验证过的 sender/operator，不信正文或按钮自报身份。普通群按 chat 共享会话，话题按 thread 隔离，附件和确认继续绑定本人。

## 真实验收

当前真实入站/回复及部分表格操作已有日志证据，监听 3000 已启用；旧的 502、未配置和无监听记录不代表当前状态。隔离 mock 通过不代表下列场景均已实测：

| 范围 | 核对内容 |
|---|---|
| 入站/身份 | challenge、签名/AES、管理员拒绝、群/话题、重投去重 |
| 展示/控制 | 富文本与卡片、停止、询问/审批、确认/取消、他人操作、过期与重启 |
| 附件/投递 | 真实格式和大小、归属与引用、哈希、限流/断网、逐文件回执 |
| 资源 | 表格多页/批次/冲突/unknown，文档范围，日历重复/通知，任务负责人/截止/完成 |
| 业务/维护 | Codex 线程与权限、发布/Git、auto/confirm、失败投递恢复及备份恢复 |

使用专用 chat、明确资源和清理范围；新获授权后才发送测试消息、邀请、改数据或推送 Git。命令统一走 [pnpm verify --live](../tests/README.md#真实服务检查)，不保存秘密、原始敏感事件或文件正文。
