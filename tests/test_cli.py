from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path


class CliTest(unittest.TestCase):
    def test_processes_audio_with_provided_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "sample.wav"
            with wave.open(str(audio), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(b"\x00\x00" * 1_600)
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps(
                    {
                        "text": "目前方案需要验证。为什么现在开始？",
                        "language": "zh",
                        "segments": [
                            {"start": 0, "end": 1.5, "text": "目前方案需要验证。"},
                            {"start": 1.5, "end": 3, "text": "为什么现在开始？"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output_root = root / "processing"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "archeos",
                    "process",
                    str(audio),
                    "--transcript",
                    str(transcript),
                    "--output-root",
                    str(output_root),
                ],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parents[1],
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            package = Path(result.stdout.strip())
            self.assertTrue((package / "manifest.json").is_file())
            self.assertIn("为什么现在开始？", (package / "meeting_summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
