from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import archeos.source.local_repository as local_repository
from archeos.source import (
    AdmissionError,
    LocalManagedSourceRepository,
    ManifestError,
    SourceConflictError,
    SourceError,
    SourceIntegrityError,
    SourceNotFoundError,
    SourceValidationError,
)


ID_1 = "src_" + "1" * 32
ID_2 = "src_" + "2" * 32
ID_3 = "src_" + "3" * 32


class SourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.managed_root = self.root / "managed"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def external(self, name: str = "synthetic.txt", content: bytes = b"synthetic") -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def repository(self, source_id: str = ID_1) -> LocalManagedSourceRepository:
        return LocalManagedSourceRepository(
            self.managed_root,
            id_factory=lambda: source_id,
            clock=lambda: "2026-08-13T00:00:00.000Z",
            chunk_size=3,
        )

    def assert_staging_clean(self) -> None:
        staging = self.managed_root / ".staging"
        if staging.exists():
            self.assertEqual(list(staging.iterdir()), [])

    def manifest_path(self, source_id: str) -> Path:
        return self.managed_root / "sources" / source_id / "manifest.json"

    # 1. ordinary admission
    def test_01_admits_regular_file(self) -> None:
        source = self.repository().admit(self.external())
        self.assertTrue(source.source_id.startswith("src_"))
        self.assertTrue((self.managed_root / source.managed_locator).is_file())

    # 2. opaque identity
    def test_02_source_id_does_not_contain_path_name_or_hash(self) -> None:
        path = self.external("name-with-private-word.bin", b"opaque")
        source = self.repository().admit(path)
        digest = hashlib.sha256(b"opaque").hexdigest()
        self.assertNotIn(path.name, source.source_id)
        self.assertNotIn(str(path), source.source_id)
        self.assertNotIn(digest, source.source_id)

    # 3. injected deterministic identity
    def test_03_injected_id_factory_is_deterministic(self) -> None:
        source = self.repository(ID_2).admit(self.external())
        self.assertEqual(source.source_id, ID_2)

    # 4. external file is unchanged
    def test_04_admission_does_not_modify_external_file(self) -> None:
        path = self.external()
        before = (path.stat().st_mtime_ns, path.stat().st_mode, path.read_bytes())
        self.repository().admit(path)
        after = (path.stat().st_mtime_ns, path.stat().st_mode, path.read_bytes())
        self.assertEqual(before, after)

    # 5. managed bytes are exact
    def test_05_managed_copy_matches_external_bytes(self) -> None:
        path = self.external(content=b"a" * 100)
        source = self.repository().admit(path)
        self.assertEqual((self.managed_root / source.managed_locator).read_bytes(), path.read_bytes())

    # 6. Manifest fields are correct
    def test_06_manifest_contains_verified_size_hash_and_locator(self) -> None:
        path = self.external("sample.md", b"manifest")
        source = self.repository().admit(path)
        manifest = json.loads(self.manifest_path(source.source_id).read_text())
        self.assertEqual(manifest["schema_version"], "1.0")
        managed = manifest["managed_source"]
        self.assertEqual(managed["size_bytes"], len(b"manifest"))
        self.assertEqual(managed["content_hash"], "sha256:" + hashlib.sha256(b"manifest").hexdigest())
        self.assertEqual(managed["managed_locator"], source.managed_locator)

    # 7. bounded streaming copy
    def test_07_admission_does_not_use_unbounded_read_bytes(self) -> None:
        path = self.external(content=b"x" * 20)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            self.repository().admit(path)

    # 8. missing input
    def test_08_missing_input_fails(self) -> None:
        with self.assertRaises(AdmissionError):
            self.repository().admit(self.root / "missing.bin")
        self.assert_staging_clean()

    # 9. directory input
    def test_09_directory_input_fails(self) -> None:
        directory = self.root / "directory"
        directory.mkdir()
        with self.assertRaises(SourceValidationError):
            self.repository().admit(directory)
        self.assert_staging_clean()

    # 10. symlink input
    def test_10_symlink_input_fails(self) -> None:
        path = self.external()
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(SourceValidationError):
            self.repository().admit(link)
        self.assert_staging_clean()

    # 11. changed input during copy
    def test_11_changed_external_file_fails_closed(self) -> None:
        path = self.external()
        real_copy = local_repository._copy_stream

        def copy_then_change(source_path: Path, destination_path: Path, **kwargs: object):
            result = real_copy(source_path, destination_path, **kwargs)
            source_path.write_bytes(source_path.read_bytes() + b"changed")
            return result

        with patch.object(local_repository, "_copy_stream", side_effect=copy_then_change):
            with self.assertRaises(AdmissionError):
                self.repository().admit(path)
        self.assert_staging_clean()

    # 12. managed copy mismatch
    def test_12_managed_copy_mismatch_fails_closed(self) -> None:
        with patch.object(
            local_repository,
            "_hash_file",
            side_effect=[("sha256:" + "0" * 64, 9), ("sha256:" + "1" * 64, 9)],
        ):
            with self.assertRaises(AdmissionError):
                self.repository().admit(self.external(content=b"123456789"))
        self.assert_staging_clean()

    # 13. Manifest write failure
    def test_13_manifest_write_failure_does_not_publish(self) -> None:
        with patch.object(local_repository.LocalManagedSourceRepository, "_write_manifest", side_effect=OSError("synthetic")):
            with self.assertRaises(AdmissionError):
                self.repository().admit(self.external())
        self.assertFalse((self.managed_root / "sources" / ID_1).exists())
        self.assert_staging_clean()

    # 14. final publication collision
    def test_14_final_rename_collision_does_not_overwrite(self) -> None:
        repo = self.repository()
        repo.admit(self.external("one.txt", b"one"))
        with self.assertRaises(SourceConflictError):
            repo.admit(self.external("two.txt", b"two"))
        self.assertEqual((self.managed_root / repo.get(ID_1).managed_locator).read_bytes(), b"one")
        self.assert_staging_clean()

    # 15. all admission failures clean staging and final output
    def test_15_admission_failures_leave_no_formal_source(self) -> None:
        path = self.external()
        with patch.object(local_repository.LocalManagedSourceRepository, "_write_manifest", side_effect=OSError):
            with self.assertRaises(AdmissionError):
                self.repository().admit(path)
        self.assertEqual(list((self.managed_root / "sources").glob("*")), [])
        self.assert_staging_clean()

    # 16. repeated same content gets a new identity and a hint
    def test_16_same_content_gets_distinct_id_and_equivalence_hint(self) -> None:
        repo = LocalManagedSourceRepository(
            self.managed_root,
            id_factory=iter((ID_1, ID_2)).__next__,
        )
        first = repo.admit(self.external("first.txt", b"same"))
        second = repo.admit(self.external("second.txt", b"same"))
        self.assertNotEqual(first.source_id, second.source_id)
        self.assertEqual(second.content_equivalent_source_ids, (first.source_id,))

    # 17. same name with different bytes is not equivalent
    def test_17_same_name_different_content_is_not_equivalent(self) -> None:
        repo = LocalManagedSourceRepository(
            self.managed_root,
            id_factory=iter((ID_1, ID_2)).__next__,
        )
        repo.admit(self.external("same.txt", b"one"))
        second = repo.admit(self.external("same.txt", b"two"))
        self.assertEqual(second.content_equivalent_source_ids, ())

    # 18. different names with same bytes are equivalent
    def test_18_different_names_same_content_are_equivalent(self) -> None:
        repo = LocalManagedSourceRepository(
            self.managed_root,
            id_factory=iter((ID_1, ID_2)).__next__,
        )
        first = repo.admit(self.external("a.txt", b"same"))
        second = repo.admit(self.external("b.bin", b"same"))
        self.assertEqual(second.content_equivalent_source_ids, (first.source_id,))

    # 19. show returns an immutable model
    def test_19_show_returns_immutable_read_model(self) -> None:
        source = self.repository().admit(self.external())
        shown = self.repository().get(source.source_id)
        self.assertEqual(shown, source)
        with self.assertRaises(Exception):
            shown.source_id = ID_2  # type: ignore[misc]

    # 20. list has stable source-id ordering
    def test_20_list_is_stably_sorted(self) -> None:
        repo = LocalManagedSourceRepository(
            self.managed_root,
            id_factory=iter((ID_2, ID_1)).__next__,
        )
        repo.admit(self.external("b", b"b"))
        repo.admit(self.external("a", b"a"))
        self.assertEqual([item.source_id for item in repo.list_sources()], [ID_1, ID_2])

    # 21. unknown source fails closed
    def test_21_unknown_source_fails_closed(self) -> None:
        with self.assertRaises(SourceNotFoundError):
            self.repository().get(ID_1)

    # 22. corrupt Manifest fails closed
    def test_22_corrupt_manifest_fails_closed(self) -> None:
        manifest = self.manifest_path(ID_1)
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{not-json")
        with self.assertRaises(ManifestError):
            self.repository().get(ID_1)

    # 23. missing managed bytes fail verification
    def test_23_missing_managed_bytes_fail_verification(self) -> None:
        source = self.repository().admit(self.external())
        (self.managed_root / source.managed_locator).unlink()
        result = self.repository().verify(source.source_id)
        self.assertFalse(result.verified)
        self.assertIn("missing", result.reason or "")

    # 24. tampered managed bytes fail verification
    def test_24_tampered_managed_bytes_fail_verification(self) -> None:
        source = self.repository().admit(self.external())
        (self.managed_root / source.managed_locator).write_bytes(b"tampered")
        result = self.repository().verify(source.source_id)
        self.assertFalse(result.verified)
        self.assertFalse(result.hash_matches)

    # 25. verify is read-only
    def test_25_verify_does_not_modify_source_or_manifest(self) -> None:
        source = self.repository().admit(self.external())
        managed = self.managed_root / source.managed_locator
        manifest = self.manifest_path(source.source_id)
        before = (managed.stat().st_mtime_ns, managed.read_bytes(), manifest.read_bytes())
        self.assertTrue(self.repository().verify(source.source_id).verified)
        after = (managed.stat().st_mtime_ns, managed.read_bytes(), manifest.read_bytes())
        self.assertEqual(before, after)

    # 26. successful restore
    def test_26_restore_is_verified_and_exact(self) -> None:
        path = self.external(content=b"restore me")
        source = self.repository().admit(path)
        target = self.root / "restored" / "copy.txt"
        target.parent.mkdir()
        result = self.repository().restore(source.source_id, target)
        self.assertTrue(result.success)
        self.assertEqual(target.read_bytes(), path.read_bytes())

    # 27. existing target is never overwritten
    def test_27_existing_restore_target_is_rejected(self) -> None:
        source = self.repository().admit(self.external())
        target = self.root / "existing.txt"
        target.write_bytes(b"keep")
        with self.assertRaises(SourceConflictError):
            self.repository().restore(source.source_id, target)
        self.assertEqual(target.read_bytes(), b"keep")

    # 28. interrupted restore leaves no half-file
    def test_28_interrupted_restore_cleans_staging(self) -> None:
        source = self.repository().admit(self.external())
        target_dir = self.root / "restore"
        target_dir.mkdir()
        with patch.object(local_repository, "_copy_stream", side_effect=OSError("interrupt")):
            with self.assertRaises(SourceError):
                self.repository().restore(source.source_id, target_dir / "result")
        self.assertFalse((target_dir / "result").exists())
        self.assertEqual(list(target_dir.iterdir()), [])

    # 29. corrupt Source cannot be restored
    def test_29_corrupt_source_cannot_be_restored(self) -> None:
        source = self.repository().admit(self.external())
        (self.managed_root / source.managed_locator).write_bytes(b"corrupt")
        with self.assertRaises(SourceIntegrityError):
            self.repository().restore(source.source_id, self.root / "copy")

    # 30. restore does not mutate Source or Manifest
    def test_30_restore_preserves_source_and_manifest(self) -> None:
        source = self.repository().admit(self.external())
        managed = self.managed_root / source.managed_locator
        manifest = self.manifest_path(source.source_id)
        before = (managed.read_bytes(), manifest.read_bytes())
        target = self.root / "copy"
        self.repository().restore(source.source_id, target)
        self.assertEqual(before, (managed.read_bytes(), manifest.read_bytes()))

    # 31. the Source package is storage-independent
    def test_31_source_core_does_not_import_tos_or_sqlite(self) -> None:
        package = Path(__file__).parents[1] / "archeos" / "source"
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ]
            imports.extend(
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            )
            self.assertNotIn("sqlite3", imports, path.name)
            self.assertFalse(any("tos" in name.lower() for name in imports), path.name)

    # 32. Managed Source runtime data is ignored while governance is tracked
    def test_32_managed_source_data_is_ignored(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        private = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", "01_inbox/sources/src_fake/original.bin"],
            cwd=repository,
            check=False,
        )
        governance = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", "01_inbox/AGENTS.md"],
            cwd=repository,
            check=False,
        )
        self.assertEqual(private.returncode, 0)
        self.assertEqual(governance.returncode, 1)

    # 33. tests use synthetic data only
    def test_33_source_fixture_has_no_private_path_or_credential(self) -> None:
        content = Path(__file__).read_text(encoding="utf-8")
        for forbidden in ("sk" + "-proj-", "xwechat" + "_files"):
            self.assertNotIn(forbidden, content)
        self.assertIn("synthetic", content)

    # 34. synthetic end-to-end admission / verify / restore smoke
    def test_34_synthetic_integration_smoke(self) -> None:
        repo = LocalManagedSourceRepository(
            self.managed_root,
            id_factory=iter((ID_1, ID_2)).__next__,
            chunk_size=2,
        )
        first_path = self.external("first.synthetic", b"same synthetic bytes")
        second_path = self.external("second.synthetic", b"same synthetic bytes")
        first = repo.admit(first_path)
        second = repo.admit(second_path)
        self.assertNotEqual(first.source_id, second.source_id)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(second.content_equivalent_source_ids, (first.source_id,))
        self.assertTrue(repo.verify(first.source_id).verified)
        self.assertTrue(repo.verify(second.source_id).verified)
        restored = self.root / "restored" / "synthetic.bin"
        restored.parent.mkdir()
        result = repo.restore(first.source_id, restored)
        self.assertTrue(result.verified)
        self.assertEqual(result.content_hash, first.content_hash)
        self.assertEqual(restored.read_bytes(), (self.managed_root / first.managed_locator).read_bytes())
        first_path.unlink()
        second_path.unlink()
        self.assertTrue(repo.verify(first.source_id).verified)
        self.assertTrue(repo.restore(second.source_id, self.root / "restored" / "second.bin").verified)
        self.assertFalse((self.root / "02_processing").exists())
        self.assertFalse((self.root / "03_information").exists())
        self.assertFalse((self.root / "04_core").exists())


if __name__ == "__main__":
    unittest.main()
