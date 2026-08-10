from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from archeos.cli import main


class CliTest(unittest.TestCase):
    @patch("archeos.cli.process_audio")
    def test_constructs_file_backed_providers(self, process_audio: Mock) -> None:
        process_audio.return_value = Path("/tmp/package")
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
        transcriber, speaker_provider, analysis_provider = process_audio.call_args.args[2:]
        self.assertEqual(transcriber.transcript_file, Path("transcript.json"))
        self.assertEqual(speaker_provider.speaker_map, Path("speakers.json"))
        self.assertEqual(analysis_provider.analysis_file, Path("analysis.json"))


if __name__ == "__main__":
    unittest.main()
