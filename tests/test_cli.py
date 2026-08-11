from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from archeos.cli import main
from archeos.codex_app_server import CodexAnalysisProvider
from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    IngestionResult,
    JsonlAtomicInformationStore,
)
from archeos.pyannote_speakers import PyannoteSpeakerProvider
from archeos.world_model import SQLiteWorldModelRepository


class CliTest(unittest.TestCase):
    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_audio")
    def test_constructs_file_backed_providers(
        self,
        process_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_audio.return_value = Path("/tmp/package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        with redirect_stdout(StringIO()):
            result = main(
                [
                    "process",
                    "sample.wav",
                    "--transcript",
                    "transcript.json",
                    "--speaker-map",
                    "speakers.json",
                    "--analysis-file",
                    "analysis.json",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(process_audio.call_count, 1)
        transcriber, speaker_provider, analysis_provider = process_audio.call_args.args[
            2:
        ]
        self.assertEqual(transcriber.transcript_file, Path("transcript.json"))
        self.assertEqual(speaker_provider.speaker_map, Path("speakers.json"))
        self.assertEqual(analysis_provider.analysis_file, Path("analysis.json"))
        self.assertEqual(ingest_processing_package.call_count, 1)
        store = ingest_processing_package.call_args.args[1]
        self.assertEqual(
            store.path,
            Path("03_information/atomic_information.jsonl"),
        )

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_audio")
    def test_uses_automatic_diarization_and_codex_sdk_by_default(
        self,
        process_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_audio.return_value = Path("/tmp/package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        with redirect_stdout(StringIO()):
            result = main(["process", "sample.wav", "--transcript", "transcript.json"])
        self.assertEqual(result, 0)
        speaker_provider = process_audio.call_args.args[3]
        analysis_provider = process_audio.call_args.args[4]
        self.assertIsInstance(speaker_provider, PyannoteSpeakerProvider)
        self.assertIsInstance(analysis_provider, CodexAnalysisProvider)

    def test_object_commands_return_human_readable_world_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "world-model.sqlite3"

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "object",
                        "--database",
                        str(database),
                        "create",
                        "--name",
                        "Synthetic Operations",
                        "--role",
                        "business_line",
                    ]
                )
            created = json.loads(output.getvalue())
            object_id = created["object_id"]
            self.assertEqual(result, 0)
            self.assertTrue(object_id.startswith("obj_"))
            self.assertEqual(created["current_name"], "Synthetic Operations")
            self.assertEqual(created["roles"], ["business_line"])

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "object",
                            "--database",
                            str(database),
                            "rename",
                            object_id,
                            "--name",
                            "Renamed Operations",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "object",
                            "--database",
                            str(database),
                            "add-role",
                            object_id,
                            "brand",
                        ]
                    ),
                    0,
                )

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    ["object", "--database", str(database), "show", object_id]
                )
            shown = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(shown["object_id"], object_id)
            self.assertEqual(shown["current_name"], "Renamed Operations")
            self.assertEqual(shown["roles"], ["brand", "business_line"])

    def test_digest_information_uses_explicit_file_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "world.sqlite3"
            information_path = root / "information.jsonl"
            proposal_path = root / "proposals.jsonl"
            journal_path = root / "journal.jsonl"
            interpretation_path = root / "interpretation.json"
            with SQLiteWorldModelRepository(database) as repository:
                record = repository.create_object("CLI Target")

            source_id = "synthetic-cli-source"
            candidate_id = "synthetic-cli-candidate"
            atomic_information_id = (
                "atomic_info_"
                + hashlib.sha256(f"{source_id}\0{candidate_id}".encode()).hexdigest()[
                    :32
                ]
            )
            JsonlAtomicInformationStore(information_path).ingest_batch(
                (
                    AtomicInformationRevision(
                        atomic_information_id=atomic_information_id,
                        revision_number=1,
                        revision_id=f"{atomic_information_id}-r0001",
                        origin_source_id=source_id,
                        origin_candidate_id=candidate_id,
                        origin_fingerprint=hashlib.sha256(b"synthetic-cli").hexdigest(),
                        statement="CLI Target is an active project.",
                        semantic_type="requirement",
                        raw_concerns=("CLI Target",),
                        related_object_ids=(),
                        source_evidence=(
                            EvidenceRecord(
                                source_id=source_id,
                                artifact="synthetic.md",
                                segment=1,
                                speaker="Speaker_1",
                                start="00:00:01.000",
                                end="00:00:02.000",
                                excerpt="CLI Target is an active project.",
                            ),
                        ),
                        context="Synthetic CLI smoke context.",
                        confidence=0.9,
                        created_at="2026-08-11T00:00:00+00:00",
                        revision_reason="initial_ingestion",
                    ),
                )
            )
            fields = {
                "target_object_id": record.object_id,
                "secondary_object_id": None,
                "name": None,
                "role": "project",
                "relation": None,
                "relationship_id": None,
                "lifecycle_state": None,
                "start_at": None,
                "actual_end_at": None,
                "target_end_at": None,
                "completion_condition": None,
            }
            interpretation_path.write_text(
                json.dumps(
                    {
                        "operations": [{"kind": "add_role", **fields}],
                        "rationale": "Synthetic deterministic CLI interpretation.",
                        "evidence_sufficient": True,
                        "conflict": False,
                        "ambiguous": False,
                        "claim": None,
                    }
                ),
                encoding="utf-8",
            )

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "digest",
                        "--database",
                        str(database),
                        "--information-store",
                        str(information_path),
                        "--proposal-store",
                        str(proposal_path),
                        "--journal",
                        str(journal_path),
                        "information",
                        atomic_information_id,
                        "--interpretation-file",
                        str(interpretation_path),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "automatic")
            with SQLiteWorldModelRepository(database) as repository:
                self.assertEqual(
                    repository.list_roles(record.object_id, active_only=True)[0].role,
                    "project",
                )
            self.assertFalse(proposal_path.exists())
            self.assertTrue(journal_path.exists())


if __name__ == "__main__":
    unittest.main()
