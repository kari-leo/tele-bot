# Codex 会话权限对照

2026-10-31 用户补充旧项目作为参考。实际核对的是仓库外 `D:/files_data/windborne/tele_bot` 的 commit **a8da478**，相关入口为 `tele_bot/tools/codex_history.py:247`、`router/codex_history_service.py` 和 `README.md:279`。只参考行为，不复制 Python Agent/执行循环，不读取用户历史正文或凭据。

| 边界 | 旧项目参考 | 当前插件 |
|---|---|---|
| inspect/plan | 只读沙箱 | 已有，后端必须保证只读 |
| apply | 本人对冻结范围确认后执行 | 已有，15 分钟、绑定用户/chat/session/cwd/摘要，一次消费 |
| 原生历史 | 认证管理员本人私聊，直接读本机 Codex，无主模型调用 | 已有，管理员/指定 history owner 私聊，原生分页/读取 |
| 归档会话 | 只读，需在 Codex 取消归档后才能继续 | 已有拒绝归档续聊；不提供删除/归档接口 |
| 续聊的写权限 | 原会话追加一轮 workspace-write，每轮重置、不继承旧全盘权限 | 已实现原线程单轮确认续聊，显式 workspace-write，冻结 cwd/请求后一次消费 |
| 写入边界 | 当前 cwd 为 writableRoots；关闭 Shell 网络、排除临时目录默认写根；approvalPolicy=never | 显式 sandbox/sandboxPolicy：当前 cwd 唯一写根、networkAccess=false、排除 slashTmp/tmpEnv、approvalPolicy=never；拒绝服务端审批，不依赖全局 profile 恰好同名 |
| 执行中的线程 | 检查服务端返回 active；独立 app-server 不保证看见其他进程的全部状态 | 检查当前会话忙碌及原生 active；独立 app-server 对跨进程状态的可见性边界保留 |
| 超时/未知结果 | 最多 20 分钟，尝试中断，未知不自动重试 | 已有有界取消、持久 unknown、一用授权和产物哈希独立投递 |

原生 CLI 固定 0.160.0，实际 app-server/MXC 沙箱验收覆盖只读拒绝、外部/临时写根拒绝、取消/历史、确认 apply/可写续聊、CSV/XLSX 产物及选定 Skill 副本，结果索引见状态页。执行时间上限由 UI 配置并传入后端。Skill 只复制选中项到运行目录，校验源码/运行副本哈希，保留原目录和全局 .codex。

未知执行不自动重发；先核对原生线程和本地产物。文件上传/消息投递另有逐文件回执，不能用投递失败触发再次写代码。真实账户和原目录保留，不新增 Codex/OpenCLI 账户接入。

P8 的可信原生入口是单独的权限策略：默认完整权限，无需飞书管理员/目录限制或逐次 apply/续聊确认，实际调用官方 dangerFullAccess；inspect/plan 仍只读。真实 CLI 隔离验收覆盖 cwd 外合成写入、回环 HTTP、完整权限续聊/历史和去重。上述表中工作区限制、冻结确认继续用于飞书及主动受限模式。身份来自经认证的原生 RPC，权限标记不出现在模型参数中。

已移除 apply <令牌> 的旧确认别名：即使令牌有效，模型的 apply 工具也不能消费它。飞书/受限原生只接受可信 confirm 路径；组合验收含实际模型尝试该别名被拒绝、随后真实用户显式确认成功。
