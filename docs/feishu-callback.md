# 接通飞书公网回调

用户指定原 Cloudflare 隧道继续使用 **3000**。业务全部为应用身份，无 OAuth 回调。此前诊断为公网 502、本机 3000 无监听；本轮已安装桌面插件，飞书仍缺本人手填配置、尚未监听 3000；没有修改实际隧道，下面在补齐配置后执行。

## 先让桌面插件监听 3000

按[安装指南](installation.md)将桌面升级为 0.2.1-rc.5，用其自带 CLI 安装 core/feishu 到 desktop profile。飞书配置启用 `dedicatedIngress: true`、`deploymentPort: 3000`，然后重启桌面。该模式复用隔离的官方 webServer，不受桌面根服务临时端口影响。

```powershell
Test-NetConnection 127.0.0.1 -Port 3000
```

在 Harness 桌面“设置 → Telebot 插件 → 飞书配置”中填写；不打开 3000 端口的配置网页。手填现有 App ID/Secret/Verification Token、管理员和工作目录，保存启用。默认主模型在官方宿主设置；飞书 provider/model 可同时留空。

公网 URL 填 `https://feishu.windborne.cc/feishu/webhook`。本机普通 GET 的 404/405 不证明失败或成功，实际验收需飞书合法 POST challenge。

## 核对原隧道

现有配置路径 `C:/Users/41547/.cloudflared/config.yml`。仅当现有规则不符合时，先备份再合并以下 ingress 片段，保留 tunnel/credentials-file/其他域名和全局兜底：

```yaml
- hostname: feishu.windborne.cc
  path: ^/feishu/webhook$
  service: http://127.0.0.1:3000
- hostname: feishu.windborne.cc
  service: http_status:404
```

不要转发整个站点；配置页、官方桌面/知识库管理接口只留本机。改 UI 端口需同时改隧道上游并重启桌面。

```powershell
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://feishu.windborne.cc/feishu/webhook
cloudflared tunnel ingress rule https://feishu.windborne.cc/telebot/feishu/setup
```

webhook 应匹配本机 3000；setup 应匹配 404。按原服务管理方式重新加载隧道。

## 飞书后台

在应用的事件订阅请求 URL 填上述 HTTPS 地址，使用现有 Verification Token；只有启用 AES 时才补对应 Encrypt Key。完成 URL 校验后订阅 `im.message.receive_v1`；卡片交互使用同一 webhook，并按后台要求发布。无需新增 OAuth 重定向 URL 或用户授权。

最后在专用 chat 验收真实入站、回复、按钮和附件。此前仅三条指定发送授权已使用，本轮未新增消息；后续测试范围按[真实清单](feishu-live-checklist.md)确认。切换前停用旧消费者，避免重复执行。
