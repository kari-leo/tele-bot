import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tele_bot.config.paths import configure_runtime_root, default_allowed_roots, runtime_root
from tele_bot.tools.file_system import FileSystemTool
from tele_bot.tools.shell_sandbox import ShellSandboxTool


class RuntimePathTests(unittest.TestCase):
    def test_configure_runtime_root_uses_script_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "launcher.py"
            script.write_text("print('ok')", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                root = configure_runtime_root(script)

                self.assertEqual(root, Path(temp_dir).resolve())
                self.assertEqual(runtime_root(), Path(temp_dir).resolve())

    def test_default_allowed_roots_use_runtime_drive_on_windows(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            with patch.dict(os.environ, {"TELE_BOT_RUNTIME_ROOT": str(root)}):
                allowed_roots = default_allowed_roots()

            if os.name == "nt":
                self.assertEqual(allowed_roots, (Path(f"{root.drive}\\"),))
            else:
                self.assertEqual(allowed_roots, (root, Path("/tmp")))

    def test_filesystem_relative_paths_are_anchored_to_runtime_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            note = root / "note.txt"
            note.write_text("hello\n", encoding="utf-8")
            tool = FileSystemTool(root=root, allowed_roots=(root,))

            result = tool.read_file("note.txt")

            self.assertEqual(result["path"], str(note))
            self.assertEqual(result["lines"][0]["content"], "hello")

    def test_shell_runs_from_runtime_root_by_default(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            note = root / "note.txt"
            note.write_text("hello\n", encoding="utf-8")
            tool = ShellSandboxTool(root=root, allowed_roots=(root,))

            result = tool.execute_shell("ls")

            self.assertEqual(result["returncode"], 0)
            self.assertIn("note.txt", result["stdout"])


if __name__ == "__main__":
    unittest.main()
