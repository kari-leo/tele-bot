"""Regression coverage for long documents read through the model-facing tool."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.tools.file_system import FileSystemTool
from tele_bot.tools.lc_adapters import _truncate_json, build_spike_tools


class FileReadingTests(unittest.TestCase):
    def test_long_documents_can_be_read_without_missing_or_repeating_lines(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            reader = next(t for t in build_spike_tools(filesystem_tool=fs)
                          if t.name == "filesystem_read_file")
            for count in (560, 765, 641):
                expected = [f"第 {i} 行：" + "正文内容" * 18 for i in range(count)]
                path = root / f"文档-{count}.md"
                path.write_text("\n".join(expected), encoding="utf-8")
                actual = []
                start = 1
                for _ in range(30):
                    encoded = reader.invoke({"path": str(path), "max_lines": 800,
                                             "start_line": start})
                    self.assertLessEqual(len(encoded), 8000)
                    page = json.loads(encoded)
                    self.assertEqual(page["start_line"], start)
                    self.assertEqual(page["line_count"], count)
                    self.assertEqual([line["line"] for line in page["lines"]],
                                     list(range(start, start + len(page["lines"]))))
                    actual.extend(line["content"] for line in page["lines"])
                    if page["next_start_line"] is None:
                        self.assertFalse(page["truncated"])
                        break
                    self.assertTrue(page["truncated"])
                    self.assertGreater(page["next_start_line"], start)
                    start = page["next_start_line"]
                else:
                    self.fail("reader did not reach the end of the document")
                self.assertEqual(actual, expected)

    def test_single_oversized_line_is_preserved_and_cursor_advances(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "long-line.md"
            path.write_text("长" * 9000 + "\nlast", encoding="utf-8")
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            reader = next(t for t in build_spike_tools(filesystem_tool=fs)
                          if t.name == "filesystem_read_file")
            page = json.loads(reader.invoke({"path": str(path)}))
            self.assertEqual(page["lines"], [{"line": 1, "content": "长" * 9000}])
            self.assertEqual(page["next_start_line"], 2)
            final = json.loads(reader.invoke({"path": str(path), "start_line": 2}))
            self.assertEqual(final["lines"], [{"line": 2, "content": "last"}])
            self.assertIsNone(final["next_start_line"])

    def test_invalid_start_line_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.md").write_text("hello", encoding="utf-8")
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            with self.assertRaises(ValueError):
                fs.read_file("note.md", start_line=0)

    def test_large_results_remain_valid_json_and_preserve_exit_status(self):
        result = {"returncode": 1, "stdout": "", "stderr": '"error\\\n' * 4000,
                  "truncated": False}
        encoded = _truncate_json(result)
        parsed = json.loads(encoded)
        self.assertLessEqual(len(encoded), 8000)
        self.assertTrue(parsed["truncated"])
        self.assertEqual(parsed["returncode"], 1)
        self.assertEqual(result["truncated"], False)


if __name__ == "__main__":
    unittest.main()
