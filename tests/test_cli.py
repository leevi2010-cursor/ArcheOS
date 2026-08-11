from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from archeos.cli import main
from archeos.codex_app_server import CodexAnalysisProvider
from archeos.atomic_information import IngestionResult
from archeos.pyannote_speakers import PyannoteSpeakerProvider


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


if __name__ == "__main__":
    unittest.main()
