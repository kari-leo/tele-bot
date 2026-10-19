from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.tools.git_push import GitPushError, GitPushTool


class GitPushToolTests(unittest.TestCase):
    def test_three_pending_tokens_in_one_chat_remain_independently_valid(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            files = [root / f"doc-{i}.md" for i in range(3)]
            for path in files:
                path.write_text("body", encoding="utf-8")
            tool = GitPushTool(repo_root=root)
            calls = []

            def run(args, allow_nonzero=False):
                calls.append(args)
                return (0, "main\n" if args[:2] == ["rev-parse", "--abbrev-ref"]
                        else "true\n" if args[:2] == ["rev-parse", "--is-inside-work-tree"] else "", "")

            tool._run_git = run
            tokens = [re.search(r"push_confirm_token=([A-Z0-9]+)", tool.request_push(
                chat_id="chat", file_path=str(path))).group(1) for path in files]
            for token in tokens:
                self.assertIn("git push 已完成", tool.confirm_push(chat_id="chat", confirm_token=token))
            self.assertEqual(sum(args[0] == "push" for args in calls), 3)

    def test_retry_after_push_failure_does_not_commit_again(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            path = root / "doc.md"
            path.write_text("body", encoding="utf-8")
            tool = GitPushTool(repo_root=root)
            calls = []

            def run(args, allow_nonzero=False):
                calls.append(args)
                if args[0] == "diff":
                    return 1, "", ""
                if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                    return 0, "true\n", ""
                if args[:2] == ["rev-parse", "--abbrev-ref"]:
                    return 0, "main\n", ""
                if args[0] == "commit" and sum(call[0] == "commit" for call in calls) > 1:
                    return 1, "nothing to commit, working tree clean\n", ""
                if args[0] == "push" and sum(call[0] == "push" for call in calls) == 1:
                    raise GitPushError("connection reset")
                return 0, "", ""

            tool._run_git = run
            token = re.search(r"push_confirm_token=([A-Z0-9]+)", tool.request_push(
                chat_id="chat", file_path=str(path))).group(1)
            with self.assertRaisesRegex(GitPushError, "connection reset"):
                tool.confirm_push(chat_id="chat", confirm_token=token)
            self.assertIn("git push 已完成", tool.confirm_push(chat_id="chat", confirm_token=token))
            self.assertEqual(sum(args[0] == "commit" for args in calls), 1)

    def test_request_then_confirm_push(self) -> None:
        with TemporaryDirectory() as td:
            repo = Path(td)
            reports = repo / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            note = reports / "demo.md"
            note.write_text("# demo\n", encoding="utf-8")

            tool = GitPushTool(repo_root=repo)
            calls: list[list[str]] = []

            def fake_run_git(args: list[str], allow_nonzero: bool = False):
                calls.append(args)
                if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                    return 0, "true\n", ""
                if args[:2] == ["rev-parse", "--abbrev-ref"]:
                    return 0, "main\n", ""
                if args[0] == "commit":
                    return 0, "[main abc123] docs\n", ""
                if args[0] == "push":
                    return 0, "pushed\n", ""
                return 0, "", ""

            tool._run_git = fake_run_git  # type: ignore[method-assign]

            request = tool.request_push(chat_id="c1", file_path=str(note))
            match = re.search(r"push_confirm_token=([A-Z0-9]+)", request)
            self.assertIsNotNone(match)
            token = match.group(1) if match else ""

            result = tool.confirm_push(chat_id="c1", confirm_token=token)
            self.assertIn("git push 已完成", result)

            self.assertIn(["add", "--", "reports/demo.md"], calls)
            self.assertIn(["push", "origin", "main"], calls)

    def test_reject_invalid_token(self) -> None:
        with TemporaryDirectory() as td:
            repo = Path(td)
            reports = repo / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            note = reports / "demo.md"
            note.write_text("# demo\n", encoding="utf-8")

            tool = GitPushTool(repo_root=repo)

            def fake_run_git(args: list[str], allow_nonzero: bool = False):
                if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                    return 0, "true\n", ""
                if args[:2] == ["rev-parse", "--abbrev-ref"]:
                    return 0, "main\n", ""
                return 0, "", ""

            tool._run_git = fake_run_git  # type: ignore[method-assign]

            tool.request_push(chat_id="c2", file_path=str(note))
            with self.assertRaises(GitPushError):
                tool.confirm_push(chat_id="c2", confirm_token="WRONG")


if __name__ == "__main__":
    unittest.main()
