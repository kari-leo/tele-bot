# Legacy 模式移除迁移指南

## 概述

本次更新移除了 Legacy 执行模式（`ControlledAgentExecutor`），简化为只使用 React 模式（`ReactAgentExecutor`）。

## 主要变更

### 1. 环境变量
- **移除**: `EXECUTOR` 环境变量（之前支持 `react` | `legacy`）
- **保留**: 
  - `TELEGRAM_STREAMING` - 流式进度开关（默认 `1`）
  - `SQLITE_CHECKPOINT_PATH` - SQLite 持久化路径（默认 `data/conversations.sqlite`）

### 2. 代码变更

#### app.py
- 移除 `_build_legacy_executor()` 函数
- `_build_react_executor()` 重命名为 `_build_executor()`
- 始终使用 `ReactAgentExecutor`

#### tele_bot/agent.py
- `AgentCore.__init__()` 简化，只接受 `executor: ReactAgentExecutor`
- 移除 `llm_client` 参数

#### tele_bot/agents/__init__.py
```python
# 之前
from tele_bot.agents import ControlledAgentExecutor, ReactAgentExecutor

# 现在
from tele_bot.agents import ReactAgentExecutor, AgentExecutionResult
```

#### tele_bot/config/runtime.py
```python
# 之前
@dataclass(frozen=True)
class RuntimeSettings:
    executor: str
    telegram_streaming: bool
    sqlite_checkpoint_path: str

# 现在
@dataclass(frozen=True)
class RuntimeSettings:
    telegram_streaming: bool
    sqlite_checkpoint_path: str
```

### 3. 启动日志变化

**之前**:
```
tele_bot starting executor=react streaming=on sqlite=data/conversations.sqlite
```

**现在**:
```
tele_bot starting streaming=on sqlite=data/conversations.sqlite
```

## 迁移步骤

### 对于部署环境

1. **更新环境变量**
   - 从 `.env` 文件中移除 `EXECUTOR=react` 配置
   - 确保 `TELEGRAM_STREAMING` 和 `SQLITE_CHECKPOINT_PATH` 配置正确

2. **更新依赖**
   ```bash
   /d/Anaconda3/envs/telebot/python.exe -m pip install -r requirements.txt --upgrade
   ```

3. **验证配置**
   ```bash
   /d/Anaconda3/envs/telebot/python.exe verify_legacy_removal.py
   ```

4. **重启服务**
   - Webhook 模式: 重启 FastAPI 应用
   - Polling 模式: 重启 `run_telegram_polling.py`

### 对于开发环境

1. **拉取最新代码**
   ```bash
   git pull
   ```

2. **更新依赖**
   ```bash
   pip install -r requirements.txt --upgrade
   ```

3. **运行验证脚本**
   ```bash
   python verify_legacy_removal.py
   ```

4. **运行测试**
   ```bash
   pytest tests/ -v
   ```

## 不再支持的功能

以下组件已被移除或不再使用：

- `ControlledAgentExecutor` - Legacy 执行器
- `EXECUTOR` 环境变量
- `tele_bot/agents/executor.py` 中的 `ControlledAgentExecutor` 类
- Legacy 模式相关的测试（已备份为 `test_executor.py.legacy_backup`）

## 保留的功能

所有核心功能完全保留：

- ✅ ReactAgentExecutor（唯一的执行器）
- ✅ LangGraph 工作流
- ✅ SQLite 对话持久化
- ✅ Telegram 流式进度推送
- ✅ 所有工具集成：
  - filesystem (list_dir, read_file)
  - shell_sandbox (执行白名单命令)
  - opencli_search (网络搜索)
  - knowledge_restore (知识还原)
  - write_report (报告生成)
  - blog_publish (博客发布，可选)
  - domain_hotspot (领域热点，可选)
  - ask_adviser (顾问咨询，可选)
- ✅ Skill 系统

## 回滚方案

如果需要回滚到 Legacy 模式：

```bash
git checkout <previous-commit>
pip install -r requirements.txt
```

然后在 `.env` 中设置：
```
EXECUTOR=legacy
```

## 验证清单

- [ ] 环境变量已更新（移除 `EXECUTOR`）
- [ ] 依赖包已安装/更新
- [ ] 验证脚本通过
- [ ] 服务启动正常
- [ ] Telegram 消息闭环正常
- [ ] 工具调用正常（搜索、文件操作等）
- [ ] SQLite 持久化正常

## 技术支持

如有问题，请检查：

1. **启动日志**: 查看 stderr 输出，确认 streaming 和 sqlite 配置
2. **依赖版本**: 确认 langchain-core 版本为 `>=0.3.78,<1.0.0`
3. **Python 版本**: 确认使用 Python 3.8+ (推荐 3.12)

## 文件变更统计

```
修改的文件:
 app.py                            | 101 +++++++++++++++-----------------------
 tele_bot/agent.py                 |  28 ++---------
 tele_bot/agents/__init__.py       |   5 +-
 tele_bot/agents/react_executor.py |  13 ++---
 tele_bot/config/runtime.py        |  20 ++------
 tele_bot/tools/lc_adapters.py     |  10 +++-
 tests/test_executor.py            |  71 +++++----------------------

删除: 172 行
新增: 76 行
净减少: 96 行
```
