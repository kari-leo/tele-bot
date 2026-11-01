# 飞书应用身份权限与资源配置

2026-10-31 最新决定：全部操作统一使用应用身份 tenant_access_token；删除用户 OAuth、用户 token、scopes 配置、授权命令及授权回调。用户已确认所需权限开通，不再要求申请 offline_access 或登记 OAuth 重定向 URL。

## 三层权限

- API 权限：使用原应用的应用身份权限，按后台要求发布/审批。用户已确认开通，本轮未读取后台完整清单。
- 资源权限：应用须对目标文档、Base、日历和任务清单有实际读写权。API scope 不等于获得全部个人资源。
- 插件权限：仅配置资源与能力可被管理员操作；冻结预览、本人确认和 unknown 对账保留。

日历使用应用身份无需 OAuth 回调；写入目标日历需 writer/owner。任务加入清单要求应用对该清单可编辑。仅支持用户身份的接口或应用不能访问的个人资源明确报不可用，不提供 OAuth 兜底。

官方依据：[日历身份与角色](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event/list)、[日历权限 FAQ](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-faq)、[任务创建](https://open.feishu.cn/document/task-v2/task/create)。

## 配置步骤

1. 按 [回调指南](feishu-callback.md) 在本机 3000 启动并打开 setup 链接。
2. 手动填写 App ID、管理员 ID、工作目录、App Secret、Verification Token；AES 启用时才填 Encrypt Key。已保存的密码框留空保留凭据，没有 env 导入入口。
3. 公网 webhook URL 填 https://feishu.windborne.cc/feishu/webhook，本地路径填 /feishu/webhook。没有 OAuth/scopes/身份选择项。
4. 启用业务，保留已有 Base；新增 Base 可通过链接读取数据表并加入白名单。其他资源手动填写元数据。
5. 保存并启用。普通配置写 config.json，秘密由官方 credentials 管理；应用 token 自动获取和更新，不手动粘贴。

资源示例（加入已有数组，替换占位 ID）：

```json
[
  {"alias":"app-calendar","kind":"calendar","id":"feishu.cn_REPLACE@group.calendar.feishu.cn","identity":"tenant","tokenRef":"FEISHU_APP_TENANT","expiresAt":0,"capabilities":["calendar.read","calendar.create","calendar.update","calendar.delete"]},
  {"alias":"app-tasks","kind":"tasks","id":"REPLACE_TASKLIST_GUID","identity":"tenant","tokenRef":"FEISHU_APP_TENANT","expiresAt":0,"capabilities":["tasks.read","tasks.create","tasks.update","tasks.delete"]}
]
```

identity/tokenRef 固定为上述应用值，expiresAt=0 由运行时更新。不要填写 owner、用户 token 或 refresh_token。逻辑能力不是后台权限编码。用户身份资源和外部凭据引用会被拒绝。

指定 Base 已有真实应用身份读写证据，无需重发。按使用场景准备文档/目录、Sheets 精确 range、calendar_id、tasklist_guid，并授予应用资源访问权；真实写入/通知按 [联调清单](feishu-live-checklist.md) 明确范围。

旧版配置若含 business.oauthRedirectUri、oauthScopes、allowTenant 或用户身份资源，停止 profile 后备份并手工移除，重新明确应用可访问的资源；不自动把用户资源转换为应用授权。旧凭据/业务数据不删除。尚未安装旧版时直接填写新表单即可。
