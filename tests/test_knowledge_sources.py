import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.knowledge.sources import KnowledgeSourceResolver
from tele_bot.tools.workspace_policy import WorkspacePolicy


class KnowledgeSourceResolverTests(unittest.TestCase):
    def test_resolves_ordered_fuzzy_directory_and_collects_supported_files(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "files_data"
            target = workspace / "Job" / "八股"
            target.mkdir(parents=True)
            (target / "a.md").write_text("a", encoding="utf-8")
            (target / "b.txt").write_text("b", encoding="utf-8")
            (target / "image.png").write_bytes(b"png")
            policy = WorkspacePolicy(
                allowed_roots=(root,), workspace_root=workspace,
                quarantine_root=root / "quarantine",
            )

            selection = KnowledgeSourceResolver(policy).resolve(
                "将files_data目录下的job目录下的八股目录里的文档全部导入",
                bound_directory=str(workspace),
            )

            self.assertEqual(selection.directory, target.resolve())
            self.assertEqual([path.name for path in selection.files], ["a.md", "b.txt"])

    def test_direct_path_cannot_escape_bound_directory(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            bound = workspace / "bound"
            outside = workspace / "outside"
            bound.mkdir(parents=True)
            outside.mkdir()
            (outside / "secret.md").write_text("secret", encoding="utf-8")
            policy = WorkspacePolicy(
                allowed_roots=(root,), workspace_root=workspace,
                quarantine_root=root / "quarantine",
            )
            with self.assertRaisesRegex(ValueError, "绑定目录之外"):
                KnowledgeSourceResolver(policy).resolve(
                    str(outside), bound_directory=str(bound)
                )


if __name__ == "__main__":
    unittest.main()
