# 测试入口

按改动选择组；默认 pnpm test 仍运行全部本地测试，不减少覆盖。唯一分组清单为 [tooling/test-suites.json](../tooling/test-suites.json)，测试文件保持原路径以免破坏引用。

```powershell
pnpm test --list
pnpm test --suite feishu
pnpm test --suite core --suite business
pnpm test
```

| 组 | 范围 |
|---|---|
| core | 产物闭包、原生命令/目录/模型、调度规则原型、验收隔离 |
| feishu | webhook/身份/队列/会话/收发、配置/凭据持久化、原生设置接口与所有者身份安全 |
| desktop | 原生 RPC 身份拒绝、持久所有者/投递、完整权限与飞书隔离、幂等 |
| business | 四类飞书应用资源/冻结账本、交互附件、Codex/adviser、P5 发布/Skill、P6 规则/授权/恢复与投递 |

| 改动 | 追加的官方安装组合验证 |
|---|---|
| 任意生产改动/交付 | build、完整 test、pack:plugins --fixtures、check:artifacts、verify:harness |
| 飞书渠道 | verify:feishu、verify:feishu --web |
| 飞书配置/页面/生命周期 | verify:feishu:setup、verify:feishu:setup --desktop（模拟桌面根端口 0）；页面改动再在官方设置页做假配置浏览器填写/保存核对 |
| 业务接线/附件/提醒/Codex/adviser 组合 | verify:features、verify:features --web |
| 原生入口/桌面业务 | verify:desktop（四种不含飞书的最小组合；另测组合、冷恢复和后台回执） |
| 高风险宿主/冷恢复 preset/调度原型 | verify:p0 |
| 原生 Codex 后端/权限 | verify:codex（真实 CLI，模型仍为 loopback） |
| 外部知识库版本/安装/目录迁移 | verify:knowledge（官方宿主、合成文本、关键词检索/重启） |

所有上述检查使用本机 mock 与隔离 home；DSH 宿主为仓库外官方发行。真实飞书工具不属于默认测试，需要明确的测试资源与写入/发送授权，绝不自动导入真实 env。命令和结果位置见 [docs/testing.md](../docs/testing.md)，验证事实见 [兼容记录](../docs/compatibility.md)。
