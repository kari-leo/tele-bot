#!/usr/bin/env python
"""
验证 Legacy 模式移除是否成功。
使用 telebot conda 环境运行：
  /d/Anaconda3/envs/telebot/python.exe verify_legacy_removal.py
"""

import sys
import os

def main():
    print("=" * 70)
    print("Legacy Mode Removal Verification")
    print("=" * 70)
    print()

    # 1. 验证 RuntimeSettings 不再需要 executor
    print("[1/5] Test RuntimeSettings...")
    try:
        from tele_bot.config import RuntimeSettings
        os.environ.setdefault('TELEGRAM_STREAMING', '1')
        os.environ.setdefault('SQLITE_CHECKPOINT_PATH', 'data/test.db')
        settings = RuntimeSettings.from_env()
        print(f"  [OK] RuntimeSettings loaded successfully")
        print(f"    - streaming: {settings.telegram_streaming}")
        print(f"    - sqlite: {settings.sqlite_checkpoint_path}")

        if hasattr(settings, 'executor'):
            print("  [FAIL] executor field still exists")
            return False
        print("  [OK] executor field removed")
    except Exception as e:
        print(f"  [FAIL] RuntimeSettings test failed: {e}")
        return False

    # 2. 验证 ReactAgentExecutor 可以导入
    print("\n[2/5] Test ReactAgentExecutor...")
    try:
        from tele_bot.agents import ReactAgentExecutor
        print("  [OK] ReactAgentExecutor imported successfully")
    except Exception as e:
        print(f"  [FAIL] ReactAgentExecutor import failed: {e}")
        return False

    # 3. 验证 AgentCore 可以导入
    print("\n[3/5] Test AgentCore...")
    try:
        from tele_bot.agent import AgentCore
        print("  [OK] AgentCore imported successfully")
    except Exception as e:
        print(f"  [FAIL] AgentCore import failed: {e}")
        return False

    # 4. 验证 ControlledAgentExecutor 不再导出
    print("\n[4/5] Test ControlledAgentExecutor removal...")
    try:
        from tele_bot.agents import ControlledAgentExecutor
        print("  [FAIL] ControlledAgentExecutor still importable")
        return False
    except ImportError:
        print("  [OK] ControlledAgentExecutor removed from exports")

    # 5. 验证 AgentExecutionResult 仍可用
    print("\n[5/5] Test AgentExecutionResult...")
    try:
        from tele_bot.agents import AgentExecutionResult
        print("  [OK] AgentExecutionResult still importable")
    except Exception as e:
        print(f"  [FAIL] AgentExecutionResult import failed: {e}")
        return False

    print()
    print("=" * 70)
    print("[SUCCESS] All validations passed! Legacy mode removed successfully.")
    print("=" * 70)
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
