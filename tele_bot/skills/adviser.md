---
name: adviser
description: Consult an external second-opinion adviser (OpenCLI ChatGPT) at key decision points
trigger: 主 Agent 在做非平凡决策前希望获得第二意见时
tool: ask_adviser
cost: HIGH — 每次调用 1-5 分钟
---

# Adviser Skill

你的副驾。当你需要第二意见时调用 `ask_adviser(question, context)`。
返回的回复是一段纯文本（通常 1-5 句），可能与你的想法**不一致**——这正是它的价值。
你可以采纳，也可以选择忽略，最终行动由你决定。

## 何时调用 adviser

**调用**（值得为此等待 1-5 分钟）：
- 在写代码前，对设计思路不确定，想听 senior 的看法
- 在两个工具/方案之间犹豫，需要外部视角拍板
- 非可逆操作前的 sanity check（写文件、git commit、删数据）
- 用户给的需求模糊，你担心理解偏差
- code_review 工具报告了问题，但你不确定如何修复

**不要调用**（成本不值）：
- 简单事实性问题，自己有把握就直接答
- 已有专门工具能解决的事（搜索用 opencli_search，文件用 filesystem_*，代码用 code_*）
- 用户只是在闲聊
- 同一会话内已问过相同问题（缓存会返回旧回复，但调用次数仍占预算）

## 怎么问 adviser

**给足够上下文**——adviser 看不到你的对话历史，只看到你这次 call 传入的 `context`。

❌ 差：`ask_adviser("这样做对吗？")`
✅ 好：
```
ask_adviser(
  question="是否应该把 push 审批 token 写入 SQLite 而不是内存？",
  context="当前实现把 PendingApproval 存内存。问题：bot 重启会丢失 token，"
          "导致用户已收到的确认消息变成废纸。但写 SQLite 增加复杂度。"
)
```

**提具体问题**——开放性问题会得到开放性答案。

❌ 差：`question="评价我的设计"`
✅ 好：`question="这个设计在用户并发请求时会有 race condition 吗？"`

## 处理 adviser 的回复

- adviser 说"建议 X，但 Y 是 tradeoff"→ 自己判断 X 是否适用当前场景
- adviser 表态不明确（"depends on..."）→ 视作没拿到强信号，按自己原计划推进
- 回复以 `⚠️ adviser 暂不可用` 开头 → adviser 失败，**不要**把这段话给用户看，按自己判断继续
- adviser 给的具体修改建议 → 可以引用，但不要复述"adviser 说"——用户不需要知道你内部咨询过谁

## 成本提示

每次 `ask_adviser` 算作 ReAct 循环的 1 轮（占用 10 轮上限）。
连续调用 3 次以上属于过度依赖，重新审视你的问题——可能你需要的是更多上下文，而不是更多意见。
