from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


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


_installed_fake_langchain = "langchain_core.tools" not in sys.modules
if _installed_fake_langchain:
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

from tele_bot.tools.blog_publish import BlogPublishTool
from tele_bot.tools.lc_adapters import build_core_tools

if _installed_fake_langchain:
    sys.modules.pop("langchain_core.tools", None)
    sys.modules.pop("langchain_core.runnables", None)
    sys.modules.pop("langchain_core", None)


class _StubGitPush:
    def __init__(self) -> None:
        self.request_calls: list[dict[str, str | None]] = []
        self.confirm_calls: list[dict[str, str]] = []

    def request_push(self, **kwargs) -> str:
        self.request_calls.append(kwargs)
        return "push-requested"

    def confirm_push(self, **kwargs) -> str:
        self.confirm_calls.append(kwargs)
        return "push-confirmed"


class BlogPublishGitPushTests(unittest.TestCase):
    def test_publish_can_request_and_confirm_push(self) -> None:
        with TemporaryDirectory() as temp_dir:
            git_push = _StubGitPush()
            posts_dir = Path(temp_dir) / "posts"
            posts_dir.mkdir()
            publisher = BlogPublishTool(
                posts_dir, git_push_tool=git_push  # type: ignore[arg-type]
            )
            content = "---\ntitle: Demo\npublished: 2026-08-10\ndescription: Test\n---\n# Demo"

            path = publisher.publish("demo", content)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(
                publisher.request_push(chat_id="chat-1", file_path=path),
                "push-requested",
            )
            self.assertEqual(git_push.request_calls[0]["file_path"], path)
            self.assertEqual(
                publisher.confirm_push(chat_id="chat-1", confirm_token="ABC123"),
                "push-confirmed",
            )

    def test_adapter_confirmation_does_not_publish_again(self) -> None:
        class StubPublisher:
            posts_dir = Path("posts")

            def __init__(self) -> None:
                self.publish_calls = 0

            def publish(self, *, slug: str, content: str) -> str:
                self.publish_calls += 1
                return "posts/demo.md"

            def request_push(self, **_kwargs) -> str:
                return "push-requested"

            def confirm_push(self, **_kwargs) -> str:
                return "push-confirmed"

        publisher = StubPublisher()
        with patch("tele_bot.tools.lc_adapters.GitPushTool"):
            tools = build_core_tools(
                blog_publish_tool=publisher, include_blog_publish=True  # type: ignore[arg-type]
            )
        blog_tool = next(tool for tool in tools if tool.name == "blog_publish")

        result = blog_tool.invoke(
            {
                "slug": "ignored-on-confirm",
                "content": "ignored",
                "git_push_enabled": True,
                "push_confirm_token": "ABC123",
            },
            config={"configurable": {"chat_id": "chat-2"}},
        )

        self.assertEqual(result, "push-confirmed")
        self.assertEqual(publisher.publish_calls, 0)


if __name__ == "__main__":
    unittest.main()