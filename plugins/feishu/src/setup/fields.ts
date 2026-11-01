import type { SettingField } from '@telebot/core/configuration'

export const feishuFields: SettingField[] = [
  {key:'appId',label:'App ID',section:'应用与会话'},
  {key:'adminUserIds',label:'管理员 ID',type:'list',section:'应用与会话',help:'填写实际事件中的 open_id 或 user_id，每行一个。'},
  {key:'allowedChatIds',label:'允许的会话 ID',type:'list',section:'应用与会话',help:'可留空；需要限定群聊时每行填写一个 chat_id。'},
  {key:'workspace',label:'Harness 工作目录',section:'应用与会话',help:'本机已存在的绝对目录。'},
  {key:'provider',label:'主模型 provider',section:'应用与会话',help:'与 model 同时留空使用 Harness 默认模型。'},
  {key:'model',label:'主模型 model',section:'应用与会话'},
  {key:'streamProgress',label:'执行进度提示',type:'boolean',section:'应用与会话',help:'思考和工具处理阶段发送限频状态提示，不发送模型内部思考内容。'},
  {key:'appSecret',label:'App Secret',type:'password',secret:true,section:'凭据与来源校验',help:'留空保留已有凭据；由官方凭据服务保存。'},
  {key:'verificationToken',label:'Verification Token',type:'password',secret:true,section:'凭据与来源校验'},
  {key:'encryptKey',label:'Encrypt Key',type:'password',secret:true,section:'凭据与来源校验',help:'Verification Token 或 Encrypt Key 至少填写一种。'},
  {key:'deploymentPort',label:'部署端口',type:'number',min:1,max:65535,section:'部署与回调',help:'修改后重启 Harness 生效；隧道上游需使用同一端口。'},
  {key:'dedicatedIngress',label:'桌面专用监听',type:'boolean',section:'部署与回调',help:'桌面版启用，webhook 使用独立的官方 HTTP 服务。'},
  {key:'publicWebhookUrl',label:'公网 webhook URL',section:'部署与回调',help:'HTTPS 地址，路径需与本地 webhook 路径一致。'},
  {key:'webhookPath',label:'本地 webhook 路径',section:'部署与回调'},
  {key:'businessEnabled',label:'启用飞书业务',type:'boolean',section:'业务资源',help:'多维表格在独立链接区管理；文档、日历、任务均使用应用身份。'},
  {key:'business.resourcesJson',label:'文档、日历和任务白名单',type:'json',emptyValue:[],section:'业务资源',help:'可留空，表示暂不接入这些资源。填写时使用资源数组；identity=tenant、tokenRef=FEISHU_APP_TENANT、expiresAt=0。不填写实际 token。'},
]
