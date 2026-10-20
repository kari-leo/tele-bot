import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tele_bot.tools.file_mutations import FileMutationService
from tele_bot.tools.workspace_policy import WorkspacePolicy


class FileMutationServiceTests(unittest.TestCase):
    def _service(self, root: Path, quarantine: Path) -> FileMutationService:
        policy = WorkspacePolicy(
            allowed_roots=(root, quarantine),
            workspace_root=root,
            quarantine_root=quarantine,
        )
        return FileMutationService(policy)

    def test_overwrite_requires_confirmation_and_keeps_backup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            quarantine = Path(temp_dir) / "quarantine"
            root.mkdir()
            target = root / "note.txt"
            target.write_text("before", encoding="utf-8")
            service = self._service(root, quarantine)

            prepared = service.prepare_overwrite("note.txt", "after")
            self.assertEqual(target.read_text(encoding="utf-8"), "before")

            result = service.confirm_overwrite(str(prepared["confirm_token"]))

            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            self.assertEqual(Path(str(result["backup"])).read_text(encoding="utf-8"), "before")

    def test_overwrite_rejects_target_hash_drift(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            quarantine = Path(temp_dir) / "quarantine"
            root.mkdir()
            target = root / "note.txt"
            target.write_text("before", encoding="utf-8")
            service = self._service(root, quarantine)

            prepared = service.prepare_overwrite("note.txt", "after")
            target.write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "target changed"):
                service.confirm_overwrite(str(prepared["confirm_token"]))
            self.assertEqual(target.read_text(encoding="utf-8"), "changed")

    def test_delete_moves_file_to_quarantine_and_token_is_single_use(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            quarantine = Path(temp_dir) / "quarantine"
            root.mkdir()
            target = root / "remove.txt"
            target.write_text("discard", encoding="utf-8")
            service = self._service(root, quarantine)

            prepared = service.prepare_delete("remove.txt")
            self.assertTrue(target.exists())
            result = service.confirm_delete(str(prepared["confirm_token"]))

            quarantined = Path(str(result["quarantine_path"]))
            self.assertFalse(target.exists())
            self.assertEqual(quarantined.read_text(encoding="utf-8"), "discard")
            with self.assertRaisesRegex(ValueError, "invalid or already used"):
                service.confirm_delete(str(prepared["confirm_token"]))


if __name__ == "__main__":
    unittest.main()
