from __future__ import annotations

import ast
import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import archeos.representation.local_repository as representation_storage
from archeos.cli import main
from archeos.representation import (
    AdapterArtifact,
    AdapterBuildResult,
    LocalRepresentationRepository,
    RepresentationError,
    RepresentationManifestError,
    RepresentationService,
    RepresentationValidationError,
    RepresentationWarning,
)
from archeos.representation.identity import (
    canonical_configuration_fingerprint,
    representation_id,
)
from archeos.source import LocalManagedSourceRepository


ID_1 = "src_" + "1" * 32
ID_2 = "src_" + "2" * 32
TIMESTAMP = "2026-08-13T00:00:00.000Z"


class FakeAdapter:
    """Deterministic test-only Adapter; production never registers it."""

    name = "fake"
    version = "1.0"
    kind = "synthetic"
    supported_media_types = ("application/x-synthetic",)

    def __init__(self, *, version: str = "1.0", mutate: Path | None = None) -> None:
        self.version = version
        self.mutate = mutate
        self.calls = 0

    def build(self, source, materialized_path, staging_dir, configuration):
        self.calls += 1
        if configuration.get("fail"):
            raise RuntimeError("synthetic adapter failure")
        if self.mutate is not None:
            self.mutate.write_bytes(b"changed managed bytes")
        artifacts = staging_dir / "artifacts"
        (artifacts / "document.json").write_text('{"synthetic":true}\n', encoding="utf-8")
        (artifacts / "preview.txt").write_text("synthetic preview\n", encoding="utf-8")
        if configuration.get("escape"):
            return AdapterBuildResult(
                kind=self.kind,
                artifacts=(AdapterArtifact("document", "../outside", "application/json"),),
                completeness=1.0,
            )
        if configuration.get("partial"):
            return AdapterBuildResult(
                kind=self.kind,
                artifacts=(AdapterArtifact("document", "artifacts/document.json", "application/json"),),
                completeness=0.5,
                warnings=(RepresentationWarning("synthetic_partial", "synthetic partial", "warning"),),
            )
        return AdapterBuildResult(
            kind=self.kind,
            artifacts=(
                AdapterArtifact("document", "artifacts/document.json", "application/json"),
                AdapterArtifact("preview", "artifacts/preview.txt", "text/plain"),
            ),
            completeness=1.0,
        )


class TrackingAccess:
    def __init__(self, repository: LocalManagedSourceRepository) -> None:
        self.repository = repository
        self.materialized: Path | None = None
        self.get_calls: list[str] = []
        self.verify_calls: list[str] = []

    def get(self, source_id):
        self.get_calls.append(source_id)
        return self.repository.get(source_id)

    def verify(self, source_id):
        self.verify_calls.append(source_id)
        return self.repository.verify(source_id)

    @contextmanager
    def materialize(self, source_id):
        with self.repository.materialize(source_id) as path:
            self.materialized = path
            yield path


class RepresentationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.managed_root = self.root / "managed"
        self.representation_root = self.root / "representations"
        self.external = self.root / "synthetic.input"
        self.external.write_bytes(b"synthetic source bytes")
        self.source_repository = LocalManagedSourceRepository(
            self.managed_root, id_factory=lambda: ID_1, clock=lambda: TIMESTAMP
        )
        self.source = self.source_repository.admit(
            self.external, metadata={"media_type": "application/x-synthetic"}
        ).source
        self.repository = LocalRepresentationRepository(
            self.representation_root, clock=lambda: TIMESTAMP
        )
        self.access = TrackingAccess(self.source_repository)
        self.service = RepresentationService(
            self.access, self.repository, clock=lambda: TIMESTAMP
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, **configuration):
        return self.service.build(self.source.source_id, FakeAdapter(), configuration)

    def representation_dir(self, representation_id_value: str) -> Path:
        return self.representation_root / self.source.source_id / representation_id_value

    def assert_staging_clean(self) -> None:
        staging = self.representation_root / ".staging"
        if staging.exists():
            self.assertEqual(list(staging.iterdir()), [])

    def test_01_same_inputs_are_deterministic_and_existing_is_noop(self) -> None:
        first = self.build()
        second = self.build()
        self.assertEqual(first.status, "built")
        self.assertEqual(second.status, "existing")
        self.assertEqual(first.representation.representation_id, second.representation.representation_id)

    def test_02_version_and_configuration_change_identity(self) -> None:
        first = self.build().representation.representation_id
        version_changed = self.service.build(self.source.source_id, FakeAdapter(version="2.0"), {}).representation.representation_id
        configuration_changed = self.build(mode="other").representation.representation_id
        self.assertEqual(len({first, version_changed, configuration_changed}), 3)

    def test_03_identity_is_independent_of_path_and_generated_time(self) -> None:
        fingerprint = canonical_configuration_fingerprint({"mode": "stable"})
        value = representation_id(
            source_id=ID_1,
            source_content_hash=self.source.content_hash,
            kind="synthetic",
            adapter_name="fake",
            adapter_version="1.0",
            configuration_fingerprint=fingerprint,
        )
        self.assertNotIn(self.external.name, value)
        self.assertNotIn(str(self.external), value)
        self.assertNotIn(TIMESTAMP, value)

    def test_04_invalid_requested_source_fails_before_access(self) -> None:
        with self.assertRaises(RepresentationValidationError):
            self.service.build("invalid", FakeAdapter(), {})
        self.assertEqual(self.access.get_calls, [])
        self.assertEqual(self.access.verify_calls, [])

    def test_05_get_or_verify_identity_mismatch_fails_closed(self) -> None:
        class MismatchAccess(TrackingAccess):
            def get(inner, source_id):
                return replace(super(MismatchAccess, inner).get(source_id), source_id=ID_2)

        with self.assertRaises(RepresentationError):
            RepresentationService(MismatchAccess(self.source_repository), self.repository).build(ID_1, FakeAdapter(), {})

        class VerifyMismatchAccess(TrackingAccess):
            def verify(inner, source_id):
                return replace(super(VerifyMismatchAccess, inner).verify(source_id), source_id=ID_2)

        with self.assertRaises(RepresentationError):
            RepresentationService(VerifyMismatchAccess(self.source_repository), self.repository).build(ID_1, FakeAdapter(), {})

    def test_06_source_change_during_adapter_build_does_not_publish(self) -> None:
        managed = self.managed_root / self.source.managed_locator
        with self.assertRaises(RepresentationError):
            self.service.build(self.source.source_id, FakeAdapter(mutate=managed), {})
        self.assertEqual(self.repository.list_for_source(self.source.source_id), ())
        self.assert_staging_clean()

    def test_07_materialization_is_cleaned_after_build(self) -> None:
        self.build()
        self.assertIsNotNone(self.access.materialized)
        self.assertFalse(self.access.materialized.exists())  # type: ignore[union-attr]

    def test_08_complete_and_partial_invariants(self) -> None:
        complete = self.build().representation
        partial = self.build(partial=True).representation
        self.assertEqual((complete.status, complete.completeness), ("complete", 1.0))
        self.assertEqual((partial.status, partial.completeness), ("partial", 0.5))
        self.assertTrue(partial.warnings)

    def test_09_runtime_failure_creates_no_representation(self) -> None:
        with self.assertRaises(RepresentationError):
            self.build(fail=True)
        self.assertEqual(self.repository.list_for_source(self.source.source_id), ())
        self.assert_staging_clean()

    def test_10_strict_manifest_round_trip_and_unknown_field_failure(self) -> None:
        built = self.build().representation
        self.assertEqual(self.repository.get(built.representation_id), built)
        manifest_path = self.representation_dir(built.representation_id) / "manifest.json"
        payload = json.loads(manifest_path.read_text())
        payload["unknown"] = True
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RepresentationManifestError):
            self.repository.get(built.representation_id)

    def test_11_bad_timestamp_hash_completeness_and_locator_fail_closed(self) -> None:
        built = self.build().representation
        base = built.to_manifest_dict()
        variants = []
        timestamp = json.loads(json.dumps(base))
        timestamp["representation"]["generated_at"] = "bad"
        variants.append(timestamp)
        content_hash = json.loads(json.dumps(base))
        content_hash["source"]["content_hash"] = "bad"
        variants.append(content_hash)
        completeness = json.loads(json.dumps(base))
        completeness["representation"]["completeness"] = 0.5
        variants.append(completeness)
        locator = json.loads(json.dumps(base))
        locator["artifacts"][0]["locator"] = "../outside"
        variants.append(locator)
        for payload in variants:
            with self.subTest(payload=payload):
                with self.assertRaises(RepresentationManifestError):
                    self.repository._parse_manifest(payload)

    def test_12_artifact_tamper_or_missing_or_symlink_fails_verify(self) -> None:
        built = self.build().representation
        artifact = self.representation_dir(built.representation_id) / built.artifacts[0].locator
        artifact.write_bytes(b"tampered")
        self.assertFalse(self.repository.verify(built.representation_id).verified)
        artifact.unlink()
        self.assertFalse(self.repository.verify(built.representation_id).verified)
        artifact.symlink_to(self.external)
        self.assertFalse(self.repository.verify(built.representation_id).verified)

    def test_13_manifest_symlink_fails_verify(self) -> None:
        built = self.build().representation
        manifest = self.representation_dir(built.representation_id) / "manifest.json"
        replacement = manifest.with_name("replacement.json")
        manifest.rename(replacement)
        manifest.symlink_to(replacement)
        self.assertFalse(self.repository.verify(built.representation_id).verified)

    def test_14_existing_corrupt_representation_is_not_overwritten(self) -> None:
        built = self.build().representation
        artifact = self.representation_dir(built.representation_id) / built.artifacts[0].locator
        artifact.write_bytes(b"tampered")
        with self.assertRaises(RepresentationError):
            self.build()
        self.assertEqual(artifact.read_bytes(), b"tampered")

    def test_15_adapter_locator_escape_and_missing_artifact_clean_staging(self) -> None:
        with self.assertRaises(RepresentationError):
            self.build(escape=True)
        self.assert_staging_clean()

        class MissingArtifact(FakeAdapter):
            def build(self, *args, **kwargs):
                return AdapterBuildResult(
                    kind=self.kind,
                    artifacts=(AdapterArtifact("missing", "artifacts/missing", "text/plain"),),
                    completeness=1.0,
                )

        with self.assertRaises(RepresentationError):
            self.service.build(self.source.source_id, MissingArtifact(), {})
        self.assert_staging_clean()

    def test_16_atomic_publish_race_does_not_overwrite(self) -> None:
        def race(staging, final):
            final.mkdir(parents=True)
            (final / "sentinel").write_text("existing", encoding="utf-8")
            raise FileExistsError("synthetic race")

        with patch.object(representation_storage, "publish_directory_no_replace", side_effect=race):
            with self.assertRaises(RepresentationError):
                self.build()
        finals = list((self.representation_root / self.source.source_id).iterdir())
        self.assertEqual(len(finals), 1)
        self.assertEqual((finals[0] / "sentinel").read_text(encoding="utf-8"), "existing")
        self.assert_staging_clean()

    def test_17_list_is_stable_and_scoped_to_source(self) -> None:
        first = self.build(mode="a").representation
        second = self.build(mode="b").representation
        listed = self.service.list(self.source.source_id)
        self.assertEqual([item.representation_id for item in listed], sorted((first.representation_id, second.representation_id)))

    def test_18_source_external_file_is_not_runtime_authority(self) -> None:
        built = self.build().representation
        self.external.unlink()
        self.assertTrue(self.source_repository.verify(self.source.source_id).verified)
        self.assertTrue(self.repository.verify(built.representation_id).verified)

    def test_19_no_information_or_world_model_output_is_created(self) -> None:
        self.build()
        self.assertFalse((self.root / "atomic_information.jsonl").exists())
        self.assertFalse((self.root / "archeos.sqlite3").exists())

    def test_20_public_contract_has_no_parser_or_ocr_imports(self) -> None:
        public_modules = (
            Path("archeos/representation/models.py"),
            Path("archeos/representation/contracts.py"),
            Path("archeos/representation/service.py"),
        )
        forbidden = {"pdfplumber", "openpyxl", "pptx", "paddleocr", "tesseract", "ocr"}
        for module in public_modules:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            imported = {
                alias.name.split(".", 1)[0]
                for node in ast.walk(tree)
                for alias in getattr(node, "names", ())
            }
            self.assertTrue(imported.isdisjoint(forbidden), module)

    def test_21_cli_show_list_verify_and_no_fake_build_adapter(self) -> None:
        built = self.build().representation
        common = (
            "--managed-root",
            str(self.managed_root),
            "--representation-root",
            str(self.representation_root),
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["representation", "show", built.representation_id, *common]), 0)
            self.assertEqual(
                main(["representation", "list", "--source", self.source.source_id, *common]),
                0,
            )
            self.assertEqual(main(["representation", "verify", built.representation_id, *common]), 0)
            self.assertEqual(
                main(["representation", "build", self.source.source_id, "--adapter", "fake", *common]),
                1,
            )

    def test_22_manifest_excludes_external_source_details_and_checks_identity(self) -> None:
        built = self.build().representation
        payload = built.to_manifest_dict()
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.external), rendered)
        self.assertNotIn("ingested" + "_from", rendered)
        self.assertNotIn(str(self.managed_root), rendered)
        payload["representation_id"] = "repr_" + "f" * 64
        with self.assertRaises(RepresentationManifestError):
            self.repository._parse_manifest(payload)


if __name__ == "__main__":
    unittest.main()
