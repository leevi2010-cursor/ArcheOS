from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archeos.speakers import FileSpeakerProvider
from archeos.transcription import Transcript, TranscriptSegment


def transcript() -> Transcript:
    return Transcript(
        "第一段。第二段。",
        (TranscriptSegment("第一段。"), TranscriptSegment("第二段。")),
        "test",
    )


class SpeakerProviderTest(unittest.TestCase):
    def test_applies_neutral_speaker_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "speakers.json"
            path.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"segment": 1, "speaker": "Speaker_1"},
                            {"segment": 2, "speaker": "Speaker_2"},
                        ]
                    }
                )
            )
            attributed = FileSpeakerProvider(path).attribute(Path("audio.wav"), transcript())
            self.assertEqual(
                [segment.speaker for segment in attributed.segments],
                ["Speaker_1", "Speaker_2"],
            )

    def test_rejects_person_identity_as_speaker_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "speakers.json"
            path.write_text(
                json.dumps({"segments": [{"segment": 1, "speaker": "Leo"}]})
            )
            with self.assertRaisesRegex(RuntimeError, "neutral Speaker_N"):
                FileSpeakerProvider(path).attribute(Path("audio.wav"), transcript())


if __name__ == "__main__":
    unittest.main()
