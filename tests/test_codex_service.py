import unittest
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.models import IncomingMessage
from tele_bot.router.codex_service import CodexCommandService
from tele_bot.tools.codex_runner import CodexResult
from tele_bot.tools.workspace_policy import WorkspacePolicy
from tele_bot.tools.workspace_resolver import WorkspaceResolver


class FakeCodexRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(self, mode: str, prompt: str, workspace: str | None = None) -> CodexResult:
        self.calls.append((mode, prompt, workspace))
        return CodexResult(
            mode=mode,
            workspace=workspace or "D:/workspace",
            returncode=0,
            stdout="ok",
            stderr="",
            truncated=False,
        )


class FailedCodexRunner(FakeCodexRunner):
    def run(self, mode: str, prompt: str, workspace: str | None = None) -> CodexResult:
        self.calls.append((mode, prompt, workspace))
        return CodexResult(
            mode=mode,
            workspace=workspace or "D:/workspace",
            returncode=1,
            stdout="",
            stderr="失败详情",
            truncated=False,
        )


class CodexCommandServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeCodexRunner()
        self.service = CodexCommandService(self.runner)

    def message(self, text: str, chat_id: str = "chat-1") -> IncomingMessage:
        return IncomingMessage(
            channel="feishu",
            user_id="user-1",
            chat_id=chat_id,
            text=text,
        )

    def test_plan_returns_confirmation_and_apply_reuses_plan(self) -> None:
        plan_reply = self.service.handle(
            self.message(r"/codex plan 在 D:\files_data\Job\Job_workspace 更新岗位")
        )

        self.assertIn("确认执行请发送：/codex apply ", plan_reply)
        self.assertEqual(self.runner.calls[0][0], "plan")
        token = plan_reply.rsplit(" ", 1)[-1]

        apply_reply = self.service.handle(self.message(f"/codex apply {token}"))

        self.assertIn("Codex apply 完成", apply_reply)
        self.assertEqual(self.runner.calls[1][0], "apply")
        self.assertIn("请执行已确认的修改计划", self.runner.calls[1][1])
        self.assertIn("只读 plan 输出", self.runner.calls[1][1])
        self.assertEqual(self.runner.calls[1][2], r"D:\files_data\Job\Job_workspace")

    def test_apply_token_is_bound_to_chat_and_single_use(self) -> None:
        plan_reply = self.service.handle(self.message("/codex plan 检查"))
        token = plan_reply.rsplit(" ", 1)[-1]

        wrong_chat = self.service.handle(self.message(f"/codex apply {token}", "chat-2"))
        first_use = self.service.handle(self.message(f"/codex apply {token}"))
        reused = self.service.handle(self.message(f"/codex apply {token}"))

        self.assertIn("令牌无效", wrong_chat)
        self.assertIn("Codex apply 完成", first_use)
        self.assertIn("令牌无效", reused)
        self.assertEqual(len(self.runner.calls), 2)

    def test_apply_can_run_direct_prompt_without_plan_token(self) -> None:
        reply = self.service.handle(
            self.message(r"/codex apply 在 D:\files_data\Job\Job_workspace 修改 README")
        )

        self.assertIn("Codex apply 完成", reply)
        self.assertEqual(
            self.runner.calls,
            [("apply", r"在 D:\files_data\Job\Job_workspace 修改 README", r"D:\files_data\Job\Job_workspace")],
        )

    def test_apply_request_previews_without_running_and_confirm_runs_once(self) -> None:
        reply = self.service.create_apply_request(
            chat_id="chat-1",
            task=r"在 D:\files_data\Job\Job_workspace 删除 旺* 文件",
            workspace_hint=r"D:\files_data\Job\Job_workspace",
        )

        self.assertIn('"sandbox": "workspace-write"', reply)
        self.assertIn('"operation": "delete"', reply)
        self.assertEqual(self.runner.calls, [])

        confirmed = self.service.handle_confirmation(self.message("确认"))

        self.assertIn("Codex apply 完成", confirmed)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIsNone(self.service.handle_confirmation(self.message("确认")))

    def test_apply_request_token_is_bound_to_chat(self) -> None:
        reply = self.service.create_apply_request(
            chat_id="chat-1",
            task="修改 README",
            workspace_hint=r"D:\files_data\Job\Job_workspace",
        )
        token = re.search(r"/codex apply ([A-Za-z0-9_-]+)", reply).group(1)

        wrong_chat = self.service.handle(self.message(f"/codex apply {token}", "chat-2"))
        valid = self.service.handle(self.message(f"/codex apply {token}"))

        self.assertIn("令牌无效", wrong_chat)
        self.assertIn("Codex apply 完成", valid)

    def test_follow_up_inspect_reuses_workspace_and_prior_codex_result(self) -> None:
        plan_reply = self.service.handle(
            self.message(r"/codex plan 在 D:\files_data\Job\Job_workspace 更新岗位偏好")
        )
        token = plan_reply.rsplit(" ", 1)[-1]
        self.service.handle(self.message(f"/codex apply {token}"))

        self.service.handle(self.message("/codex inspect 把刚才的修改点逐项列出来"))

        inspect_prompt = self.runner.calls[2][1]
        self.assertIn("这是同一个飞书会话中的后续 Codex 请求", inspect_prompt)
        self.assertIn("把刚才的修改点逐项列出来", inspect_prompt)
        self.assertIn("更新岗位偏好", inspect_prompt)
        self.assertEqual(self.runner.calls[2][2], r"D:\files_data\Job\Job_workspace")

    def test_fuzzy_workspace_requires_selection_when_candidates_are_ambiguous(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "files_data"
            quarantine = Path(temp_dir) / "quarantine"
            first = root / "Job_alpha"
            second = root / "Job_beta"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "README.md").write_text("alpha", encoding="utf-8")
            (second / "README.md").write_text("beta", encoding="utf-8")
            policy = WorkspacePolicy(
                allowed_roots=(root, quarantine),
                workspace_root=root,
                quarantine_root=quarantine,
            )
            runner = FakeCodexRunner()
            service = CodexCommandService(runner, WorkspaceResolver(policy))

            reply = service.handle(self.message("/codex apply 在 Job 项目中修改 README"))

        self.assertIn("多个可能的工作区", reply)
        self.assertIn("Job_alpha", reply)
        self.assertIn("Job_beta", reply)
        self.assertEqual(runner.calls, [])

    def test_fuzzy_workspace_selects_unique_candidate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "files_data"
            quarantine = Path(temp_dir) / "quarantine"
            target = root / "Job_workspace"
            target.mkdir(parents=True)
            (target / "README.md").write_text("target", encoding="utf-8")
            policy = WorkspacePolicy(
                allowed_roots=(root, quarantine),
                workspace_root=root,
                quarantine_root=quarantine,
            )
            runner = FakeCodexRunner()
            service = CodexCommandService(runner, WorkspaceResolver(policy, search_roots=(root,)))

            reply = service.handle(self.message("/codex apply 在 Job 项目中修改 README"))

        self.assertIn("Codex apply 完成", reply)
        self.assertEqual(runner.calls[0][2], str(target.resolve()))

    def test_fuzzy_workspace_supports_chinese_directory_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "files_data"
            quarantine = Path(temp_dir) / "quarantine"
            target = root / "奖学金"
            target.mkdir(parents=True)
            (target / "README.md").write_text("target", encoding="utf-8")
            policy = WorkspacePolicy(
                allowed_roots=(root, quarantine),
                workspace_root=root,
                quarantine_root=quarantine,
            )
            runner = FakeCodexRunner()
            service = CodexCommandService(runner, WorkspaceResolver(policy, search_roots=(root,)))

            reply = service.create_apply_request(
                chat_id="chat-1",
                task="删除旺*文件",
                workspace_hint="filesdata目录下的奖学金目录",
            )

        self.assertIn("奖学金", reply)
        self.assertEqual(runner.calls, [])

    def test_non_codex_message_is_ignored(self) -> None:
        self.assertIsNone(self.service.handle(self.message("请帮我检查岗位")))
        self.assertEqual(self.runner.calls, [])

    def test_success_does_not_forward_codex_stderr(self) -> None:
        self.runner = FakeCodexRunner()
        self.service = CodexCommandService(self.runner)
        self.runner.run = lambda mode, prompt, workspace=None: CodexResult(
            mode=mode,
            workspace=workspace or "D:/workspace",
            returncode=0,
            stdout="最终结果",
            stderr="Codex 内部执行日志",
            truncated=False,
        )

        reply = self.service.handle(self.message("/codex inspect 检查"))

        self.assertIn("最终结果", reply)
        self.assertNotIn("错误输出", reply)
        self.assertNotIn("内部执行日志", reply)

    def test_failure_forwards_error_details(self) -> None:
        service = CodexCommandService(FailedCodexRunner())

        reply = service.handle(self.message("/codex inspect 检查"))

        self.assertIn("执行失败", reply)
        self.assertIn("错误输出", reply)
        self.assertIn("失败详情", reply)


if __name__ == "__main__":
    unittest.main()