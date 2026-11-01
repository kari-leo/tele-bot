# 受控定时自动化

官方 schedule 是唯一计时器。插件以公开 catalog/history 的结构化回执关联专用的只收唤醒 Session，再通过唯一业务派发者执行；不解析提醒正文，不新增扫描线程。

本机 `/telebot/automation/setup` 配置管理员、允许目录、时区、迟到容忍及最长 1200 秒执行限额。任务 `input`：`{title,prompt,mode:remind|auto|confirm,executor:research|codex_apply,catchUp:skip|run_once|ask,rule:{kind:once|daily|weekly,first:带偏移的ISO时间,timeZone,interval:1,weekdays:[0],count,until}}`。weekday 为旧规则周一=0；日期间隔按时区日历计算，DST 空洞跳过、重叠取较早时刻。

`/schedule create <input JSON>`、`update {id,input}`、`delete <id>`、`batch [{action,input,id}]` 先冻结 15 分钟变更提案；`confirm <token>` 消费一次。auto 还需独立 `authorize <id>` 及本人确认。confirm 每次生成 execution ID，经 `execute|reject <ID>` 一次性授权。list/get/history/status 查询不走模型。任务修改撤销旧执行授权和待执行项；配置策略变化暂停任务重新授权。

恢复逐次记录遗漏，最多补执行/申请授权一次；默认迟到 5 分钟 skip。串行执行，运行中崩溃进入 unknown 不重跑。Codex 通过其独立连接器的原生工作区沙箱；research 只允许网页搜索/提取、受控文件读/列举和只新建报告，禁止 Shell/MCP/发布/任意飞书写入。出站经 core 可选交付端口，发送失败只恢复投递；删除保留历史。官方 Web 若改动或删除本插件的唤醒记录，任务暂停等待核对。

`/schedule stop` 停止当前运行；history/status 分离执行与逐文件投递；retry-delivery 只重试明确失败/待投递，不重做业务、不重置 unknown。唤醒会话持久绑定受限 preset，公开 SessionController 冷恢复也不调用模型或业务工具。
