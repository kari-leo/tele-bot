# 每月预算自动监控系统（AI Cost Budget Monitor）设计文档

## 1. 项目概述

本系统用于监控远程AI助手（如 OpenAI、Claude、国产大模型API）的调用成本，实现：

- 实时费用统计
- 月度预算控制
- 模型使用优化建议
- 超预算自动降级/限制
- 成本可视化与告警

目标是让用户在有限预算（如 200–400 RMB/月）内稳定使用多模型AI系统。

---

## 2. 设计目标

### 2.1 核心目标

- 精确统计每次AI API调用成本
- 支持多模型（OpenAI / Claude / Kimi / GLM / Qwen）
- 支持按“日 / 周 / 月”维度统计
- 提供预算上限控制机制
- 自动生成成本报告

### 2.2 非目标（明确不做）

- 不负责模型调用本身（仅监控层）
- 不实现复杂LLM训练或推理
- 不依赖云服务（默认本地运行）

---

## 3. 系统架构

### 3.1 总体架构

```
+----------------------+
|  AI Application Layer|
| (Agent / Bot / CLI)  |
+----------+-----------+
           |
           v
+----------------------+
| Cost Middleware      |
| (Token Tracker)      |
+----------+-----------+
           |
           v
+----------------------+
| Budget Monitor Core  |
| - cost calculator    |
| - policy engine      |
| - routing hints      |
+----------+-----------+
           |
           v
+----------------------+
| Storage Layer        |
| SQLite / JSON Logs   |
+----------+-----------+
           |
           v
+----------------------+
| Dashboard / Reports  |
| CLI / Web / Telegram |
+----------------------+
```

---

## 4. 核心模块设计

---

## 4.1 Token 计费模块（Cost Calculator）

### 功能

- 输入 tokens 估算
- 输出 tokens 估算
- 根据模型计算费用

### 示例公式

```text
cost = (input_tokens / 1e6) * input_price
     + (output_tokens / 1e6) * output_price
```

### 支持模型定价结构

```python
MODEL_PRICING = {
    "gpt-4.1": {
        "input": 2.0,
        "output": 8.0
    },
    "claude-sonnet": {
        "input": 3.0,
        "output": 15.0
    },
    "qwen": {
        "input": 0.3,
        "output": 1.0
    }
}
```

---

## 4.2 预算控制模块（Budget Controller）

### 功能

- 设置月度预算
- 跟踪当前消耗
- 判断是否超限

### 规则定义

```python
MONTHLY_BUDGET = 300  # RMB

def is_over_budget(current_cost):
    return current_cost >= MONTHLY_BUDGET
```

### 分级策略

| 使用比例 | 行为 |
|----------|------|
| < 50% | 正常使用 |
| 50–80% | 提醒 |
| 80–100% | 降级模型 |
| > 100% | 阻断高成本模型 |

---

## 4.3 模型路由模块（Cost-Aware Router）

### 功能

根据预算状态自动选择模型

### 示例策略

```python
def select_model(task_type, budget_ratio):
    if budget_ratio > 0.8:
        return "cheap_model"

    if task_type == "coding":
        return "gpt-4.1"

    if task_type == "long_context":
        return "claude-sonnet"

    return "gpt-4o-mini"
```

---

## 4.4 数据存储模块（Storage Layer）

### 存储方式

默认使用 SQLite

### 表结构

#### requests 表

```sql
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost REAL,
    task_type TEXT
);
```

#### daily_summary 表

```sql
CREATE TABLE daily_summary (
    date TEXT PRIMARY KEY,
    total_cost REAL
);
```

---

## 4.5 预算分析模块（Analytics Engine）

### 功能

- 日消耗统计
- 月消耗预测
- 模型成本分布分析

### 示例输出

```text
Monthly Projection:
Current spend rate: 9.8 RMB/day
Estimated monthly cost: 294 RMB

Top cost driver:
- GPT-4.1 coding: 62%
- Claude long context: 28%
```

---

## 4.6 告警系统（Alert System）

### 触发规则

```python
if cost_ratio > 0.5:
    send_alert("50% budget used")

if cost_ratio > 0.8:
    send_alert("Switching to low-cost models")

if cost_ratio > 1.0:
    send_alert("Budget exceeded")
```

### 通知方式

- CLI warning
- Telegram bot message
- log file

---

## 5. 数据流设计

### 单次请求流程

```
User Request
    ↓
Task Classification
    ↓
Model Router (budget-aware)
    ↓
API Call
    ↓
Token Usage Capture
    ↓
Cost Calculation
    ↓
Database Storage
    ↓
Budget Evaluation
    ↓
Alert / Feedback
```

---

## 6. 技术选型

| 模块 | 技术 |
|------|------|
| 后端 | Python |
| API层 | FastAPI |
| 存储 | SQLite |
| 任务调度 | APScheduler |
| 可视化 | Streamlit / CLI |
| 通知 | Telegram Bot API |

---

## 7. MVP实现范围

### 第一阶段（必须实现）

- token计费
- SQLite记录
- 月度预算统计
- 简单CLI输出

---

### 第二阶段（优化）

- 模型路由
- 自动降级策略
- 日报系统

---

### 第三阶段（增强）

- Telegram bot接入
- Web dashboard
- 多设备同步

---

## 8. 成本优化策略

### 8.1 Prompt压缩

- 自动summary history
- 避免重复上下文

### 8.2 Cache机制

- 相同问题复用结果
- embedding检索

### 8.3 模型分层调用

- cheap model first
- expensive model fallback

---

## 9. 风险与限制

### 9.1 成本误差

- token估算可能偏差 ±10%

### 9.2 API价格变动

- 需支持动态更新 pricing config

### 9.3 多模型兼容问题

- 不同API返回格式不一致

---

## 10. 扩展方向

- 自动优化 prompt（Prompt compression agent）
- 多用户系统（SaaS化）
- AI成本信用系统（类似“余额”概念）
- 企业级调用分析

---

## 11. 总结

该系统本质是：

> 一个“AI调用的财务操作系统（AI FinOps Layer）”

它解决的不是“用什么模型”，而是：

- 如何控制AI消费行为
- 如何在预算内最大化能力
- 如何让AI使用变得可预测、可管理

---