# AI Telegram Agent 项目分阶段实施说明（小白执行版）

---

## 0. 你要做的东西是什么（一句话理解）

你要做的是一个系统：

```
Telegram = 输入输出界面
AI Agent = 大脑
Tools = 执行能力（搜索 / 本地命令 / ROS / 文件）
```

最终效果：

- 在 Telegram 发一句话
- AI 自动帮你：
  - 搜索资料
  - 总结成文档
  - 执行本地任务
  - 定时推送最新研究

---

# 第一阶段：跑通最简单的“AI聊天机器人”

## 目标

你只需要做到：

```
Telegram → AI → 回复
```

不需要工具、不需要复杂系统。

---

## 你要安装的东西

```bash
pip install python-telegram-bot openai
```

---

## 你要做的事情

### 1. 创建 Telegram Bot

在 Telegram 搜：

```
@BotFather
```

执行：

```
/newbot
```

你会得到：

```
BOT_TOKEN
```

---

### 2. 写最简单代码

创建文件：

```
bot.py
```

写入：

```python
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from openai import OpenAI

client = OpenAI(api_key="YOUR_OPENAI_KEY")

async def handler(update: Update, context):
    user_text = update.message.text

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "user", "content": user_text}
        ]
    )

    reply = response.choices[0].message.content
    await update.message.reply_text(reply)

app = ApplicationBuilder().token("YOUR_BOT_TOKEN").build()
app.add_handler(MessageHandler(filters.TEXT, handler))

app.run_polling()
```

---

## 运行

```bash
python bot.py
```

---

## 阶段结果

你现在已经有：

```
Telegram + GPT 聊天机器人
```

---

# 第二阶段：加入“工具能力（Tools）”

## 目标

让 AI 不只是聊天，而是能：

```
- 搜索
- 运行命令
- 读文件
```

---

## 你要理解一个关键概念

AI 现在变成：

```
AI = 会思考 + 会调用工具
```

---

## 添加第一个工具：搜索

```python
def search_tool(query):
    return f"模拟搜索结果：{query}"
```

---

## 修改 AI 逻辑（关键）

你要让 AI 决定：

```
要不要用工具
```

简单版本：

```python
def agent(user_text):
    if "搜索" in user_text:
        return search_tool(user_text)

    return chat_with_ai(user_text)
```

---

## 阶段结果

你现在可以：

```
Telegram → AI → 判断 → 调工具 → 回复
```

---

# 第三阶段：引入 Agent（LangGraph）

## 目标

解决问题：

```
AI 需要多步骤思考
```

例如：

```
用户：帮我写机器人论文总结
```

流程：

```
搜索 → 阅读 → 总结 → 写文档
```

---

## 安装

```bash
pip install langgraph langchain
```

---

## 核心概念

LangGraph = “流程控制器”

你不用再写 if else，而是：

```
AI 工作流图
```

---

## 最简单结构

```
START → AI → TOOL → AI → END
```

---

## 你不用一开始写复杂图

只要理解：

```
LangGraph = AI流程控制器
```

---

## 阶段结果

你可以：

```
多步任务自动执行
```

---

# 第四阶段：加入真实工具（Search / 文件 / 系统）

## 目标

让 AI 能做真实世界事情。

---

## 1. 搜索工具（推荐）

你可以用：

```
Tavily API（推荐）
```

或者：

```
SerpAPI
```

---

## 示例

```python
def search_web(query):
    return "真实搜索结果"
```

---

## 2. 文件工具

```python
def write_file(content):
    with open("output.md", "w") as f:
        f.write(content)
```

---

## 3. 本地命令（危险）

```python
import subprocess

def run_cmd(cmd):
    return subprocess.check_output(cmd, shell=True).decode()
```

---

注意：

```
这一部分必须小心（后面会讲安全）
```

---

## 阶段结果

AI 可以：

```
搜索 + 写文件 + 执行任务
```

---

# 第五阶段：做“研究助手”（你的核心需求）

## 目标功能

你要实现：

```
/research humanoid robotics
```

自动输出：

```
1. 最新论文
2. 技术总结
3. Markdown报告
```

---

## 工作流

```
用户输入
→ 搜索 arXiv
→ 搜索网页
→ 提取信息
→ AI总结
→ 写成Markdown
→ Telegram返回
```

---

## 工具组合

```
- arxiv API
- web search
- LLM summarization
- markdown writer
```

---

## 阶段结果

你得到一个：

```
AI研究员
```

---

# 第六阶段：加入定时任务（前沿追踪）

## 目标

每天自动：

```
推送最新论文 / 技术动态
```

---

## 安装

```bash
pip install apscheduler
```

---

## 示例

```python
from apscheduler.schedulers.background import BackgroundScheduler

def daily_task():
    result = agent("总结 humanoid 最新进展")
    send_to_telegram(result)

scheduler = BackgroundScheduler()
scheduler.add_job(daily_task, "interval", hours=24)
scheduler.start()
```

---

## 阶段结果

你拥有：

```
AI自动日报系统
```

---

# 第七阶段：加入 MCP（高级扩展）

## MCP 是什么

一句话：

```
AI 工具标准接口系统
```

---

## 为什么需要 MCP

因为你会有很多工具：

```
ROS
shell
browser
filesystem
search
```

MCP 让它们统一：

```
所有工具 = 标准接口
```

---

## 架构

```
AI → MCP Client → MCP Server → 工具
```

---

## 阶段结果

你得到：

```
可扩展 AI 工具系统
```

---

# 第八阶段：安全系统（非常重要）

## 必须做的事

---

## 1. 用户白名单

```python
ALLOWED_USERS = [123456]
```

---

## 2. 工具分级

```
L0：搜索（安全）
L1：写文件
L2：执行命令
L3：控制机器人（危险）
```

---

## 3. 危险操作确认

```
是否执行？
[YES] [NO]
```

---

## 4. 禁止直接执行用户输入

❌ 错误：

```
subprocess.run(user_input)
```

---

# 第九阶段：系统结构整理（最终形态）

你的项目最终应该长这样：

```
telegram-bot/
    bot.py

agent/
    graph.py
    memory.py

tools/
    search.py
    arxiv.py
    shell.py
    file.py

skills/
    research.py
    summary.py

scheduler/
    jobs.py

mcp/
    client.py
    servers/
```

---

# 最终你会得到什么系统

## 1. Telegram AI助手

```
聊天 + 问答
```

## 2. AI研究员

```
自动搜论文 + 写报告
```

## 3. AI运维工具

```
控制ROS / Linux / GPU
```

## 4. AI情报系统

```
每天推送前沿动态
```

---

# 最重要总结（必须记住）

```
第一阶段：AI聊天
第二阶段：AI + 工具
第三阶段：AI Agent
第四阶段：真实世界执行
第五阶段：自动研究系统
```

---

如果你下一步要继续，我可以帮你做三件更关键的事：

1. 给你“可直接运行的完整代码模板（最小系统）”
2. 给你“ROS + AI Agent 专用工具设计”
3. 给你“从0到可用系统的目录初始化脚本”

你直接说你要哪一步就行。
```