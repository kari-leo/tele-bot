"""The scheduled executor exposes only approved tools and file roots."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from tele_bot.scheduled import execution


class FakeLLM:
    def bind_tools(self, tools):
        return self


class ExecutionScopeTests(unittest.TestCase):
    def test_misspelled_skill_is_resolved_before_codex_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("autumn-recruitment-tracker", "mock-interview"):
                skill_dir = root / ".codex" / "skills" / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
            payload = {
                "content": "在 job_workspace 目录下用 codex 调用里面的 autumn-recruiment skill，更新产品经理岗位",
                "planned_at": "2026-10-05T10:38:00+00:00",
                "scope": {"tools": ["codex_apply"], "directories": [str(root)]},
            }
            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.return_value = SimpleNamespace(
                    returncode=0, stdout="执行结论：成功\n已更新", stderr=""
                )
                outcome = execution.execute(payload)
            self.assertEqual(outcome["status"], "success")
            prompt = factory.return_value.run.call_args.args[1]
            self.assertIn("指定 skill：autumn-recruitment-tracker", prompt)
            self.assertIn(".codex/skills/autumn-recruitment-tracker/SKILL.md", prompt)
            self.assertIn("autumn-recruiment skill", prompt)

    def test_codex_skill_execution_uses_only_authorized_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / ".codex" / "skills" / "autumn-recruitment-tracker"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: autumn-recruitment-tracker\n---\n", encoding="utf-8")
            payload = {"content": "用 Codex 调用 autumn-recruitment-tracker skill 更新岗位", "planned_at": "2026-10-05T06:00:00+00:00",
                       "scope": {"tools": ["codex_apply"], "directories": [str(root)]}}
            result = SimpleNamespace(returncode=0, stdout="执行结论：成功\n| 类型 | 岗位 |\n|---|---|\n| 新增 | PM |", stderr="")
            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.return_value = result
                outcome = execution.execute(payload)
                factory.return_value.run.return_value = SimpleNamespace(
                    returncode=1, stdout="", stderr=(
                        "ERROR: Reconnecting... 5/5\n"
                        "warning: request timed out\n"
                        "ERROR: The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account."
                    ),
                )
                failure = execution.execute(payload)
            self.assertEqual(outcome["status"], "success")
            self.assertIn("新增", outcome["result"])
            self.assertEqual(factory.return_value.run.call_args.args[2], str(root.resolve()))
            self.assertEqual(failure["status"], "failed")
            self.assertIn("Codex CLI 登录会话无法使用模型 gpt-6.1-sol", failure["result"])
            self.assertNotIn("Reconnecting", failure["result"])

    def test_tool_selection_and_filesystem_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permitted = root / "allowed.txt"
            permitted.write_text("okay", encoding="utf-8")
            outside = Path(__file__).resolve()
            seen = {}

            class Graph:
                def invoke(self, state, config):
                    self.assert_tools()
                    return {"messages": [ToolMessage(content="saved", name="write_report", tool_call_id="call1"),
                                         AIMessage(content="执行结论：成功\ndone")]}

                def assert_tools(self):
                    tools = {item.name: item for item in seen["tools"]}
                    self_names = set(tools)
                    assert self_names == {"filesystem_read_file", "write_report"}
                    assert "okay" in tools["filesystem_read_file"].invoke({"path": str(permitted)})
                    with self_test.assertRaises(ValueError):
                        tools["filesystem_read_file"].invoke({"path": str(outside)})
                    report = tools["write_report"].invoke({"content": "report", "filename": "scheduled"})
                    assert Path(report).parent == root

            self_test = self

            def fake_graph(llm, tools, **kwargs):
                seen["tools"] = tools
                return Graph()

            payload = {"run_id": "r1", "task_id": "t1", "owner_id": "alice",
                       "chat_id": "chat", "content": "整理文件", "planned_at": "2026-10-03T11:00:00+00:00",
                       "scope": {"tools": ["filesystem_read_file", "write_report"],
                                 "directories": [str(root)]}}
            with patch.object(execution.AliBailianSettings, "from_env", return_value=SimpleNamespace(
                api_key="test", base_url="https://example.com", model="fake", timeout_seconds=10)), \
                 patch.object(execution, "build_chat_openai", return_value=FakeLLM()), \
                 patch.object(execution, "build_react_graph", side_effect=fake_graph):
                result = execution.execute(payload)
            self.assertIn("done", result["result"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(result["paths"]), 1)


if __name__ == "__main__":
    unittest.main()
