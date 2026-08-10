import unittest

from tele_bot.router.codex_commands import parse_codex_command


class CodexCommandTests(unittest.TestCase):
    def test_natural_language_does_not_trigger_codex(self) -> None:
        self.assertIsNone(parse_codex_command("请使用 codex 分析项目"))

    def test_explicit_inspect_command_is_parsed(self) -> None:
        command = parse_codex_command("/codex inspect 分析当前目录")
        self.assertIsNotNone(command)
        self.assertEqual(command.mode, "inspect")
        self.assertEqual(command.prompt, "分析当前目录")

    def test_unknown_subcommand_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "用法"):
            parse_codex_command("/codex run 修改文件")

    def test_plan_extracts_windows_workspace(self) -> None:
        command = parse_codex_command(
            r"/codex plan 在 D:\files_data\Job\Job_workspace 使用 tracker skill"
        )
        self.assertEqual(command.workspace, r"D:\files_data\Job\Job_workspace")

    def test_apply_direct_prompt_extracts_windows_workspace(self) -> None:
        command = parse_codex_command(
            r"/codex apply 在 D:\files_data\Job\Job_workspace 修改 README"
        )
        self.assertEqual(command.workspace, r"D:\files_data\Job\Job_workspace")


if __name__ == "__main__":
    unittest.main()
