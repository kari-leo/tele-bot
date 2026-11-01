# 报告与发布

独立安装到官方 DSH 0.2.1-rc.5，依赖 core。启用后通过 `/telebot/publishing/setup` 本机 UI 配置管理员、根目录、可选博客目录与 Git 目标；访问令牌位于独立 `config/setup.token`。首次缺项时只提供配置引导。

`write_report`/`/report {"filename":"report.md","content":"正文"}` 只新建并回读校验。`publish_source` 或 `/publish publish {"items":[...]}` 冻结源文件、元数据和目标；保留原文正文，单独本人确认后批量新建。`/publish git {"files":["指定.md"],"message":"提交说明"}` 再次确认后只提交指定文件并推送当前目标分支。Git 使用官方 Shell，保留既有登录，禁止混入已暂存文件和客户端 hooks。发布和 Git 分别授权，不自动推送。

`/publish confirm|cancel|status <令牌>`；令牌绑定用户/chat/会话/目录，15 分钟有效。内容/目标变化拒绝旧确认；中途失败保留已完成文件/提交审计并进入 unknown，不自动重复执行。卸载保留配置与账本。

`/publish files` 支持替换/移动/隔离删除预览，确认后保留恢复副本。Windows 本地 file Git 传输可能受官方沙箱 MSYS IPC 限制；保留 unknown，不关闭沙箱。HTTP 推送路径经合成本地仓库验证。
