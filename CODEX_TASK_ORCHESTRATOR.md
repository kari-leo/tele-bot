# TeleBot Codex Task Orchestrator 设计文档

## 1. 背景

TeleBot 已具备通过工具调用外部能力完成任务的基本框架。对于代码开发类任务，计划接入 Codex CLI，使 TeleBot 可以将具体的软件工程任务交给 Codex 执行。

基础调用方式可以实现：

```text
用户请求
→ TeleBot 调用 Codex
→ Codex 执行
→ 返回结果
→ TeleBot 回复用户
```

但实际的软件开发任务通常无法在单轮调用中完成。例如：

- Codex 在分析过程中发现多个实现方案，需要进行选择；
- 修改代码后测试失败，需要继续调试；
- 实现过程中发现额外依赖，需要补充修改；
- Codex 需要进一步确认任务边界；
- 用户在第一次任务完成后继续要求调整同一个功能。

因此，Codex Tool 不应被设计为一次性的 Prompt → Result 工具，而应具备面向“任务”的持续多轮执行能力。

本功能的核心目标是：

> 将一个用户开发任务映射为一个持续存在的 Codex Thread，在任务生命周期内持续复用该 Thread，并由 TeleBot 自动协调 Codex 的多轮执行、决策、阻塞和后续修改。

---

# 2. 产品目标

## 2.1 核心目标

实现以下交互：

```text
用户：
帮我修改 scheduler，让周期任务失败后不会导致整个任务失效。

↓
TeleBot 创建 Codex Task A

↓
Codex Thread #123
分析代码

↓
Codex：
这里有两个实现方式……

↓
TeleBot 自动判断并选择方案

↓
resume Thread #123

↓
Codex 修改代码并测试

↓
发现测试失败

↓
TeleBot 要求继续修复

↓
resume Thread #123

↓
测试通过

↓
TeleBot：
任务已完成……
```

此时用户继续：

```text
用户：
刚才那个异常处理再增加三次重试。
```

TeleBot 应识别该请求属于 Task A：

```text
Task A
↓
thread_id = #123
↓
resume #123
```

而不是重新创建新的 Codex Thread。

---

# 3. 核心设计原则

## 3.1 Thread 属于任务，而不是会话

Codex Thread 的生命周期应与“用户任务”绑定，而不是与 Telegram Chat、TeleBot Conversation 或用户账号永久绑定。

错误设计：

```text
Telegram Chat
    ↓
永久 Codex Thread
```

这种方式会导致多个不同开发任务共享上下文，最终造成上下文污染。

正确设计：

```text
TeleBot Conversation
    │
    ├── Task A
    │    └── Codex Thread A
    │
    ├── Task B
    │    └── Codex Thread B
    │
    └── Task C
         └── Codex Thread C
```

一个任务对应一个 Codex Thread。

---

## 3.2 一个任务允许包含多轮 Codex Turn

Task 和 Codex 调用不是一一对应关系。

关系应为：

```text
Task
 └── Thread
      ├── Turn 1
      ├── Turn 2
      ├── Turn 3
      └── Turn N
```

例如：

```text
Task：实现文件上传功能

Turn 1：
分析现有代码

Turn 2：
确定接口设计

Turn 3：
修改代码

Turn 4：
运行测试

Turn 5：
修复测试问题

Turn 6：
重新测试
```

只有整体任务满足完成条件后，Task 才进入完成状态。

---

# 4. 用户流程

## 4.1 创建新任务

用户提出新的 Codex 开发任务：

```text
用户：
帮我让 Codex 给 scheduler 增加任务暂停功能。
```

TeleBot 判断：

```text
当前不存在对应 Codex Task
```

因此执行：

```text
Create CodexTask

↓

codex exec

↓

获取 thread_id

↓

保存：

task_id
thread_id
task_goal
task_status
```

随后进入自动执行流程。

---

## 4.2 Codex 自动多轮执行

第一次调用后，Codex 可能出现不同结果。

### 情况 A：任务已经完成

例如：

```text
已完成修改。

修改文件：
scheduler.py
task_manager.py

测试：
12 passed
```

TeleBot：

```text
Task → COMPLETED
```

并向用户汇总结果。

---

### 情况 B：需要普通技术决策

Codex：

```text
这里可以采用：

A. 修改现有 Scheduler
B. 引入新的 Queue abstraction

从当前代码规模来看，两种方式都可行。
```

如果该决策：

- 不改变用户核心需求；
- 不涉及高风险操作；
- 不显著扩大任务范围；
- TeleBot 可以依据上下文合理判断；

则由 TeleBot 自动决策。

例如：

```text
TeleBot：

采用方案 A，优先保持当前架构简单，继续实现并测试。
```

随后：

```text
codex exec resume <thread_id>
```

继续同一 Thread。

---

## 4.3 需要用户决策

部分问题不应由 TeleBot 自动决定。

例如：

```text
Codex：

当前数据库迁移存在两种方案：

A. 保留旧字段并兼容
B. 删除旧字段并迁移数据

方案 B 会删除旧结构。
```

如果涉及：

- 数据删除；
- 大规模架构变化；
- 明显扩大需求范围；
- 外部系统调用；
- 可能产生费用；
- 用户业务偏好；
- 无法根据当前上下文合理推断；

则 Task 状态进入：

```text
WAITING_USER
```

TeleBot 向用户发送决策问题。

用户回答后：

```text
resume thread
```

继续执行。

---

# 5. 用户完成后的后续修改

任务第一次完成后，不应立即丢弃 Thread。

例如：

```text
Task A
status = COMPLETED
thread_id = 123
```

用户随后说：

```text
刚才那个功能再增加失败重试。
```

TeleBot 需要进行 Task Resolution。

如果判断该请求属于：

- 原功能修改；
- 原任务补充；
- Bug 修复；
- 测试调整；
- 重构；
- 原实现解释；
- 原设计方案修改；

则恢复 Task A：

```text
COMPLETED
    ↓
RUNNING
```

并执行：

```text
codex exec resume 123
```

继续使用原 Thread。

---

# 6. 新任务与旧任务判定

增加 Task Resolver，用于判断用户当前请求应该：

```text
继续已有 Task
```

还是：

```text
创建新 Task
```

## 6.1 应继续已有 Task

例如：

```text
刚才那个再加个日志。
```

```text
之前的测试还是没过，继续修。
```

```text
把刚才方案 A 改成方案 B。
```

```text
异常处理改成最多重试三次。
```

```text
解释一下为什么刚才这么设计。
```

这些都属于原任务上下文。

---

## 6.2 应创建新 Task

例如原 Task 是：

```text
修改 scheduler 的周期任务逻辑
```

用户随后说：

```text
帮我开发一个天气查询插件。
```

这是新的独立目标，应：

```text
Create Task B

↓

Create Codex Thread B
```

---

# 7. CodexTask 状态模型

建议每个 Codex Task 至少包含以下逻辑字段：

```text
task_id

thread_id

task_goal

status

created_at

updated_at

turn_count

codex_model

execution_started_at

execution_deadline_at

last_codex_message

pending_question

parent_conversation_id
```

具体数据结构由实现层决定，本设计只定义逻辑需求。

---

# 8. Task 状态

建议定义以下状态。

## RUNNING

Codex 当前正在执行任务，或 TeleBot 正在自动推进下一轮。

---

## WAITING_AGENT_DECISION

Codex 提出了一个可以由 TeleBot 自主判断的问题。

例如：

```text
应该修改现有模块还是新增一个 wrapper？
```

TeleBot 做出判断后继续 resume Thread。

通常该状态只在内部短暂存在，不需要直接暴露给用户。

---

## WAITING_USER

Codex 遇到必须由用户做出的决定。

例如：

```text
是否删除旧数据库字段？
```

TeleBot 停止自动执行，并将问题发送给用户。

---

## COMPLETED

任务达到目标，并具备足够完成证据。

例如：

```text
代码修改完成
+
测试通过
```

或：

```text
用户指定目标已经实现
```

---

## BLOCKED

经过多轮执行仍无法完成。

例如：

```text
缺少外部 API Key
```

或者：

```text
连续多轮修改仍无法通过测试
```

此时 TeleBot 应向用户说明当前进度和阻塞原因。

---

## FAILED

任务因执行错误无法继续。

例如：

```text
Codex CLI 无法启动
```

```text
Thread 不存在
```

```text
CLI 返回不可恢复错误
```

---

## CANCELLED

用户主动终止任务。

---

# 9. 自动执行循环

Codex Tool 不再采用：

```text
run_codex(prompt)
```

这样的单次调用模型。

而应抽象为：

```text
run_codex_task(task)
```

逻辑类似：

```python
while task.status == RUNNING:

    result = codex_turn(
        thread_id=task.thread_id,
        model=task.codex_model,
        timeout_seconds=remaining_seconds(task.execution_deadline_at)
    )

    state = analyze_result(result)

    if state == DONE:
        task.status = COMPLETED
        break

    if state == NEED_AGENT_DECISION:
        decision = telebot_decide(result)
        resume(thread_id, decision)
        continue

    if state == NEED_USER:
        task.status = WAITING_USER
        break

    if state == BLOCKED:
        task.status = BLOCKED
        break
```

TeleBot 是任务协调者，而 Codex 是代码执行者。

---

# 10. TeleBot 与 Codex 的职责边界

## TeleBot 负责

```text
理解用户目标

创建任务

识别任务关联关系

管理 thread_id

管理任务生命周期

决定是否继续执行

进行低风险技术决策

判断是否需要询问用户

控制执行预算

汇总 Codex 执行结果

向用户汇报
```

---

## Codex 负责

```text
读取代码

分析实现

修改代码

运行命令

运行测试

定位错误

修复问题

验证结果
```

核心原则：

> TeleBot 决定“做什么、是否继续、什么时候问用户”；Codex 决定“代码具体怎么做”。

---

# 11. 防止无限循环

TeleBot 不允许无限调用 Codex。

例如：

```text
Codex：
测试失败

↓

TeleBot：
继续修

↓

Codex：
仍然失败

↓

TeleBot：
继续修

↓

……
```

需要设置 Execution Budget。

例如逻辑限制：

```text
max_turns

max_retries

max_consecutive_failures

max_execution_seconds = 1200
```

具体阈值可配置。

`max_execution_seconds` 的硬上限为 1200 秒（20 分钟）。计时从本次自动执行开始，
覆盖创建 Thread、所有自动 resume、Codex 子进程和中间的 TeleBot 决策时间。
每轮 Codex CLI 调用只获得当前剩余时间，不会在 resume 时重新获得 20 分钟。
允许配置更短的时限，不允许配置超过 20 分钟。等待用户决定时停止本次自动执行；
用户答复后开启新的执行窗口，但继续原 Task 和 Thread。

例如：

```text
max_turns = 8

max_retries = 3
```

达到限制后：

```text
Task → BLOCKED
```

而不是：

```text
Task → COMPLETED
```

TeleBot 向用户反馈：

```text
当前已经进行了 8 轮尝试。

已完成：
- scheduler 状态拆分
- 重试机制

当前阻塞：
- integration test 仍然失败

错误集中在……
```

用户可以决定：

```text
继续尝试
```

此时恢复：

```text
BLOCKED → RUNNING
```

并继续原 Thread。

超时也按执行预算耗尽处理：终止当前 Codex 子进程，保留已取得的输出、变更和
`thread_id`，将 Task 标记为 `BLOCKED`，向用户报告实际进度和超时原因；
不得把超时当作完成，也不得自动开启新的 20 分钟窗口继续运行。

---

# 12. 任务完成判定

不能仅依赖 Codex 输出：

```text
Done
```

就认为任务完成。

应综合判断是否存在完成证据。

可能的完成证据包括：

```text
代码已修改

测试通过

Build 成功

用户要求的文件已经生成

指定接口已经实现

指定 Bug 已经复现并修复
```

建议 Codex 每次任务结束时输出结构化结果：

```text
STATUS: COMPLETED

SUMMARY:
完成 scheduler 重构。

CHANGES:
- 修改 scheduler.py
- 修改 task_manager.py

VALIDATION:
- pytest: 18 passed

REMAINING:
None
```

TeleBot 可以据此判断 Task 状态。

---

# 13. Task Resume 机制

每次 Codex 调用都应保存：

```text
task_id → thread_id
```

例如：

```text
task_001 → thread_abc

task_002 → thread_def
```

调用逻辑：

首次执行：

```text
codex exec --model <task.codex_model>
```

获取：

```text
thread_id
```

后续所有执行：

```text
codex exec resume --model <task.codex_model> <thread_id>
```

直到任务生命周期结束。

即使任务已经 COMPLETED，也应该在一定范围内保留该映射，用于用户后续调整。

## 13.1 模型指定与修改

用户可以在创建任务时指定 Codex 模型，也可以对已有 Task 明确要求更换模型。
未指定时使用 TeleBot 的全局默认值 `TELE_BOT_CODEX_MODEL`。Task 创建后将实际使用的
模型保存到 `codex_model`，后续自动 resume 一律读取该字段，而不依赖进程启动时的默认值。

模型更改以 Task 为单位；从下一轮 Codex Turn 生效，正在运行的 Turn 不会被中途切换。
更改模型时继续使用原 `thread_id`，并记录旧模型、新模型、操作者和更改时间。
更改全局默认模型只影响之后创建的新 Task；已有 Task 保持原设置，除非用户明确修改。
每次执行结果应显示实际请求的模型。CLI 或当前登录账号不支持指定模型时，保留
Task 和 Thread，向用户报告原始模型不可用的原因并允许选择其他模型，不要静默回退。

示例：

```text
用户：用 gpt-6.1-sol 创建这个 Codex 任务。
TeleBot：Task A 使用 gpt-6.1-sol。

用户：把 Task A 的 Codex 模型改为 gpt-6-astra，继续刚才的任务。
TeleBot：保存新模型；下一轮 resume 原 Thread 时传入 --model gpt-6-astra。
```

---

# 14. 多任务场景

用户可能同时维护多个 Codex Task。

例如：

```text
Task A
scheduler 重构
thread_001

Task B
Knowledge Tool 接口修改
thread_002

Task C
Telegram 消息格式优化
thread_003
```

用户说：

```text
继续修刚才 scheduler 的问题。
```

Task Resolver 应匹配：

```text
Task A
```

用户说：

```text
Knowledge Tool 那边增加错误日志。
```

匹配：

```text
Task B
```

因此 Codex Tool 需要维护多个可恢复 Thread，而不是单一的：

```text
current_thread_id
```

---

# 15. 推荐架构

整体结构：

```text
                         User
                           │
                           ▼
                    ┌─────────────┐
                    │   TeleBot   │
                    └──────┬──────┘
                           │
                           ▼
                  Intent / Task Resolver
                           │
               ┌───────────┴───────────┐
               │                       │
         Existing Task              New Task
               │                       │
        Resume Thread            Create Thread
               │                       │
               └───────────┬───────────┘
                           ▼
                  ┌─────────────────┐
                  │ CodexTaskManager│
                  ├─────────────────┤
                  │ task_id         │
                  │ thread_id       │
                  │ goal            │
                  │ status          │
                  │ turn_count      │
                  │ codex_model     │
                  │ budget          │
                  └────────┬────────┘
                           │
                           ▼
                      Codex CLI
                           │
                 exec / exec resume
                           │
                           ▼
                    Codex Thread
                           │
            ┌──────────────┼──────────────┐
            │              │              │
           Done       Need Decision     Blocked
            │              │              │
            │       ┌──────┴──────┐       │
            │       │             │       │
            │    TeleBot        User      │
            │    decides        decides   │
            │       │             │       │
            └───────┴──────┬──────┴───────┘
                           │
                         Resume
```

---

# 16. 推荐模块划分

建议拆分为以下模块。

## CodexCLIAdapter

只负责 Codex CLI 调用。

提供类似能力：

```text
create_thread(prompt, model, timeout_seconds)

resume_thread(thread_id, prompt, model, timeout_seconds)
```

不负责业务判断。

---

## CodexTaskManager

负责：

```text
创建 Task

保存 thread_id

修改状态

统计执行轮数

恢复任务

结束任务

管理 Execution Budget

保存并修改 Task 的 Codex 模型
```

---

## CodexTaskResolver

负责：

```text
当前用户请求
        ↓
是否属于已有 Codex Task？
```

输出：

```text
existing_task_id
```

或：

```text
NEW_TASK
```

---

## CodexResultInterpreter

分析 Codex 返回结果。

判断：

```text
COMPLETED

NEED_AGENT_DECISION

NEED_USER

CONTINUE

BLOCKED

FAILED
```

---

## CodexDecisionAgent

处理 Codex 提出的普通技术决策。

要求：

```text
不能改变用户核心目标

不能进行高风险行为

不能自行扩大任务范围
```

否则转交用户。

---

## CodexTaskOrchestrator

作为上述模块的统一协调层。

逻辑：

```text
用户任务
↓
Task Resolver
↓
Task Manager
↓
CLI Adapter
↓
Result Interpreter
↓
继续 / 决策 / 等待用户 / 完成
```

---

# 17. MVP 范围

第一版不需要实现过度复杂的自主 Agent。

MVP 只需要支持：

```text
1. 新任务创建 Codex Thread

2. 保存 task_id ↔ thread_id

3. 同一任务自动 resume

4. Codex 出现普通问题时 TeleBot 可以继续推进

5. 重大决策暂停并询问用户

6. 用户后续修改可以重新进入旧 Task

7. 设置最大自动执行轮数

8. 最终统一汇总执行结果

9. 创建任务时指定模型，后续可修改 Task 模型并在下一轮生效

10. 自动执行窗口与单次 Codex 调用均不得超过 20 分钟
```

第一版暂不需要：

```text
复杂 DAG Task

多个 Codex Thread 协作

Codex 子 Agent

自动跨任务依赖管理

长期无限 Thread

复杂项目管理系统
```

---

# 18. 核心体验

用户侧理想体验应该是：

```text
用户：
帮我让 Codex 修改 scheduler，
周期任务失败后不要让整个任务失效。

↓

TeleBot：
正在处理……

↓

[内部]

Codex 分析
↓
TeleBot 决策
↓
Codex 修改
↓
Codex 测试
↓
TeleBot 继续
↓
Codex 修复
↓
测试通过

↓

TeleBot：

已完成。

主要修改：
- 分离任务状态和执行状态
- 单次执行失败不会终止周期任务
- 保留下一次调度时间

验证：
18 项测试通过。
```

整个过程中用户不需要参与 Codex 的普通工程细节。

但如果出现：

```text
需要删除已有数据库字段，
是否继续？
```

TeleBot 才中断并询问用户。

---

# 19. 验收标准

MVP 完成后，应满足以下条件。

### 新任务

用户要求 Codex 完成新的开发任务时：

```text
必须创建新的 Codex Thread
```

并持久化 Thread ID。

---

### 自动多轮

Codex 第一轮未完成任务时：

```text
TeleBot 可以自动继续调用同一 Thread
```

而不是重新开始。

---

### 决策

普通技术问题：

```text
TeleBot 可以自主处理
```

重大产品或高风险问题：

```text
必须询问用户
```

---

### 后续调整

任务完成后用户提出：

```text
“刚才那个再改一下”
```

TeleBot 能够定位原 Task，并继续原 Thread。

---

### 多任务隔离

两个不同 Codex 开发任务：

```text
不得共享同一个 Thread。
```

---

### 防止死循环

达到最大执行预算时：

```text
必须停止自动执行
```

并将任务标记为 BLOCKED，而不是假装完成。

---

### 模型选择

新 Task 可以指定模型；未指定时继承全局默认模型。修改已有 Task 的模型后，
下一次 resume 必须在原 Thread 上使用新模型。模型不可用时必须明确报错，
不能静默换模型或丢失 Thread。

---

### 20 分钟超时

一次自动执行从开始到结束最多持续 1200 秒，包含所有自动续跑；单次 CLI 调用
不得超过剩余时间。达到上限后必须停止自动执行、保留任务进度并报告超时。

---

### 结果汇报

任务结束时必须返回：

```text
做了什么

修改了什么

如何验证

是否存在遗留问题
```

而不是直接转发 Codex 的完整过程日志。

---

# 20. 一句话产品定义

> Codex Task Orchestrator 是 TeleBot 中面向软件工程任务的持续执行层：它以“任务”为单位管理 Codex Thread，由 TeleBot 负责目标、决策和生命周期管理，由 Codex 负责代码实现和验证，使一次用户指令可以自动完成多轮软件工程执行，并允许后续修改持续复用原任务上下文。
