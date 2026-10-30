"""The scheduled executor exposes only approved tools and file roots."""

import json
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
    def test_attachment_validation_failure_preserves_summary_and_names_delivery_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"content": "核对代码进度并交付报告", "planned_at": "2026-10-29T19:00:00+00:00",
                       "scope": {"tools": ["codex_apply"], "directories": [directory]}}
            receipt = {"status": "success", "result": "已完成代码进度核对", "paths": [str(Path(directory) / "missing.md")]}
            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(receipt), stderr="")
                result = execution.execute(payload)
            self.assertEqual(result["status"], "failed")
            self.assertIn("交付未完成", result["result"])
            self.assertIn("已完成代码进度核对", result["result"])
            self.assertNotIn('"paths":', result["result"])
            self.assertEqual(result["paths"], [])

    def test_old_resume_task_without_scoped_id_does_not_launch_fresh_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"content": "调用 Codex 继续评估项目开发进度这个对话", "planned_at": "2026-10-29T19:00:00+00:00",
                       "scope": {"tools": ["codex_apply"], "directories": [directory]}}
            with patch.object(execution.CodexRunner, "from_env") as factory:
                result = execution.execute(payload)
            factory.assert_not_called()
            self.assertEqual(result["status"], "failed")
            self.assertIn("未新建会话", result["result"])

    def test_scheduled_codex_thread_id_uses_resume_and_keeps_current_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.md"
            thread_id = "01a187a9-24cf-7e72-ad49-93b6c99817dc"
            payload = {"content": "用 Codex 继续指定会话核对进度", "planned_at": "2026-10-29T19:00:00+00:00",
                       "scope": {"tools": ["codex_apply"], "directories": [directory], "codex_thread_id": thread_id}}

            def resume(*args, **kwargs):
                report.write_text("本轮进度", encoding="utf-8")
                return json.dumps({"status": "success", "result": "已续聊", "paths": [str(report)]})

            with patch.object(execution.CodexRunner, "from_env") as factory, \
                    patch("tele_bot.scheduled.execution.CodexHistoryClient") as history:
                history.return_value.resume.side_effect = resume
                result = execution.execute(payload)
            factory.return_value.run.assert_not_called()
            call = history.return_value.resume.call_args
            self.assertEqual(call.args[0], thread_id)
            self.assertEqual(call.kwargs["workspace"], str(Path(directory).resolve()))
            self.assertEqual(call.kwargs["output_schema"], execution.OUTPUT_SCHEMA)
            self.assertEqual(result, {"status": "success", "result": "已续聊", "paths": [str(report)]})

    def test_code_task_delivers_current_json_evidence_with_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "tracking_outputs" / "report.md"
            evidence = root / "tracking_outputs" / "evidence.json"
            payload = {"content": "核对代码并输出进度和测试证据", "planned_at": "2026-10-29T19:00:00+00:00",
                       "scope": {"tools": ["codex_apply"], "directories": [directory]}}

            def run(*args, **kwargs):
                report.parent.mkdir()
                report.write_text("本轮进度", encoding="utf-8")
                evidence.write_text('{"tests": 30}', encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "status": "success", "result": "核对完成",
                    "paths": [str(report), str(evidence)],
                }), stderr="")

            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.side_effect = run
                result = execution.execute(payload)
            self.assertEqual(result["status"], "success", result["result"])
            self.assertEqual(result["paths"], [str(report), str(evidence)])

    def test_table_request_without_current_attachment_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"content": "查询岗位，将新增和修改总结成表格发给我", "planned_at": "2026-10-05T18:30:00+00:00",
                       "scope": {"tools": ["codex_apply"], "directories": [directory]}}
            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.return_value = SimpleNamespace(
                    returncode=0, stdout=json.dumps({"status": "success", "result": "已查询岗位", "paths": []}), stderr="")
                result = execution.execute(payload)
            self.assertEqual(result["status"], "failed")
            self.assertIn("未生成并确认本轮表格附件", result["result"])

    def test_partial_failure_keeps_completed_current_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "partial.csv"
            payload = {"content": "生成表格", "planned_at": "2026-10-05T18:30:00+00:00",
                       "scope": {"tools": ["codex_apply"], "directories": [directory]}}
            def run(*args, **kwargs):
                report.write_text("部分结果", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "failed", "result": "一个来源失败", "paths": [str(report)]}), stderr="")
            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.side_effect = run
                result = execution.execute(payload)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["paths"], [str(report)])

    def test_codex_artifacts_are_validated_and_skill_state_is_writable_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / ".codex" / "skills" / "autumn-recruitment-tracker"
            (source / "state").mkdir(parents=True)
            (source / "SKILL.md").write_text("skill", encoding="utf-8")
            (source / "state" / "seen_postings.json").write_text('{"postings": {}}', encoding="utf-8")
            (source / "config.json").write_text(json.dumps({"excel_output_path": "../../../tracking_outputs/tracker.xlsx"}), encoding="utf-8")
            report = root / "tracking_outputs" / "changes.xlsx"
            report.parent.mkdir()
            payload = {"content": "用 autumn-recruitment-tracker skill 更新产品经理岗位，将表格文件发给我",
                       "planned_at": "2026-10-05T18:30:00+00:00", "run_id": "r1",
                       "scope": {"tools": ["codex_apply"], "directories": [str(root)]}}

            def fake_run(mode, prompt, workspace, **kwargs):
                self.assertIn(payload["content"], prompt)
                self.assertIn("用户原始任务优先", prompt)
                runtime = root / "telebot_runtime" / "skills" / source.name
                self.assertIn(str(runtime / "SKILL.md"), prompt)
                self.assertTrue((runtime / "state" / "seen_postings.json").is_file())
                config = json.loads((runtime / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(Path(config["excel_output_path"]), root / "tracking_outputs" / "tracker.xlsx")
                (runtime / "state" / "seen_postings.json").write_text('{"postings": {"new": 1}}', encoding="utf-8")
                report.write_bytes(b"current report")
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "success", "result": "执行结论：成功", "paths": [str(report)]}), stderr="", truncated=False)

            with patch.object(execution.CodexRunner, "from_env") as factory:
                factory.return_value.run.side_effect = fake_run
                result = execution.execute(payload)
            self.assertEqual(result["paths"], [str(report)])
            self.assertEqual(result["status"], "success")
            self.assertEqual(json.loads((source / "state" / "seen_postings.json").read_text()), {"postings": {}})
            from tele_bot.scheduled.codex_artifacts import prepare_skill_runtime
            runtime = prepare_skill_runtime(root, source.name)
            self.assertEqual(json.loads((runtime / "state" / "seen_postings.json").read_text()), {"postings": {"new": 1}})

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

    def test_web_tools_follow_authorized_scope_and_replace_legacy_search(self):
        from unittest.mock import Mock
        from tele_bot.tools.bailian_web import build_web_tools

        web = Mock()
        web.execute.return_value = {"content": "searched", "engine": "bailian"}
        web.read_page.return_value = {"content": "read", "engine": "bailian"}
        for allowed, expected in [(["web_search"], "web_search"),
                                  (["web_extractor"], "web_extractor"),
                                  (["opencli_search"], "web_search")]:
            with self.subTest(allowed=allowed):
                def fake_graph(llm, tools, **kwargs):
                    self.assertEqual([item.name for item in tools], [expected])
                    args = {"query": "news"} if expected == "web_search" else {"url": "https://example.com"}
                    receipt = tools[0].invoke(args)
                    return SimpleNamespace(invoke=lambda *args, **kwargs: {"messages": [
                        ToolMessage(content=receipt, name=expected, tool_call_id="call1"),
                        AIMessage(content="执行结论：成功")]})

                payload = {"run_id": "r1", "task_id": "t1", "owner_id": "alice",
                           "chat_id": "chat", "content": "查询信息", "planned_at": "2026-10-21T01:00:00+00:00",
                           "scope": {"tools": allowed, "directories": []}}
                with patch.object(execution.AliBailianSettings, "from_env", return_value=SimpleNamespace(
                    api_key="test", base_url="https://example.com", model="fake", timeout_seconds=10)), \
                     patch.object(execution, "build_web_tools", side_effect=lambda: build_web_tools(web)), \
                     patch.object(execution, "build_chat_openai", return_value=FakeLLM()), \
                     patch.object(execution, "build_context_manager", return_value=None), \
                     patch.object(execution, "build_react_graph", side_effect=fake_graph):
                    self.assertEqual(execution.execute(payload)["status"], "success")

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
                 patch.object(execution, "build_context_manager", return_value=None), \
                 patch.object(execution, "build_react_graph", side_effect=fake_graph):
                result = execution.execute(payload)
            self.assertIn("done", result["result"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(result["paths"]), 1)


if __name__ == "__main__":
    unittest.main()
