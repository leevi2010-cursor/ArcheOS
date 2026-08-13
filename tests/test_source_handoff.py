from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from archeos.source import (
    HandoffMarkerConflictError,
    HandoffMarkerError,
    HandoffMarkerService,
    LocalManagedSourceRepository,
)


ID_1 = "src_" + "1" * 32
ID_2 = "src_" + "2" * 32
TIMESTAMP = "2026-08-13T00:00:00.000Z"


class HandoffMarkerTest(unittest.TestCase):
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
            clock=lambda: TIMESTAMP,
            chunk_size=3,
        )

    def admitted_service(self, source_id: str = ID_1):
        repository = self.repository(source_id)
        external = self.external()
        source = repository.admit(external).source
        return repository, external, source, HandoffMarkerService(
            repository, self.managed_root, clock=lambda: TIMESTAMP
        )

    @staticmethod
    def marker_for(external: Path) -> Path:
        return external.with_name(f"{external.name}.archeos.md")

    # 1. verified Source writes a marker next to original.
    def test_01_writes_marker_next_to_verified_source(self) -> None:
        _, external, source, service = self.admitted_service()
        result = service.write(source.source_id)
        self.assertEqual(result.status, "written")
        self.assertTrue(self.marker_for(external).is_file())

    # 2. no directory-level marker is created.
    def test_02_marker_path_is_exact_file_sidecar(self) -> None:
        _, external, source, service = self.admitted_service()
        result = service.write(source.source_id)
        self.assertEqual(result.marker.marker_path, self.marker_for(external))
        self.assertFalse((external.parent / "向阳经营系统归档说明.md").exists())

    # 3 and 4. ordinary-language content contains the only system pointer.
    def test_03_marker_uses_chinese_business_language_and_source_id(self) -> None:
        _, external, source, service = self.admitted_service()
        service.write(source.source_id)
        content = self.marker_for(external).read_text(encoding="utf-8")
        self.assertIn("# 向阳经营系统归档说明", content)
        self.assertIn("请不要在此位置继续修改。", content)
        self.assertIn(f"Source ID：{source.source_id}", content)
        self.assertIn(f"python -m archeos source show {source.source_id}", content)

    # 5. the marker contains no controlled storage details.
    def test_04_marker_does_not_leak_managed_path_hash_or_credentials(self) -> None:
        repository, external, source, service = self.admitted_service()
        service.write(source.source_id)
        content = self.marker_for(external).read_text(encoding="utf-8")
        self.assertNotIn(str(self.managed_root), content)
        self.assertNotIn(source.content_hash, content)
        self.assertNotIn("credential", content.lower())
        self.assertNotIn("signature", content.lower())
        self.assertTrue(repository.verify(source.source_id).verified)

    # 6. admission itself never writes external markers.
    def test_05_admission_does_not_write_marker(self) -> None:
        _, external, _, _ = self.admitted_service()
        self.assertFalse(self.marker_for(external).exists())

    # 7. unknown Source fails closed.
    def test_06_missing_source_fails_without_marker(self) -> None:
        repository = self.repository()
        external = self.external()
        service = HandoffMarkerService(repository, self.managed_root)
        with self.assertRaises(HandoffMarkerError):
            service.write(ID_1, target_file=external)
        self.assertFalse(self.marker_for(external).exists())

    # 8. corrupt managed bytes cannot produce a marker.
    def test_07_unverified_source_fails_without_marker(self) -> None:
        repository, external, source, service = self.admitted_service()
        (self.managed_root / source.managed_locator).write_bytes(b"changed")
        with self.assertRaises(HandoffMarkerError):
            service.write(source.source_id)
        self.assertFalse(self.marker_for(external).exists())
        self.assertFalse(repository.verify(source.source_id).verified)

    # 9. without ingested_from, implicit target selection is forbidden.
    def test_08_missing_ingested_from_requires_explicit_target(self) -> None:
        repository, external, source, _ = self.admitted_service()
        service = HandoffMarkerService(repository, self.managed_root)
        with patch.object(repository, "get", return_value=replace(source, ingested_from=None)):
            with self.assertRaises(HandoffMarkerError):
                service.write(source.source_id)
        self.assertFalse(self.marker_for(external).exists())

    # 10. stale historical locations are not guessed.
    def test_09_missing_historical_file_fails_without_marker(self) -> None:
        _, external, source, service = self.admitted_service()
        external.unlink()
        with self.assertRaises(HandoffMarkerError):
            service.write(source.source_id)
        self.assertFalse(self.marker_for(external).exists())

    # 11. an explicit, existing replacement entrance is accepted.
    def test_10_explicit_target_file_is_supported(self) -> None:
        _, external, source, service = self.admitted_service()
        external.unlink()
        replacement = self.external("still-here.txt")
        result = service.write(source.source_id, target_file=replacement)
        self.assertEqual(result.marker.marker_path, self.marker_for(replacement))

    # 12. a directory cannot be marked.
    def test_11_directory_target_fails(self) -> None:
        _, _, source, service = self.admitted_service()
        directory = self.root / "directory"
        directory.mkdir()
        with self.assertRaises(HandoffMarkerError):
            service.write(source.source_id, target_file=directory)

    # 13. managed storage never receives a marker.
    def test_12_target_under_managed_root_fails(self) -> None:
        _, _, source, service = self.admitted_service()
        target = self.managed_root / "outside.txt"
        target.write_bytes(b"not external")
        with self.assertRaises(HandoffMarkerError):
            service.write(source.source_id, target_file=target)
        self.assertFalse(self.marker_for(target).exists())

    # 14. write permission is a required precondition.
    def test_13_non_writable_target_directory_fails(self) -> None:
        _, external, source, service = self.admitted_service()
        with patch("archeos.source.handoff.os.access", return_value=False):
            with self.assertRaises(HandoffMarkerError):
                service.write(source.source_id)
        self.assertFalse(self.marker_for(external).exists())

    # 15. original contents, timestamps, and permissions are untouched.
    def test_14_marker_write_does_not_modify_external_file(self) -> None:
        _, external, source, service = self.admitted_service()
        os.chmod(external, 0o640)
        before = (external.read_bytes(), external.stat().st_mtime_ns, external.stat().st_mode)
        service.write(source.source_id)
        after = (external.read_bytes(), external.stat().st_mtime_ns, external.stat().st_mode)
        self.assertEqual(before, after)

    # 16. the immutable snapshot and manifest remain untouched.
    def test_15_marker_write_does_not_modify_managed_source_or_manifest(self) -> None:
        _, _, source, service = self.admitted_service()
        managed = self.managed_root / source.managed_locator
        manifest = managed.parent / "manifest.json"
        before = (managed.read_bytes(), manifest.read_bytes())
        service.write(source.source_id)
        self.assertEqual(before, (managed.read_bytes(), manifest.read_bytes()))

    # 17. retry is an idempotent no-op.
    def test_16_same_source_retry_returns_existing(self) -> None:
        _, _, source, service = self.admitted_service()
        self.assertEqual(service.write(source.source_id).status, "written")
        self.assertEqual(service.write(source.source_id).status, "existing")

    # 18. a sidecar cannot silently move to another Source.
    def test_17_different_source_marker_conflicts(self) -> None:
        repository, external, source, service = self.admitted_service()
        service.write(source.source_id)
        second = self.external("second.txt", b"second")
        second_source = LocalManagedSourceRepository(
            self.managed_root, id_factory=lambda: ID_2, clock=lambda: TIMESTAMP
        ).admit(second).source
        with self.assertRaises(HandoffMarkerConflictError):
            HandoffMarkerService(repository, self.managed_root).write(
                second_source.source_id, target_file=external
            )

    # 19. user content is never overwritten.
    def test_18_non_archeos_existing_file_conflicts(self) -> None:
        _, external, source, service = self.admitted_service()
        marker = self.marker_for(external)
        marker.write_text("user-authored", encoding="utf-8")
        with self.assertRaises(HandoffMarkerError):
            service.write(source.source_id)
        self.assertEqual(marker.read_text(encoding="utf-8"), "user-authored")

    # 20. failed publication cleans the staged sidecar.
    def test_19_publish_failure_leaves_no_partial_marker_or_staging(self) -> None:
        _, external, source, service = self.admitted_service()
        with patch("archeos.source.handoff.publish_file_no_replace", side_effect=OSError):
            with self.assertRaises(HandoffMarkerError):
                service.write(source.source_id)
        self.assertFalse(self.marker_for(external).exists())
        self.assertEqual(list(external.parent.glob("*.staging")), [])

    # 21. a valid marker is readable and confirms the Source.
    def test_20_show_reads_valid_marker_and_verifies_source(self) -> None:
        _, external, source, service = self.admitted_service()
        service.write(source.source_id)
        shown = service.show(self.marker_for(external))
        self.assertEqual(shown.marker.source_id, source.source_id)
        self.assertTrue(shown.source_exists)
        self.assertTrue(shown.source_verified)

    # 22. malformed data cannot be presented as a marker.
    def test_21_show_rejects_corrupt_marker(self) -> None:
        _, external, _, service = self.admitted_service()
        self.marker_for(external).write_text("broken", encoding="utf-8")
        with self.assertRaises(HandoffMarkerError):
            service.show(self.marker_for(external))

    # 23. external disappearance does not invalidate the Managed Source.
    def test_22_show_survives_external_file_deletion(self) -> None:
        repository, external, source, service = self.admitted_service()
        marker = self.marker_for(external)
        service.write(source.source_id)
        external.unlink()
        shown = service.show(marker)
        self.assertTrue(shown.source_exists)
        self.assertTrue(shown.source_verified)
        self.assertTrue(repository.verify(source.source_id).verified)

    # 24. deleting the optional marker cannot affect Source verification or restore.
    def test_23_marker_deletion_does_not_affect_source_or_restore(self) -> None:
        repository, external, source, service = self.admitted_service()
        marker = self.marker_for(external)
        service.write(source.source_id)
        marker.unlink()
        self.assertTrue(repository.verify(source.source_id).verified)
        restored = self.root / "restored.txt"
        repository.restore(source.source_id, restored)
        self.assertEqual(restored.read_bytes(), external.read_bytes())

    # 25. source_id-only marker semantics do not create external-file synchronization.
    def test_24_external_changes_do_not_sync_managed_bytes(self) -> None:
        repository, external, source, service = self.admitted_service()
        service.write(source.source_id)
        external.write_bytes(b"external changed")
        self.assertTrue(repository.verify(source.source_id).verified)
        self.assertEqual((self.managed_root / source.managed_locator).read_bytes(), b"synthetic")

    # 25. symlink targets cannot redirect a write outside the user-selected file.
    def test_25_symlink_target_fails_without_marker(self) -> None:
        _, external, source, service = self.admitted_service()
        link = self.root / "external-link.txt"
        link.symlink_to(external)
        with self.assertRaises(HandoffMarkerError):
            service.write(source.source_id, target_file=link)
        self.assertFalse(self.marker_for(link).exists())
