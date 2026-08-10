import unittest

from tele_bot.models import IncomingMessage
from tele_bot.router.codex_service import CodexCommandService
from tele_bot.tools.codex_runner import CodexResult


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
        self.assertEqual(self.runner.calls[1], (
            "apply",
            r"在 D:\files_data\Job\Job_workspace 更新岗位",
            r"D:\files_data\Job\Job_workspace",
        ))

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