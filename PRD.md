# AI Telegram Agent System PRD

## 1. 项目概述

### 1.1 项目名称
AI Telegram Agent System（ATAS）

### 1.2 项目目标
构建一个基于 Telegram 的 AI Agent 系统，用于：

- 远程信息搜索与总结
- 自动生成技术文档
- 跟踪前沿研究动态
- 执行本地系统任务（ROS / Shell / Git）
- 支持可扩展 Tool / Skill / MCP 架构

---

## 2. 系统架构

### 2.1 总体架构

```
Telegram User
    ↓
Telegram Bot (python-telegram-bot)
    ↓
Agent Gateway (FastAPI)
    ↓
LangGraph Agent Runtime
    ↓
Tool Orchestrator
    ├── Search Tools (Tavily / SerpAPI / arXiv)
    ├── Reading Tools (Firecrawl / Browser)
    ├── Execution Tools (OpenCLI / Shell / ROS)
    ├── File Tools (Markdown / PDF / Notes)
    └── Scheduler (APScheduler)
    ↓
LLM (GPT / Claude / Gemini)
```

---

## 3. 核心功能模块

### 3.1 搜索与信息获取

#### 功能
- Web 搜索
- 学术论文搜索
- 技术趋势追踪
- 社区信息（Reddit / GitHub）

#### 工具
- Tavily API
- SerpAPI
- arXiv API
- Firecrawl

---

### 3.2 文档生成系统

#### 功能
- 自动生成 Markdown 报告
- 技术综述
- 每日/每周总结
- PDF 导出

#### 输出格式
- Markdown
- PDF
- Notion（可选）

---

### 3.3 前沿动态追踪系统

#### 功能
- 定时搜索关键词
- 自动总结趋势
- Telegram 推送日报

#### 示例任务
- humanoid robotics
- motion planning
- diffusion policy
- ROS2 updates

---

### 3.4 本地任务执行系统

#### 功能
- ROS 命令执行
- Shell 脚本执行
- Git 操作
- 文件系统操作

#### 工具
- OpenCLI
- subprocess (restricted)
- ROS CLI wrapper

---

### 3.5 Agent 推理系统

#### 基于 LangGraph

能力包括：
- 多步骤推理
- Tool 调用
- 状态管理
- retry机制
- human-in-loop

---

### 3.6 Scheduler 系统

#### 功能
- 定时任务
- 每日研究总结
- 监控任务

#### 工具
- APScheduler

---

## 4. Tool System 设计

### 4.1 Tool 分类

| 类型 | 示例 | 权限等级 |
|------|------|----------|
| Search Tool | Tavily | L0 |
| Read Tool | Firecrawl | L0 |
| Document Tool | Markdown Writer | L1 |
| Shell Tool | OpenCLI | L2 |
| ROS Tool | Robot Control | L3 |

---

### 4.2 Tool 调用流程

```
LLM → Tool Selection → Policy Check → Execution → Result → LLM
```

---

## 5. Skill 系统设计

### 5.1 Skill 定义

Skill = 多 Tool 组合工作流

---

### 5.2 示例 Skills

#### research_skill
- 搜索论文
- 阅读内容
- 提取要点
- 生成报告

#### ros_debug_skill
- 查看 topic
- 检查 log
- 分析 error
- 提出修复建议

---

## 6. MCP 扩展层

### 6.1 目标

统一 Tool 接口标准

---

### 6.2 MCP Server 示例

- filesystem-mcp
- browser-mcp
- ros-mcp
- shell-mcp

---

## 7. 安全设计

### 7.1 用户权限控制

- Telegram User ID whitelist
- Role-based access control

---

### 7.2 Tool 权限分级

- L0: read-only tools
- L1: file write
- L2: system execution
- L3: robot control

---

### 7.3 执行安全策略

- 禁止 raw shell input
- command whitelist
- human approval for critical actions

---

### 7.4 沙箱机制

- Docker isolation
- restricted user environment

---

## 8. 技术栈

### Backend
- Python 3.10+
- FastAPI
- python-telegram-bot

### Agent
- LangGraph
- LangChain

### Tools
- Tavily API
- SerpAPI
- Firecrawl
- OpenCLI
- arXiv

### Scheduler
- APScheduler

---

## 9. 非功能需求

- 支持异步任务
- 支持长时间运行任务
- 支持并发请求
- 支持任务状态查询
- 支持日志记录

---

## 10. MVP 开发计划

### Phase 1
- Telegram Bot
- basic LLM reply
- simple search tool

### Phase 2
- LangGraph Agent
- tool calling
- markdown report

### Phase 3
- scheduler
- research skill
- arXiv tracking

### Phase 4
- OpenCLI integration
- ROS integration
- MCP layer

---

## 11. 成功标准

- 可通过 Telegram 完成完整 research workflow
- 可自动生成技术报告
- 可执行本地 ROS / shell 任务
- 可每日自动推送前沿动态