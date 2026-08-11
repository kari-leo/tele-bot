import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tele_bot.tools.codex_runner import CodexRunner
from tele_bot.tools.workspace_policy import WorkspacePolicy


class CodexRunnerTests(unittest.TestCase):
    def test_from_env_reads_ignored_local_env_as_fallback(self) -> None:
        with patch(
            "tele_bot.tools.codex_runner._local_env_values",
            return_value={"TELE_BOT_CODEX_COMMAND": "codex.cmd", "TELE_BOT_CODEX_TIMEOUT": "42"},
        ), patch.dict(os.environ, {}, clear=True):
            runner = CodexRunner.from_env()

        self.assertEqual(runner.command, "codex.cmd")
        self.assertEqual(runner.timeout_seconds, 42)

    def test_from_env_reads_codex_workspace_from_local_env(self) -> None:
        base_policy = WorkspacePolicy(
            allowed_roots=(Path("D:/"),),
            workspace_root=Path("D:/files_data/windborne/tele_bot"),
            quarantine_root=Path("D:/files_data/windborne/tele_bot-quarantine"),
        )
        with patch(
            "tele_bot.tools.codex_runner._local_env_values",
            return_value={
                "TELE_BOT_CODEX_WORKSPACE_ROOT": "D:/files_data/Job/Job_workspace"
            },
        ), patch("tele_bot.tools.codex_runner.WorkspacePolicy.from_env", return_value=base_policy):
            runner = CodexRunner.from_env()

        self.assertEqual(
            runner.policy.workspace_root,
            Path("D:/files_data/Job/Job_workspace").resolve(),
        )

    def test_from_env_defaults_codex_workspace_to_files_data(self) -> None:
        with patch(
            "tele_bot.tools.codex_runner._local_env_values", return_value={}
        ), patch.dict(os.environ, {}, clear=True):
            runner = CodexRunner.from_env()

        self.assertEqual(runner.default_workspace, Path("D:/files_data"))

    def test_skip_git_repo_check_is_configurable(self) -> None:
        with patch(
            "tele_bot.tools.codex_runner._local_env_values",
            return_value={"TELE_BOT_CODEX_SKIP_GIT_REPO_CHECK": "true"},
        ), patch.dict(os.environ, {}, clear=True):
            runner = CodexRunner.from_env()

        self.assertTrue(runner.skip_git_repo_check)
        self.assertIn(
            "--skip-git-repo-check",
            runner._command_args("plan", "检查"),
        )

    def test_skip_git_repo_check_defaults_to_true(self) -> None:
        with patch(
            "tele_bot.tools.codex_runner._local_env_values", return_value={}
        ), patch.dict(os.environ, {}, clear=True):
            runner = CodexRunner.from_env()

        self.assertTrue(runner.skip_git_repo_check)

    def test_windows_prompt_requires_explicit_utf8_file_encoding(self) -> None:
        prompt = CodexRunner._prepare_prompt("读取 skill")

        if os.name == "nt":
            self.assertIn("Get-Content", prompt)
            self.assertIn("-Encoding UTF8", prompt)
            self.assertIn("OutputEncoding", prompt)
        else:
            self.assertEqual(prompt, "读取 skill")

    def test_windows_environment_enables_utf8(self) -> None:
        environment = CodexRunner._safe_environment()

        if os.name == "nt":
            self.assertEqual(environment["PYTHONUTF8"], "1")
            self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")

    def test_runner_uses_fixed_workspace_and_no_shell(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            quarantine = Path(temp_dir) / "quarantine"
            root.mkdir()
            policy = WorkspacePolicy(
                allowed_roots=(root, quarantine),
                workspace_root=root,
                quarantine_root=quarantine,
            )
            runner = CodexRunner(policy=policy, command="codex")
            completed = type("Completed", (), {
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
            })()
            with patch("tele_bot.tools.codex_runner.subprocess.run", return_value=completed) as run:
                result = runner.run("inspect", "检查文件")

            self.assertEqual(result.returncode, 0)
            self.assertEqual(run.call_args.kwargs["cwd"], str(root))
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertEqual(run.call_args.args[0][:2], ["codex", "exec"])
            self.assertIn("--sandbox", run.call_args.args[0])

    def test_runner_rejects_workspace_outside_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            quarantine = Path(temp_dir) / "quarantine"
            outside = Path(temp_dir) / "outside"
            root.mkdir()
            outside.mkdir()
            policy = WorkspacePolicy(
                allowed_roots=(root, quarantine, outside),
                workspace_root=root,
                quarantine_root=quarantine,
            )
            runner = CodexRunner(policy=policy)
            with self.assertRaisesRegex(ValueError, "inside"):
                runner.run("inspect", "检查", workspace=str(outside))


if __name__ == "__main__":
    unittest.main()
