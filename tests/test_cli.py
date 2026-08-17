from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    IngestionResult,
    JsonlAtomicInformationStore,
)
from archeos.cli import main
from archeos.codex_app_server import CodexAnalysisProvider
from archeos.pipeline import ProcessingError
from archeos.pyannote_speakers import PyannoteSpeakerProvider
from archeos.representation_information import (
    FileRepresentationAnalysisProvider,
)
from archeos.wechat_digest import WechatDigestResult, WechatSemanticPreparation
from archeos.workspace import WorkspaceConfig
from archeos.world_model import SQLiteWorldModelRepository


class CliTest(unittest.TestCase):
    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_prints_business_summary(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.run.return_value = WechatDigestResult(
            run_id="run_" + "a" * 32,
            new_messages=3,
            new_attachments=1,
            durable_information=4,
            local_only=1,
            unsupported=2,
            pending_human=1,
            context_objects=2,
            checkpoint_published=True,
            replayed=False,
        )
        output = StringIO()
        with redirect_stdout(output):
            result = main(["wechat", "digest", "--from-now"])
        self.assertEqual(result, 0)
        self.assertIn("新增消息：3", output.getvalue())
        self.assertIn("待你判断：1", output.getvalue())
        self.assertIn("checkpoint：已推进", output.getvalue())
        digest_service.return_value.run.assert_called_once_with(
            since=None, from_now=True, all_history=False
        )
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_prepares_without_running_digest(self, require_workspace: Mock, capture_provider: Mock, digest_service: Mock) -> None:
        require_workspace.return_value = WorkspaceConfig(Path("/workspace"), Path("/config"))
        digest_service.return_value.prepare_next_semantic.return_value = WechatSemanticPreparation("run_" + "a" * 32, "repr_" + "b" * 32, ("unit_" + "c" * 64,))
        output = StringIO()
        with redirect_stdout(output):
            result = main(["wechat", "digest", "--prepare-next-semantic"])
        self.assertEqual(result, 0)
        self.assertIn('"semantic_provider_calls": 0', output.getvalue())
        self.assertIn('"governance_provider_calls": "unavailable"', output.getvalue())
        digest_service.return_value.prepare_next_semantic.assert_called_once_with(batch_size=40)
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_managed_audio")
    def test_constructs_file_backed_providers(
        self,
        process_managed_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_managed_audio.return_value = Path("/tmp/package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        with (
            patch("archeos.cli.require_workspace", return_value=WorkspaceConfig(Path("/workspace"), Path("/config"))),
            redirect_stdout(StringIO()),
        ):
            result = main(
                [
                    "process", "sample.wav", "--transcript", "transcript.json",
                    "--speaker-map", "speakers.json", "--analysis-file", "analysis.json",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(process_managed_audio.call_count, 1)
        self.assertEqual(process_managed_audio.call_args.args[0], "sample.wav")
        transcriber, speaker_provider, analysis_provider = process_managed_audio.call_args.args[
            3:
        ]
        self.assertEqual(transcriber.transcript_file, Path("transcript.json"))
        self.assertEqual(speaker_provider.speaker_map, Path("speakers.json"))
        self.assertEqual(analysis_provider.analysis_file, Path("analysis.json"))
        self.assertEqual(ingest_processing_package.call_count, 1)
        store = ingest_processing_package.call_args.args[1]
        self.assertEqual(
            store.path,
            Path("/workspace/03_information/atomic_information.jsonl"),
        )

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_managed_audio")
    def test_uses_automatic_diarization_and_codex_sdk_by_default(
        self,
        process_managed_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_managed_audio.return_value = Path("/tmp/package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        with (
            patch("archeos.cli.require_workspace", return_value=WorkspaceConfig(Path("/workspace"), Path("/config"))),
            redirect_stdout(StringIO()),
        ):
            result = main(["process", "sample.wav", "--transcript", "transcript.json"])
        self.assertEqual(result, 0)
        speaker_provider = process_managed_audio.call_args.args[4]
        analysis_provider = process_managed_audio.call_args.args[5]
        self.assertIsInstance(speaker_provider, PyannoteSpeakerProvider)
        self.assertIsInstance(analysis_provider, CodexAnalysisProvider)

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.RepresentationInformationService.extract")
    def test_representation_extract_requires_an_explicit_file_provider(
        self,
        extract: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        extract.return_value = Path("/tmp/representation-package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        arguments = [
            "information",
            "--store",
            "store.jsonl",
            "extract",
            "repr_" + "a" * 64,
            "--managed-root",
            "managed",
            "--representation-root",
            "representations",
            "--output-root",
            "information",
        ]
        with (
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(arguments)
        self.assertEqual(error.exception.code, 2)
        extract.assert_not_called()

        with redirect_stdout(StringIO()):
            self.assertEqual(main([*arguments, "--analysis-file", "fixture.json"]), 0)
        fixture_provider = extract.call_args.args[-1]
        self.assertIsInstance(fixture_provider, FileRepresentationAnalysisProvider)
        self.assertEqual(fixture_provider.path, Path("fixture.json"))

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

    def test_context_build_cli_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "world.sqlite3"
            information = root / "information.jsonl"
            proposals = root / "proposals.jsonl"
            journal = root / "journal.jsonl"
            with SQLiteWorldModelRepository(database) as repository:
                record = repository.create_object("CLI Context Target")
            output = StringIO()
            with redirect_stdout(output):
                result = main([
                    "context", "--database", str(database),
                    "--information-store", str(information),
                    "--proposal-store", str(proposals), "--journal", str(journal),
                    "build", "--scope", "object", record.object_id,
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["root"]["object_id"], record.object_id)
            self.assertEqual(payload["metadata"]["complete"], True)

    def test_context_build_cli_invalid_root_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "world.sqlite3"
            output = StringIO()
            with redirect_stdout(output):
                result = main([
                    "context", "--database", str(database), "build",
                    "--scope", "object", "missing",
                ])
            self.assertEqual(result, 1)
            self.assertIn("error:", output.getvalue())
            self.assertFalse(database.exists())

    def test_source_cli_admit_show_verify_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root / "synthetic.txt"
            external.write_bytes(b"synthetic source")
            managed_root = root / "managed"
            source_id = "src_" + "a" * 32

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "source",
                        "admit",
                        str(external),
                        "--source-id",
                        source_id,
                        "--managed-root",
                        str(managed_root),
                    ]
                )
            admitted = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(admitted["source"]["source_id"], source_id)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "show",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["source_id"], source_id)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "verify",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertTrue(json.loads(output.getvalue())["verified"])

            target = root / "restored.txt"
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "restore",
                            source_id,
                            str(target),
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertTrue(json.loads(output.getvalue())["verified"])
            self.assertEqual(target.read_bytes(), external.read_bytes())

    def test_source_handoff_cli_write_and_show(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            external = root / "synthetic.txt"
            external.write_bytes(b"synthetic handoff")
            managed_root = root / "managed"
            source_id = "src_" + "c" * 32

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "admit",
                            str(external),
                            "--source-id",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "handoff",
                            "write",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["status"], "written")

            output = StringIO()
            marker = external.with_name(f"{external.name}.archeos.md")
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "handoff",
                            "show",
                            str(marker),
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            shown = json.loads(output.getvalue())
            self.assertEqual(shown["marker"]["source_id"], source_id)
            self.assertTrue(shown["source_verified"])

    def test_process_managed_source_synthetic_end_to_end_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "synthetic.wav"
            with wave.open(str(original), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16_000)
                audio.writeframes(b"\x00\x00" * 1_600)
            source_id = "src_" + "b" * 32
            managed_root = root / "managed"
            output_root = root / "processing"
            information_store = root / "information.jsonl"
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps(
                    {
                        "text": "Synthetic decision. Synthetic ambiguity.",
                        "segments": [
                            {"text": "Synthetic decision.", "start": 0, "end": 1},
                            {"text": "Synthetic ambiguity.", "start": 1, "end": 2},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            speakers = root / "speakers.json"
            speakers.write_text(
                json.dumps({"segments": [{"segment": 1, "speaker": "Speaker_1"}]}),
                encoding="utf-8",
            )
            analysis = root / "analysis.json"
            analysis.write_text(
                json.dumps(
                    {
                        "meeting_summary": {
                            "topic": "Synthetic smoke",
                            "participants": ["Speaker_1"],
                            "discussion_goal": "Validate Managed Source provenance.",
                            "main_discussion": ["Synthetic processing."],
                            "key_viewpoints": ["Managed Source is authoritative."],
                            "agreements": ["Use source_id."],
                            "disagreements": [],
                            "unresolved_questions": ["Synthetic ambiguity."],
                            "next_actions": ["Review the output."],
                        },
                        "atomic_information_candidates": [
                            {
                                "statement": "Synthetic decision.",
                                "semantic_type": "decision",
                                "concerns": ["Synthetic smoke"],
                                "evidence_segments": [1],
                                "context": "Synthetic test only.",
                                "confidence": 0.9,
                            }
                        ],
                        "residue": [
                            {
                                "evidence_segments": [2],
                                "reason_not_absorbed": "Synthetic ambiguity.",
                                "future_value_or_uncertainty": "Needs synthetic review.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "source", "admit", str(original), "--source-id", source_id,
                            "--managed-root", str(managed_root),
                        ]
                    ),
                    0,
                )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "process", source_id,
                            "--managed-root", str(managed_root),
                            "--output-root", str(output_root),
                            "--information-store", str(information_store),
                            "--transcript", str(transcript),
                            "--speaker-map", str(speakers),
                            "--analysis-file", str(analysis),
                        ]
                    ),
                    0,
                )
            package = output_root / source_id
            manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "1.2")
            self.assertEqual(manifest["source"]["id"], source_id)
            self.assertNotIn("path", manifest["source"])
            self.assertNotIn(str(original), (package / "transcript.md").read_text())
            candidate = json.loads(
                (package / "atomic_information_candidates.jsonl").read_text().splitlines()[0]
            )
            self.assertEqual(candidate["source_evidence"][0]["source_id"], source_id)
            self.assertEqual(
                JsonlAtomicInformationStore(information_store)
                .list_atomic_information()[0]
                .source_evidence[0]
                .source_id,
                source_id,
            )
            original.unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(["source", "verify", source_id, "--managed-root", str(managed_root)]),
                    0,
                )

    def test_process_rejects_external_path_as_unknown_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "process", str(Path(temp) / "external.wav"),
                        "--managed-root", str(Path(temp) / "managed"),
                        "--output-root", str(Path(temp) / "processing"),
                        "--information-store", str(Path(temp) / "information.jsonl"),
                    ]
                )
            self.assertEqual(result, 1)
            self.assertIn("source_id", output.getvalue())

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_managed_audio")
    def test_processing_publish_failure_never_triggers_ingestion(
        self,
        process_managed_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_managed_audio.side_effect = ProcessingError("cannot publish safely")
        with redirect_stdout(StringIO()):
            result = main(["process", "src_" + "a" * 32])

        self.assertEqual(result, 1)
        ingest_processing_package.assert_not_called()


if __name__ == "__main__":
    unittest.main()
