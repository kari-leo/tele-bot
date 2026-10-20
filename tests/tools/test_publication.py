"""Blog publication, report delivery and push confirmation workflows."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tele_bot.tools.blog_publish import BlogPublishError, BlogPublishTool
from tele_bot.tools.file_system import FileSystemTool
from tele_bot.tools.lc_adapters import build_core_tools


class PublishFromFileTests(unittest.TestCase):
    def test_batch_publication_covers_all_three_sources_and_pushes_once(self):
        class Push:
            def __init__(self):
                self.calls = []

            def push_files(self, **kwargs):
                self.calls.append(kwargs["file_paths"])
                return "git push 已完成"

        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "content" / "posts"
            posts.mkdir(parents=True)
            (posts.parent / "projects").mkdir()
            (posts.parent / "projects" / "telebot.md").write_text(
                "project", encoding="utf-8"
            )
            documents = []
            for i in range(3):
                source = root / f"source-{i}.md"
                source.write_bytes(f"# doc {i}\r\ncomplete body {i}\n".encode())
                documents.append(
                    {"slug": f"doc-{i}", "source_path": str(source), "order": i}
                )
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            push = Push()
            tools = build_core_tools(
                filesystem_tool=fs,
                blog_publish_tool=BlogPublishTool(posts, git_push_tool=push),
                include_blog_publish=True,
            )
            batch = next(t for t in tools if t.name == "blog_publish_files")
            result = json.loads(
                batch.invoke(
                    {
                        "documents": documents,
                        "project": "telebot",
                        "git_push_enabled": True,
                    },
                    config={
                        "configurable": {
                            "chat_id": "chat",
                            "message_text": "把三个文档推出去",
                        }
                    },
                )
            )
            self.assertEqual(result["status"], "pushed")
            self.assertEqual(
                (
                    result["requested_count"],
                    result["written_count"],
                    result["pushed_count"],
                ),
                (3, 3, 3),
            )
            self.assertEqual(len(push.calls), 1)
            self.assertEqual(len(push.calls[0]), 3)
            for item in result["files"]:
                self.assertEqual(
                    Path(item["path"]).read_bytes().split(b"---\n", 2)[2],
                    Path(item["source_path"]).read_bytes(),
                )

    def test_batch_with_missing_source_does_not_push_a_partial_set(self):
        class Push:
            def push_files(self, **kwargs):
                raise AssertionError("must not push an incomplete batch")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "posts"
            posts.mkdir()
            source = root / "source.md"
            source.write_text("body", encoding="utf-8")
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            tools = build_core_tools(
                filesystem_tool=fs,
                blog_publish_tool=BlogPublishTool(posts, git_push_tool=Push()),
                include_blog_publish=True,
            )
            batch = next(t for t in tools if t.name == "blog_publish_files")
            result = json.loads(
                batch.invoke(
                    {
                        "documents": [
                            {
                                "slug": "first",
                                "source_path": str(source),
                                "description": "doc",
                            },
                            {
                                "slug": "missing",
                                "source_path": str(root / "missing.md"),
                                "description": "doc",
                            },
                        ],
                        "git_push_enabled": True,
                    },
                    config={
                        "configurable": {"chat_id": "chat", "message_text": "推送文档"}
                    },
                )
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["requested_count"], 2)
            self.assertEqual(result["pushed_count"], 0)
            self.assertEqual(list(posts.iterdir()), [])

    def test_same_body_with_a_new_slug_reuses_existing_document_across_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "content" / "posts"
            posts.mkdir(parents=True)
            (posts.parent / "projects").mkdir()
            (posts.parent / "projects" / "telebot.md").write_text(
                "project", encoding="utf-8"
            )
            source = root / "source.md"
            source.write_bytes(b"\n# source\r\ncomplete body\n")
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            original = BlogPublishTool(posts).publish_from_file(
                "case", str(source), project="telebot", filesystem_tool=fs
            )
            body = Path(original).read_bytes()
            restarted = BlogPublishTool(posts)
            retried = restarted.publish_from_file(
                "case-v2",
                str(source),
                title="another title",
                project="telebot",
                filesystem_tool=fs,
            )
            self.assertEqual(retried, original)
            self.assertEqual(Path(original).read_bytes(), body)
            self.assertEqual(
                len(list((posts.parent / "projectDocuments" / "telebot").glob("*.md"))),
                1,
            )

    def test_explicit_user_push_request_pushes_without_generating_another_token(self):
        class Push:
            def push_files(self, **kwargs):
                self.paths = kwargs["file_paths"]
                return "git push 已完成"

            def request_push(self, **kwargs):
                raise AssertionError("the user already requested publishing")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "posts"
            posts.mkdir()
            source = root / "source.md"
            source.write_text("body", encoding="utf-8")
            push = Push()
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            pub = BlogPublishTool(posts, git_push_tool=push)
            publish = next(
                t
                for t in build_core_tools(
                    filesystem_tool=fs, blog_publish_tool=pub, include_blog_publish=True
                )
                if t.name == "blog_publish_from_file"
            )
            result = publish.invoke(
                {
                    "slug": "source",
                    "source_path": str(source),
                    "description": "document",
                    "git_push_enabled": True,
                },
                config={
                    "configurable": {
                        "chat_id": "chat",
                        "message_text": "将这三个文档作为博客中 telebot 的子文档推出去",
                    }
                },
            )
            self.assertIn("git push 已完成", result)
            self.assertEqual(len(push.paths), 1)

    def test_project_document_copies_full_body_without_model_roundtrip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "content" / "posts"
            posts.mkdir(parents=True)
            projects = posts.parent / "projects"
            projects.mkdir()
            (projects / "telebot.md").write_text("project", encoding="utf-8")
            body = "\n# 原文\r\n" + "正文内容\r\n" * 800 + "最后一行"
            source = root / "产品案例.md"
            source.write_bytes(body.encode("utf-8"))
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            tools = build_core_tools(
                filesystem_tool=fs,
                blog_publish_tool=BlogPublishTool(posts),
                include_blog_publish=True,
            )
            publish = next(t for t in tools if t.name == "blog_publish_from_file")
            path = Path(
                publish.invoke(
                    {
                        "slug": "product-case",
                        "source_path": str(source),
                        "title": "产品案例",
                        "description": "完整原文",
                        "project": "telebot",
                        "order": 1,
                    }
                )
            )
            output = path.read_bytes().decode("utf-8")
            self.assertEqual(path.parent, posts.parent / "projectDocuments" / "telebot")
            self.assertIn('project: "telebot"', output)
            self.assertEqual(output.split("---\n", 2)[2], body)
            self.assertEqual(
                str(path),
                publish.invoke(
                    {
                        "slug": "product-case",
                        "source_path": str(source),
                        "title": "again",
                        "project": "telebot",
                    }
                ),
            )
            self.assertEqual(path.read_bytes().decode("utf-8"), output)

    def test_source_and_destination_permissions_are_enforced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "posts"
            posts.mkdir()
            source = root / "note.md"
            source.write_text("source", encoding="utf-8")
            fs = FileSystemTool(root=posts, allowed_roots=(posts,))
            tools = build_core_tools(
                filesystem_tool=fs,
                blog_publish_tool=BlogPublishTool(posts),
                include_blog_publish=True,
            )
            publish = next(t for t in tools if t.name == "blog_publish_from_file")
            with self.assertRaises(ValueError):
                publish.invoke({"slug": "note", "source_path": str(source)})
            self.assertEqual(list(posts.iterdir()), [])

    def test_post_frontmatter_is_generated_and_source_frontmatter_is_replaced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "posts"
            posts.mkdir()
            source = root / "source.md"
            source.write_text(
                "---\ntitle: Old\n---\n\n# exact body\n", encoding="utf-8"
            )
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            publisher = BlogPublishTool(posts)
            path = publisher.publish_from_file(
                "post",
                str(source),
                title='A: "title"',
                description="Description",
                published="2026-10-05",
                filesystem_tool=fs,
            )
            output = Path(path).read_text(encoding="utf-8")
            self.assertIn("title: " + json.dumps('A: "title"'), output)
            self.assertTrue(output.endswith("\n# exact body\n"))
            self.assertNotIn("title: Old", output)

    def test_push_confirmation_does_not_reread_or_rewrite_source(self):
        class Push:
            def request_push(self, **kwargs):
                self.path = kwargs["file_path"]
                return "confirmation"

            def confirm_push(self, **kwargs):
                return "confirmed"

        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "posts"
            posts.mkdir()
            source = root / "source.md"
            source.write_bytes(b"exact body")
            push = Push()
            pub = BlogPublishTool(posts, git_push_tool=push)
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            tools = build_core_tools(
                filesystem_tool=fs, blog_publish_tool=pub, include_blog_publish=True
            )
            publish = next(t for t in tools if t.name == "blog_publish_from_file")
            config = {"configurable": {"chat_id": "chat"}}
            self.assertEqual(
                publish.invoke(
                    {
                        "slug": "source",
                        "source_path": str(source),
                        "description": "document",
                        "git_push_enabled": True,
                    },
                    config=config,
                ),
                "confirmation",
            )
            output = Path(push.path).read_bytes()
            source.unlink()
            self.assertEqual(
                publish.invoke(
                    {
                        "slug": "source",
                        "source_path": str(source),
                        "git_push_enabled": True,
                        "push_confirm_token": "token",
                    },
                    config=config,
                ),
                "confirmed",
            )
            self.assertEqual(Path(push.path).read_bytes(), output)

    def test_project_and_slug_traversal_are_rejected_before_writing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "content" / "posts"
            posts.mkdir(parents=True)
            source = root / "source.md"
            source.write_text("body", encoding="utf-8")
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            pub = BlogPublishTool(posts)
            for slug, project in (
                ("../escape", ""),
                ("okay", "../escape"),
                ("okay", "missing"),
            ):
                with self.assertRaises(BlogPublishError):
                    pub.publish_from_file(
                        slug, str(source), project=project, filesystem_tool=fs
                    )
            self.assertFalse((posts.parent / "projectDocuments").exists())

    def test_existing_document_with_different_body_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            posts = root / "posts"
            posts.mkdir()
            source = root / "source.md"
            source.write_text("original body", encoding="utf-8")
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            pub = BlogPublishTool(posts)
            original = pub.publish_from_file(
                "source", str(source), description="document", filesystem_tool=fs
            )
            output = Path(original).read_bytes()
            source.write_text("changed body", encoding="utf-8")
            with self.assertRaises(BlogPublishError):
                pub.publish_from_file(
                    "source", str(source), description="document", filesystem_tool=fs
                )
            self.assertEqual(Path(original).read_bytes(), output)

    def test_push_only_requires_actual_user_authorization(self):
        class Push:
            def push_files(self, **kwargs):
                raise AssertionError("must not push without authorization")

        with TemporaryDirectory() as directory:
            posts = Path(directory) / "posts"
            posts.mkdir()
            pub = BlogPublishTool(posts, git_push_tool=Push())
            push = next(
                t
                for t in build_core_tools(
                    blog_publish_tool=pub, include_blog_publish=True
                )
                if t.name == "blog_push"
            )
            for text in (
                "解释 git push 是什么",
                "只写入博客，不要推送",
                "blog publish 功能没有直接推",
                "Explain how git push works",
                "这个 blog publish 功能有问题",
                "hello",
            ):
                with self.assertRaisesRegex(ValueError, "未明确要求推送"):
                    push.invoke(
                        {"file_paths": []},
                        config={
                            "configurable": {"chat_id": "chat", "message_text": text}
                        },
                    )

    def test_batch_push_retries_against_local_bare_git_without_rewriting_or_recommitting(
        self,
    ):
        import subprocess

        from tele_bot.tools.git_push import GitPushError, GitPushTool

        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "site"
            repo.mkdir()
            remote = root / "remote.git"

            def git(*args, cwd=repo):
                return subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=True,
                ).stdout.strip()

            git("init", "-b", "main")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            git("init", "--bare", "-b", "main", str(remote))
            git("remote", "add", "origin", str(remote))
            (repo / "README.md").write_text("site", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "initial")
            (repo / "unrelated.md").write_text("unrelated work", encoding="utf-8")
            git("add", "unrelated.md")
            posts = repo / "src" / "content" / "posts"
            posts.mkdir(parents=True)
            sources = []
            for i in range(3):
                source = root / f"source-{i}.md"
                source.write_bytes(f"# document {i}\r\nexact body\n".encode())
                sources.append(source)
            fs = FileSystemTool(root=root, allowed_roots=(root,))
            git_push = GitPushTool(repo_root=posts.parent)
            pub = BlogPublishTool(posts, git_push_tool=git_push)
            paths = [
                pub.publish_from_file(
                    f"doc-{i}", str(source), description="document", filesystem_tool=fs
                )
                for i, source in enumerate(sources)
            ]
            originals = {path: Path(path).read_bytes() for path in paths}
            real_run = git_push._run_git
            commands = []

            def flaky_run(args, allow_nonzero=False):
                commands.append(args)
                if args[0] == "push" and sum(cmd[0] == "push" for cmd in commands) == 1:
                    raise GitPushError("simulated connection reset")
                return real_run(args, allow_nonzero)

            git_push._run_git = flaky_run
            push = next(
                t
                for t in build_core_tools(
                    filesystem_tool=fs, blog_publish_tool=pub, include_blog_publish=True
                )
                if t.name == "blog_push"
            )
            config = {
                "configurable": {
                    "chat_id": "chat",
                    "message_text": "把三个文档推送到博客",
                }
            }
            with self.assertRaisesRegex(GitPushError, "connection reset"):
                push.invoke({"file_paths": paths}, config=config)
            for i, source in enumerate(sources):
                self.assertEqual(
                    pub.publish_from_file(
                        f"doc-{i}-v2",
                        str(source),
                        description="renamed",
                        filesystem_tool=fs,
                    ),
                    paths[i],
                )
            self.assertIn(
                "git push 已完成", push.invoke({"file_paths": paths}, config=config)
            )
            self.assertEqual(sum(cmd[0] == "commit" for cmd in commands), 1)
            self.assertEqual(git("rev-list", "--count", "HEAD"), "2")
            tracked = git(
                "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"
            )
            self.assertNotIn("unrelated.md", tracked)
            self.assertEqual(len(list(posts.glob("*.md"))), 3)
            for path, body in originals.items():
                self.assertEqual(Path(path).read_bytes(), body)
                self.assertIn(Path(path).name, tracked)


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
                posts_dir,
                git_push_tool=git_push,  # type: ignore[arg-type]
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
                blog_publish_tool=publisher,
                include_blog_publish=True,  # type: ignore[arg-type]
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


class _StubWriter:
    def __init__(self, out_path: str) -> None:
        self.out_path = out_path
        self.calls = 0

    def write(
        self, content: str, title: str | None = None, filename: str | None = None
    ) -> str:
        self.calls += 1
        return self.out_path


class _StubGitPushTool:
    instances: list["_StubGitPushTool"] = []

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.request_calls: list[dict[str, str | None]] = []
        self.confirm_calls: list[dict[str, str]] = []
        _StubGitPushTool.instances.append(self)

    def request_push(
        self,
        *,
        chat_id: str,
        file_path: str,
        commit_message: str | None = None,
        remote: str | None = None,
        branch: str | None = None,
    ) -> str:
        self.request_calls.append(
            {
                "chat_id": chat_id,
                "file_path": file_path,
                "commit_message": commit_message,
                "remote": remote,
                "branch": branch,
            }
        )
        return "request-ok"

    def confirm_push(self, *, chat_id: str, confirm_token: str) -> str:
        self.confirm_calls.append({"chat_id": chat_id, "confirm_token": confirm_token})
        return "confirm-ok"


class WriteReportGitPushFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        _StubGitPushTool.instances.clear()

    def _get_write_report_tool(self, writer: _StubWriter):
        with patch("tele_bot.tools.lc_adapters.GitPushTool", _StubGitPushTool):
            tools = build_core_tools(write_report_tool=writer)
        write_tools = [tool for tool in tools if tool.name == "write_report"]
        self.assertEqual(len(write_tools), 1)
        return write_tools[0]

    def test_confirm_push_does_not_write_report_again(self) -> None:
        with TemporaryDirectory() as temp_dir:
            out_path = str(Path(temp_dir) / "reports" / "demo.md")
            writer = _StubWriter(out_path=out_path)
            write_report_tool = self._get_write_report_tool(writer)

            result = write_report_tool.invoke(
                {
                    "content": "# ignored on confirm",
                    "git_push_enabled": True,
                    "push_confirm_token": "ABC123",
                },
                config={"configurable": {"chat_id": "chat-1"}},
            )

            self.assertEqual(result, "confirm-ok")
            self.assertEqual(writer.calls, 0)
            self.assertEqual(len(_StubGitPushTool.instances), 1)
            self.assertEqual(
                _StubGitPushTool.instances[0].confirm_calls,
                [{"chat_id": "chat-1", "confirm_token": "ABC123"}],
            )

    def test_request_push_writes_report_then_requests_push(self) -> None:
        with TemporaryDirectory() as temp_dir:
            out_path = str(Path(temp_dir) / "reports" / "demo.md")
            writer = _StubWriter(out_path=out_path)
            write_report_tool = self._get_write_report_tool(writer)

            result = write_report_tool.invoke(
                {
                    "content": "# note",
                    "title": "Demo",
                    "git_push_enabled": True,
                    "commit_message": "docs: add demo note",
                },
                config={"configurable": {"chat_id": "chat-2"}},
            )

            self.assertEqual(result, "request-ok")
            self.assertEqual(writer.calls, 1)
            self.assertEqual(len(_StubGitPushTool.instances), 1)
            self.assertEqual(
                _StubGitPushTool.instances[0].request_calls,
                [
                    {
                        "chat_id": "chat-2",
                        "file_path": out_path,
                        "commit_message": "docs: add demo note",
                        "remote": "origin",
                        "branch": None,
                    }
                ],
            )
