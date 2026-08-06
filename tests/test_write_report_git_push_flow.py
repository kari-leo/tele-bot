from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import types


class _FakeTool:
    def __init__(self, func):
        self._func = func
        self.name = func.__name__

    def invoke(self, payload: dict, config: dict | None = None):
        kwargs = dict(payload)
        if "config" in self._func.__code__.co_varnames:
            kwargs["config"] = config
        return self._func(**kwargs)


def _fake_tool(func=None, **_kwargs):
    if func is None:
        def wrapper(inner):
            return _FakeTool(inner)

        return wrapper
    return _FakeTool(func)


if "langchain_core.tools" not in sys.modules:
    langchain_core = types.ModuleType("langchain_core")
    tools_mod = types.ModuleType("langchain_core.tools")
    tools_mod.tool = _fake_tool
    runnables_mod = types.ModuleType("langchain_core.runnables")
    runnables_mod.RunnableConfig = dict
    langchain_core.tools = tools_mod
    langchain_core.runnables = runnables_mod
    sys.modules["langchain_core"] = langchain_core
    sys.modules["langchain_core.tools"] = tools_mod
    sys.modules["langchain_core.runnables"] = runnables_mod

from tele_bot.tools.lc_adapters import build_core_tools


class _StubWriter:
    def __init__(self, out_path: str) -> None:
        self.out_path = out_path
        self.calls = 0

    def write(self, content: str, title: str | None = None, filename: str | None = None) -> str:
        self.calls += 1
        return self.out_path


class _StubGitPushTool:
    instances: list["_StubGitPushTool"] = []

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.request_calls: list[dict[str, str | None]] = []
        self.confirm_calls: list[dict[str, str]] = []
        _StubGitPushTool.instances.append(self)

    def request_push(
        self,
        *,
        chat_id: str,
        file_path: str,
        commit_message: str | None = None,
        remote: str | None = None,
        branch: str | None = None,
    ) -> str:
        self.request_calls.append(
            {
                "chat_id": chat_id,
                "file_path": file_path,
                "commit_message": commit_message,
                "remote": remote,
                "branch": branch,
            }
        )
        return "request-ok"

    def confirm_push(self, *, chat_id: str, confirm_token: str) -> str:
        self.confirm_calls.append({"chat_id": chat_id, "confirm_token": confirm_token})
        return "confirm-ok"


class WriteReportGitPushFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        _StubGitPushTool.instances.clear()

    def _get_write_report_tool(self, writer: _StubWriter):
        with patch("tele_bot.tools.lc_adapters.GitPushTool", _StubGitPushTool):
            tools = build_core_tools(write_report_tool=writer)
        write_tools = [tool for tool in tools if tool.name == "write_report"]
        self.assertEqual(len(write_tools), 1)
        return write_tools[0]

    def test_confirm_push_does_not_write_report_again(self) -> None:
        with TemporaryDirectory() as temp_dir:
            out_path = str(Path(temp_dir) / "reports" / "demo.md")
            writer = _StubWriter(out_path=out_path)
            write_report_tool = self._get_write_report_tool(writer)

            result = write_report_tool.invoke(
                {
                    "content": "# ignored on confirm",
                    "git_push_enabled": True,
                    "push_confirm_token": "ABC123",
                },
                config={"configurable": {"chat_id": "chat-1"}},
            )

            self.assertEqual(result, "confirm-ok")
            self.assertEqual(writer.calls, 0)
            self.assertEqual(len(_StubGitPushTool.instances), 1)
            self.assertEqual(
                _StubGitPushTool.instances[0].confirm_calls,
                [{"chat_id": "chat-1", "confirm_token": "ABC123"}],
            )

    def test_request_push_writes_report_then_requests_push(self) -> None:
        with TemporaryDirectory() as temp_dir:
            out_path = str(Path(temp_dir) / "reports" / "demo.md")
            writer = _StubWriter(out_path=out_path)
            write_report_tool = self._get_write_report_tool(writer)

            result = write_report_tool.invoke(
                {
                    "content": "# note",
                    "title": "Demo",
                    "git_push_enabled": True,
                    "commit_message": "docs: add demo note",
                },
                config={"configurable": {"chat_id": "chat-2"}},
            )

            self.assertEqual(result, "request-ok")
            self.assertEqual(writer.calls, 1)
            self.assertEqual(len(_StubGitPushTool.instances), 1)
            self.assertEqual(
                _StubGitPushTool.instances[0].request_calls,
                [
                    {
                        "chat_id": "chat-2",
                        "file_path": out_path,
                        "commit_message": "docs: add demo note",
                        "remote": "origin",
                        "branch": None,
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
