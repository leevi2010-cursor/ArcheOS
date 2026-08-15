from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import archeos.atomic_information.ingestion as atomic_information_ingestion
from archeos.cli import main
from archeos.atomic_information import (
    AtomicInformationRevision,
    IngestionResult,
    JsonlAtomicInformationStore,
    ingest_processing_package,
)

SOURCE_ID = "synthetic-source-123456789abc"
MANAGED_SOURCE_ID = "src_" + "a" * 32
PROCESSING_TIME = "2026-08-11T00:00:00+00:00"


def candidate(
    candidate_id: str = "synthetic-source-123456789abc-0001",
    *,
    statement: str = "Synthetic operations require traceable information.",
    semantic_type: str = "requirement",
    status: str = "candidate",
    segment: int = 1,
    evidence_source_id: str = SOURCE_ID,
) -> dict[str, object]:
    return {
        "id": candidate_id,
        "statement": statement,
        "semantic_type": semantic_type,
        "concerns": ["Synthetic Operations"],
        "source_evidence": [
            {
                "source_id": evidence_source_id,
                "artifact": "transcript.md",
                "segment": segment,
                "speaker": "Speaker_1",
                "start": "00:00:01.000",
                "end": "00:00:02.500",
                "excerpt": "Synthetic evidence excerpt.",
            }
        ],
        "context": "Synthetic context retained for validation.",
        "confidence": 0.9,
        "processing_time": PROCESSING_TIME,
        "status": status,
    }


def write_package(
    root: Path,
    candidates: list[dict[str, object]],
    *,
    schema_version: str = "1.1",
    legacy_review: bool = False,
) -> Path:
    package = root / "processing-package"
    package.mkdir()
    manifest: dict[str, object] = {
        "schema_version": schema_version,
        "source": {"id": SOURCE_ID},
    }
    if schema_version == "1.2":
        manifest["source"] = {
            "id": MANAGED_SOURCE_ID,
            "content_hash": "sha256:" + "a" * 64,
            "size_bytes": 3200,
            "media_type": "audio/wav",
            "filename_hint": "synthetic.wav",
        }
    if legacy_review:
        manifest["review"] = {
            "status": "awaiting_human_review",
            "automatic_core_write": False,
        }
    else:
        manifest["downstream"] = {
            "atomic_information_ingestion": "automatic_after_contract_validation",
            "world_model_write": "governed",
        }
    (package / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    artifact = (
        "atomic_notes.jsonl"
        if schema_version == "1.0"
        else "atomic_information_candidates.jsonl"
    )
    (package / artifact).write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates),
        encoding="utf-8",
    )
    for artifact in ("transcript.md", "meeting_summary.md", "residue.md"):
        (package / artifact).write_text("synthetic\n", encoding="utf-8")
    return package


def managed_candidate() -> dict[str, object]:
    return candidate(evidence_source_id=MANAGED_SOURCE_ID)


class RecordingAtomicInformationStore:
    def __init__(self) -> None:
        self.received: tuple[AtomicInformationRevision, ...] = ()

    def ingest_batch(
        self, revisions: tuple[AtomicInformationRevision, ...]
    ) -> IngestionResult:
        self.received = tuple(revisions)
        return IngestionResult(
            created=len(self.received),
            existing=0,
            failed=0,
            atomic_information_ids=tuple(
                item.atomic_information_id for item in self.received
            ),
        )

    def get_current(self, atomic_information_id: str) -> AtomicInformationRevision:
        raise NotImplementedError(atomic_information_id)

    def list_revisions(
        self, atomic_information_id: str
    ) -> tuple[AtomicInformationRevision, ...]:
        raise NotImplementedError(atomic_information_id)

    def append_revision(
        self, revision: AtomicInformationRevision
    ) -> AtomicInformationRevision:
        raise NotImplementedError(revision)

    def list_atomic_information(self) -> tuple[AtomicInformationRevision, ...]:
        return self.received


class AtomicInformationIngestionTest(unittest.TestCase):
    def test_concurrent_store_writes_do_not_drop_distinct_origins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "first").mkdir()
            (root / "second").mkdir()
            first_package = write_package(root / "first", [candidate()])
            second_package = write_package(
                root / "second",
                [candidate("synthetic-source-123456789abc-0002", segment=2)],
            )
            store_path = root / "atomic_information.jsonl"
            entered = threading.Event()
            release = threading.Event()
            read_calls = 0

            class PausingStore(JsonlAtomicInformationStore):
                def _read_all(self):
                    nonlocal read_calls
                    read_calls += 1
                    if read_calls == 1:
                        entered.set()
                        release.wait(timeout=3)
                    return super()._read_all()

            errors: list[Exception] = []

            def ingest(package: Path) -> None:
                try:
                    ingest_processing_package(package, PausingStore(store_path))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            first = threading.Thread(target=ingest, args=(first_package,))
            second = threading.Thread(target=ingest, args=(second_package,))
            first.start()
            self.assertTrue(entered.wait(timeout=3))
            second.start()
            self.assertEqual(read_calls, 1)
            release.set()
            first.join(timeout=3)
            second.join(timeout=3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                len(JsonlAtomicInformationStore(store_path).list_atomic_information()), 2
            )

    def test_valid_candidate_becomes_durable_information_with_exact_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlAtomicInformationStore(root / "atomic_information.jsonl")

            result = ingest_processing_package(package, store)
            information = store.list_atomic_information()[0]

            self.assertEqual(result.created, 1)
            self.assertEqual(information.revision_number, 1)
            self.assertEqual(
                information.revision_id,
                f"{information.atomic_information_id}-r0001",
            )
            self.assertEqual(information.statement, candidate()["statement"])
            self.assertEqual(information.semantic_type, "requirement")
            self.assertEqual(information.raw_concerns, ("Synthetic Operations",))
            self.assertEqual(information.related_object_ids, ())
            self.assertEqual(information.context, candidate()["context"])
            self.assertEqual(information.confidence, 0.9)
            evidence = information.source_evidence[0]
            self.assertEqual(evidence.source_id, SOURCE_ID)
            self.assertEqual(evidence.artifact, "transcript.md")
            self.assertEqual(evidence.segment, 1)
            self.assertEqual(evidence.speaker, "Speaker_1")
            self.assertEqual(evidence.start, "00:00:01.000")
            self.assertEqual(evidence.end, "00:00:02.500")
            self.assertEqual(evidence.excerpt, "Synthetic evidence excerpt.")

    def test_atomic_information_identity_and_exact_reingestion_are_deterministic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlAtomicInformationStore(root / "atomic_information.jsonl")

            first = ingest_processing_package(package, store)
            before = (root / "atomic_information.jsonl").read_bytes()
            second = ingest_processing_package(package, store)

            self.assertEqual(
                first.atomic_information_ids, second.atomic_information_ids
            )
            expected = (
                "atomic_info_"
                + hashlib.sha256(
                    f"{SOURCE_ID}\0{candidate()['id']}".encode()
                ).hexdigest()[:32]
            )
            self.assertEqual(first.atomic_information_ids[0], expected)
            self.assertEqual(second.created, 0)
            self.assertEqual(second.existing, 1)
            self.assertEqual(before, (root / "atomic_information.jsonl").read_bytes())
            self.assertEqual(
                len(store.list_revisions(first.atomic_information_ids[0])), 1
            )

    def test_changed_content_for_same_origin_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlAtomicInformationStore(root / "atomic_information.jsonl")
            ingest_processing_package(package, store)
            before = (root / "atomic_information.jsonl").read_bytes()

            changed_root = root / "changed"
            changed_root.mkdir()
            changed = write_package(
                changed_root,
                [
                    candidate(
                        "synthetic-source-123456789abc-0002",
                        statement="New statement that must not be partially written.",
                        segment=2,
                    ),
                    candidate(statement="Mutated source statement."),
                ],
            )
            with self.assertRaisesRegex(ValueError, "origin collision"):
                ingest_processing_package(changed, store)

            self.assertEqual(before, (root / "atomic_information.jsonl").read_bytes())

    def test_invalid_candidate_rejects_the_whole_batch(self) -> None:
        mutations = {
            "semantic type": lambda item: item.update(semantic_type="unsupported"),
            "confidence": lambda item: item.update(confidence=1.5),
            "required field": lambda item: item.pop("statement"),
            "Evidence": lambda item: item.update(source_evidence=[]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                invalid = candidate("synthetic-source-123456789abc-0002", segment=2)
                mutate(invalid)
                package = write_package(root, [candidate(), invalid])
                store_path = root / "atomic_information.jsonl"

                with self.assertRaises(ValueError):
                    ingest_processing_package(
                        package, JsonlAtomicInformationStore(store_path)
                    )

                self.assertFalse(store_path.exists())

    def test_duplicate_origin_inside_batch_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate(), candidate()])
            store_path = root / "atomic_information.jsonl"

            with self.assertRaisesRegex(ValueError, "duplicate origin candidate"):
                ingest_processing_package(
                    package, JsonlAtomicInformationStore(store_path)
                )

            self.assertFalse(store_path.exists())

    def test_empty_atomic_information_candidate_file_is_a_successful_no_op(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [])
            store_path = root / "atomic_information.jsonl"

            result = ingest_processing_package(
                package, JsonlAtomicInformationStore(store_path)
            )

            self.assertEqual(result, IngestionResult(0, 0, 0, ()))
            self.assertFalse(store_path.exists())

    def test_explicit_revision_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlAtomicInformationStore(root / "atomic_information.jsonl")
            atomic_information_id = ingest_processing_package(
                package, store
            ).atomic_information_ids[0]
            first = store.get_current(atomic_information_id)
            second = replace(
                first,
                revision_number=2,
                revision_id=f"{atomic_information_id}-r0002",
                statement="Corrected synthetic statement.",
                created_at="2026-08-11T01:00:00+00:00",
                revision_reason="explicit_correction",
            )

            store.append_revision(second)

            history = store.list_revisions(atomic_information_id)
            self.assertEqual(history, (first, second))
            self.assertEqual(store.get_current(atomic_information_id), second)

    def test_legacy_v1_package_with_proposed_status_is_ingestible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(
                root,
                [candidate(status="proposed")],
                schema_version="1.0",
                legacy_review=True,
            )

            result = ingest_processing_package(
                package,
                JsonlAtomicInformationStore(root / "atomic_information.jsonl"),
            )

            self.assertEqual(result.created, 1)

    def test_managed_source_v1_2_package_is_ingestible_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [managed_candidate()], schema_version="1.2")
            store = JsonlAtomicInformationStore(root / "atomic_information.jsonl")

            first = ingest_processing_package(package, store)
            second = ingest_processing_package(package, store)

            self.assertEqual(first.created, 1)
            self.assertEqual(second.existing, 1)
            self.assertEqual(
                store.list_atomic_information()[0].origin_source_id, MANAGED_SOURCE_ID
            )

    def test_managed_source_v1_2_rejects_path_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [managed_candidate()], schema_version="1.2")
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["path"] = "/private/synthetic.wav"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must not contain paths"):
                ingest_processing_package(
                    package,
                    JsonlAtomicInformationStore(root / "atomic_information.jsonl"),
                )

    def test_managed_source_v1_2_rejects_invalid_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [managed_candidate()], schema_version="1.2")
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["content_hash"] = "not-a-hash"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "full sha256 hash"):
                ingest_processing_package(
                    package,
                    JsonlAtomicInformationStore(root / "atomic_information.jsonl"),
                )

    def test_managed_source_v1_2_rejects_invalid_source_ids_without_writing(self) -> None:
        invalid_ids = (
            "",
            "..",
            "../escape",
            "/absolute/path",
            "src_x/child",
            "src_" + "a" * 31 + "\\",
            "src_" + "A" * 32,
            "src_" + "a" * 31,
            "src_" + "a" * 33,
            "src_" + "g" * 32,
            "a" * 32,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, invalid_id in enumerate(invalid_ids):
                with self.subTest(source_id=invalid_id):
                    package_root = root / str(index)
                    package_root.mkdir()
                    package = write_package(
                        package_root, [managed_candidate()], schema_version="1.2"
                    )
                    manifest_path = package / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["source"]["id"] = invalid_id
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    store_path = package_root / "atomic_information.jsonl"

                    with self.assertRaises(ValueError):
                        ingest_processing_package(
                            package, JsonlAtomicInformationStore(store_path)
                        )
                    self.assertFalse(store_path.exists())

    def test_corrupted_existing_store_fails_instead_of_skipping_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store_path = root / "atomic_information.jsonl"
            store_path.write_text("not json\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "corrupted Atomic Information store"
            ):
                ingest_processing_package(
                    package, JsonlAtomicInformationStore(store_path)
                )

            self.assertEqual(store_path.read_text(encoding="utf-8"), "not json\n")

    def test_ingestion_depends_only_on_atomic_information_store_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = write_package(Path(temp), [candidate()])
            store = RecordingAtomicInformationStore()

            result = ingest_processing_package(package, store)

            self.assertEqual(result.created, 1)
            self.assertEqual(len(store.received), 1)
            self.assertNotIn(
                "jsonl_store", inspect.getsource(atomic_information_ingestion)
            )
            self.assertNotIn(
                "world_model", inspect.getsource(atomic_information_ingestion)
            )

    def test_normal_process_orchestration_ingests_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store_path = root / "atomic_information.jsonl"

            with (
                patch("archeos.cli.process_managed_audio", return_value=package),
                redirect_stdout(StringIO()),
            ):
                process_result = main(
                    [
                        "process",
                        SOURCE_ID,
                        "--information-store",
                        str(store_path),
                    ]
                )
            with redirect_stdout(StringIO()):
                retry_result = main(
                    [
                        "information",
                        "--store",
                        str(store_path),
                        "ingest",
                        str(package),
                    ]
                )

            self.assertEqual(process_result, 0)
            self.assertEqual(retry_result, 0)
            store = JsonlAtomicInformationStore(store_path)
            self.assertEqual(len(store.list_atomic_information()), 1)
            self.assertEqual(
                len(
                    store.list_revisions(
                        store.list_atomic_information()[0].atomic_information_id
                    )
                ),
                1,
            )

    def test_ingestion_failure_keeps_processing_package_and_store_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invalid = candidate()
            invalid["confidence"] = 2
            package = write_package(root, [invalid])
            store_path = root / "atomic_information.jsonl"

            with (
                patch("archeos.cli.process_managed_audio", return_value=package),
                redirect_stdout(StringIO()),
            ):
                result = main(
                    [
                        "process",
                        SOURCE_ID,
                        "--information-store",
                        str(store_path),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertTrue(package.is_dir())
            self.assertTrue((package / "atomic_information_candidates.jsonl").is_file())
            self.assertFalse(store_path.exists())

    def test_three_atomic_information_smoke_is_idempotent_and_revision_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidates = [
                candidate(
                    f"synthetic-source-123456789abc-{index:04d}",
                    statement=f"Synthetic statement {index}.",
                    segment=index,
                )
                for index in range(1, 4)
            ]
            package = write_package(root, candidates)
            store = JsonlAtomicInformationStore(root / "atomic_information.jsonl")

            first = ingest_processing_package(package, store)
            second = ingest_processing_package(package, store)
            original = store.get_current(first.atomic_information_ids[0])
            revision = replace(
                original,
                revision_number=2,
                revision_id=f"{original.atomic_information_id}-r0002",
                statement="Revised synthetic statement.",
                created_at="2026-08-11T02:00:00+00:00",
                revision_reason="synthetic_smoke_revision",
            )
            store.append_revision(revision)

            self.assertEqual(first.created, 3)
            self.assertEqual(second.existing, 3)
            self.assertEqual(len(store.list_atomic_information()), 3)
            self.assertEqual(
                len(store.list_revisions(original.atomic_information_id)), 2
            )
            self.assertEqual(
                store.list_revisions(original.atomic_information_id)[0],
                original,
            )

    def test_clean_cut_removes_canonical_note_domain_aliases(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertFalse((repository / "archeos" / "notes").exists())
        self.assertFalse((repository / "03_notes").exists())
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (repository / "archeos").rglob("*.py")
        )
        for forbidden in (
            "NoteRevision",
            "NoteStore",
            "JsonlNoteStore",
            "note_id",
            "source_note_id",
            "archeos.notes",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
