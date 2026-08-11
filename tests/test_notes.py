from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import archeos.notes.ingestion as note_ingestion
from archeos.cli import main
from archeos.notes import (
    IngestionResult,
    JsonlNoteStore,
    NoteRevision,
    ingest_processing_package,
)

SOURCE_ID = "synthetic-source-123456789abc"
PROCESSING_TIME = "2026-08-11T00:00:00+00:00"


def candidate(
    candidate_id: str = "synthetic-source-123456789abc-0001",
    *,
    statement: str = "Synthetic operations require traceable information.",
    semantic_type: str = "requirement",
    status: str = "candidate",
    segment: int = 1,
) -> dict[str, object]:
    return {
        "id": candidate_id,
        "statement": statement,
        "semantic_type": semantic_type,
        "concerns": ["Synthetic Operations"],
        "source_evidence": [
            {
                "source_id": SOURCE_ID,
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
    if legacy_review:
        manifest["review"] = {
            "status": "awaiting_human_review",
            "automatic_core_write": False,
        }
    else:
        manifest["downstream"] = {
            "note_ingestion": "automatic_after_contract_validation",
            "world_model_write": "governed",
        }
    (package / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    (package / "atomic_notes.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates),
        encoding="utf-8",
    )
    for artifact in ("transcript.md", "meeting_summary.md", "residue.md"):
        (package / artifact).write_text("synthetic\n", encoding="utf-8")
    return package


class RecordingNoteStore:
    def __init__(self) -> None:
        self.received: tuple[NoteRevision, ...] = ()

    def ingest_batch(self, revisions: tuple[NoteRevision, ...]) -> IngestionResult:
        self.received = tuple(revisions)
        return IngestionResult(
            created=len(self.received),
            existing=0,
            failed=0,
            note_ids=tuple(item.note_id for item in self.received),
        )

    def get_current(self, note_id: str) -> NoteRevision:
        raise NotImplementedError(note_id)

    def list_revisions(self, note_id: str) -> tuple[NoteRevision, ...]:
        raise NotImplementedError(note_id)

    def append_revision(self, revision: NoteRevision) -> NoteRevision:
        raise NotImplementedError(revision)

    def list_notes(self) -> tuple[NoteRevision, ...]:
        return self.received


class NoteIngestionTest(unittest.TestCase):
    def test_valid_candidate_becomes_durable_note_with_exact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlNoteStore(root / "notes.jsonl")

            result = ingest_processing_package(package, store)
            note = store.list_notes()[0]

            self.assertEqual(result.created, 1)
            self.assertEqual(note.revision_number, 1)
            self.assertEqual(note.revision_id, f"{note.note_id}-r0001")
            self.assertEqual(note.statement, candidate()["statement"])
            self.assertEqual(note.semantic_type, "requirement")
            self.assertEqual(note.raw_concerns, ("Synthetic Operations",))
            self.assertEqual(note.related_object_ids, ())
            self.assertEqual(note.context, candidate()["context"])
            self.assertEqual(note.confidence, 0.9)
            evidence = note.source_evidence[0]
            self.assertEqual(evidence.source_id, SOURCE_ID)
            self.assertEqual(evidence.artifact, "transcript.md")
            self.assertEqual(evidence.segment, 1)
            self.assertEqual(evidence.speaker, "Speaker_1")
            self.assertEqual(evidence.start, "00:00:01.000")
            self.assertEqual(evidence.end, "00:00:02.500")
            self.assertEqual(evidence.excerpt, "Synthetic evidence excerpt.")

    def test_note_identity_and_exact_reingestion_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlNoteStore(root / "notes.jsonl")

            first = ingest_processing_package(package, store)
            before = (root / "notes.jsonl").read_bytes()
            second = ingest_processing_package(package, store)

            self.assertEqual(first.note_ids, second.note_ids)
            expected = (
                "note_"
                + hashlib.sha256(
                    f"{SOURCE_ID}\0{candidate()['id']}".encode()
                ).hexdigest()[:32]
            )
            self.assertEqual(first.note_ids[0], expected)
            self.assertEqual(second.created, 0)
            self.assertEqual(second.existing, 1)
            self.assertEqual(before, (root / "notes.jsonl").read_bytes())
            self.assertEqual(len(store.list_revisions(first.note_ids[0])), 1)

    def test_changed_content_for_same_origin_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlNoteStore(root / "notes.jsonl")
            ingest_processing_package(package, store)
            before = (root / "notes.jsonl").read_bytes()

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

            self.assertEqual(before, (root / "notes.jsonl").read_bytes())

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
                store_path = root / "notes.jsonl"

                with self.assertRaises(ValueError):
                    ingest_processing_package(package, JsonlNoteStore(store_path))

                self.assertFalse(store_path.exists())

    def test_duplicate_origin_inside_batch_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate(), candidate()])
            store_path = root / "notes.jsonl"

            with self.assertRaisesRegex(ValueError, "duplicate origin candidate"):
                ingest_processing_package(package, JsonlNoteStore(store_path))

            self.assertFalse(store_path.exists())

    def test_empty_atomic_note_file_is_a_successful_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [])
            store_path = root / "notes.jsonl"

            result = ingest_processing_package(package, JsonlNoteStore(store_path))

            self.assertEqual(result, IngestionResult(0, 0, 0, ()))
            self.assertFalse(store_path.exists())

    def test_explicit_revision_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store = JsonlNoteStore(root / "notes.jsonl")
            note_id = ingest_processing_package(package, store).note_ids[0]
            first = store.get_current(note_id)
            second = replace(
                first,
                revision_number=2,
                revision_id=f"{note_id}-r0002",
                statement="Corrected synthetic statement.",
                created_at="2026-08-11T01:00:00+00:00",
                revision_reason="explicit_correction",
            )

            store.append_revision(second)

            history = store.list_revisions(note_id)
            self.assertEqual(history, (first, second))
            self.assertEqual(store.get_current(note_id), second)

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
                package, JsonlNoteStore(root / "notes.jsonl")
            )

            self.assertEqual(result.created, 1)

    def test_corrupted_existing_store_fails_instead_of_skipping_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store_path = root / "notes.jsonl"
            store_path.write_text("not json\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "corrupted Note store"):
                ingest_processing_package(package, JsonlNoteStore(store_path))

            self.assertEqual(store_path.read_text(encoding="utf-8"), "not json\n")

    def test_ingestion_depends_only_on_note_store_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = write_package(Path(temp), [candidate()])
            store = RecordingNoteStore()

            result = ingest_processing_package(package, store)

            self.assertEqual(result.created, 1)
            self.assertEqual(len(store.received), 1)
            self.assertNotIn("jsonl_store", inspect.getsource(note_ingestion))
            self.assertNotIn("world_model", inspect.getsource(note_ingestion))

    def test_normal_process_orchestration_ingests_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = write_package(root, [candidate()])
            store_path = root / "notes.jsonl"

            with (
                patch("archeos.cli.process_audio", return_value=package),
                redirect_stdout(StringIO()),
            ):
                process_result = main(
                    [
                        "process",
                        "synthetic.wav",
                        "--note-store",
                        str(store_path),
                    ]
                )
            with redirect_stdout(StringIO()):
                retry_result = main(
                    [
                        "note",
                        "--store",
                        str(store_path),
                        "ingest",
                        str(package),
                    ]
                )

            self.assertEqual(process_result, 0)
            self.assertEqual(retry_result, 0)
            store = JsonlNoteStore(store_path)
            self.assertEqual(len(store.list_notes()), 1)
            self.assertEqual(
                len(store.list_revisions(store.list_notes()[0].note_id)), 1
            )

    def test_ingestion_failure_keeps_processing_package_and_store_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invalid = candidate()
            invalid["confidence"] = 2
            package = write_package(root, [invalid])
            store_path = root / "notes.jsonl"

            with (
                patch("archeos.cli.process_audio", return_value=package),
                redirect_stdout(StringIO()),
            ):
                result = main(
                    [
                        "process",
                        "synthetic.wav",
                        "--note-store",
                        str(store_path),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertTrue(package.is_dir())
            self.assertTrue((package / "atomic_notes.jsonl").is_file())
            self.assertFalse(store_path.exists())

    def test_three_note_smoke_is_idempotent_and_revision_safe(self) -> None:
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
            store = JsonlNoteStore(root / "notes.jsonl")

            first = ingest_processing_package(package, store)
            second = ingest_processing_package(package, store)
            original = store.get_current(first.note_ids[0])
            revision = replace(
                original,
                revision_number=2,
                revision_id=f"{original.note_id}-r0002",
                statement="Revised synthetic statement.",
                created_at="2026-08-11T02:00:00+00:00",
                revision_reason="synthetic_smoke_revision",
            )
            store.append_revision(revision)

            self.assertEqual(first.created, 3)
            self.assertEqual(second.existing, 3)
            self.assertEqual(len(store.list_notes()), 3)
            self.assertEqual(len(store.list_revisions(original.note_id)), 2)
            self.assertEqual(store.list_revisions(original.note_id)[0], original)


if __name__ == "__main__":
    unittest.main()
