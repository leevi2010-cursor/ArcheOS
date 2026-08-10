from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from archeos.pyannote_speakers import (
    DiarizationTurn,
    PyannoteSpeakerProvider,
    _PyannoteBackend,
    attribute_speakers,
)
from archeos.speakers import FileSpeakerProvider
from archeos.transcription import Transcript, TranscriptSegment


def transcript(*segments: TranscriptSegment) -> Transcript:
    return Transcript(
        " ".join(segment.text for segment in segments),
        segments,
        "test",
    )


class FakeBackend:
    def __init__(self, turns: tuple[DiarizationTurn, ...]) -> None:
        self.turns = turns

    def diarize(self, audio: Path) -> tuple[DiarizationTurn, ...]:
        self.audio = audio
        return self.turns


class FakeAnnotation:
    def itertracks(self, *, yield_label: bool) -> list[tuple[object, None, str]]:
        assert yield_label
        return [
            (SimpleNamespace(start=0.0, end=1.5), None, "SPEAKER_00"),
            (SimpleNamespace(start=1.5, end=3.0), None, "SPEAKER_01"),
        ]


class SpeakerProviderTest(unittest.TestCase):
    def test_applies_neutral_speaker_labels_from_file(self) -> None:
        source = transcript(
            TranscriptSegment("第一段。"), TranscriptSegment("第二段。")
        )
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
            attributed = FileSpeakerProvider(path).attribute(Path("audio.wav"), source)
            self.assertEqual(
                [segment.speaker for segment in attributed.segments],
                ["Speaker_1", "Speaker_2"],
            )

    def test_rejects_person_identity_as_speaker_label(self) -> None:
        source = transcript(TranscriptSegment("第一段。"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "speakers.json"
            path.write_text(
                json.dumps({"segments": [{"segment": 1, "speaker": "Leo"}]})
            )
            with self.assertRaisesRegex(RuntimeError, "neutral Speaker_N"):
                FileSpeakerProvider(path).attribute(Path("audio.wav"), source)

    def test_maps_clear_dominant_overlap_and_normalizes_labels(self) -> None:
        source = transcript(
            TranscriptSegment("第一段。", 0, 4),
            TranscriptSegment("第二段。", 4, 8),
        )
        attributed = attribute_speakers(
            source,
            (
                DiarizationTurn(0, 3.5, "SPEAKER_08"),
                DiarizationTurn(3.5, 4, "SPEAKER_02"),
                DiarizationTurn(4, 8, "SPEAKER_02"),
            ),
        )
        self.assertEqual(
            [segment.speaker for segment in attributed.segments],
            ["Speaker_1", "Speaker_2"],
        )

    def test_keeps_equal_or_non_dominant_overlap_unknown(self) -> None:
        source = transcript(
            TranscriptSegment("平分。", 0, 4),
            TranscriptSegment("无明显主导。", 4, 8),
            TranscriptSegment("没有重叠。", 8, 10),
        )
        attributed = attribute_speakers(
            source,
            (
                DiarizationTurn(0, 2, "A"),
                DiarizationTurn(2, 4, "B"),
                DiarizationTurn(4, 5.5, "A"),
                DiarizationTurn(5.5, 7, "B"),
            ),
        )
        self.assertEqual(
            [segment.speaker for segment in attributed.segments],
            [None, None, None],
        )

    def test_requires_timestamps_for_automatic_alignment(self) -> None:
        source = transcript(TranscriptSegment("缺少时间。"))
        with self.assertRaisesRegex(RuntimeError, "timestamped transcription"):
            attribute_speakers(source, (DiarizationTurn(0, 2, "A"),))

    def test_preserves_complete_existing_neutral_labels(self) -> None:
        source = transcript(TranscriptSegment("已标注。", 0, 2, "Speaker_3"))

        def unexpected_backend() -> FakeBackend:
            self.fail("complete labels should not trigger diarization")

        attributed = PyannoteSpeakerProvider(
            backend_factory=unexpected_backend
        ).attribute(Path("audio.wav"), source)
        self.assertIs(attributed, source)

    def test_disables_telemetry_before_loading_backend(self) -> None:
        source = transcript(TranscriptSegment("第一段。", 0, 2))
        backend = FakeBackend((DiarizationTurn(0, 2, "A"),))

        def backend_factory() -> FakeBackend:
            self.assertEqual(os.environ.get("PYANNOTE_METRICS_ENABLED"), "0")
            return backend

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYANNOTE_METRICS_ENABLED", None)
            attributed = PyannoteSpeakerProvider(
                backend_factory=backend_factory
            ).attribute(Path("audio.wav"), source)
        self.assertEqual(attributed.segments[0].speaker, "Speaker_1")

    def test_pyannote_adapter_uses_exclusive_diarization_with_preloaded_audio(
        self,
    ) -> None:
        decoded = {"waveform": object(), "sample_rate": 16000}

        class FakePipeline:
            def __call__(self, audio: object) -> object:
                self.audio = audio
                return SimpleNamespace(exclusive_speaker_diarization=FakeAnnotation())

        pipeline = FakePipeline()
        backend = _PyannoteBackend(pipeline, decoder=lambda _path: decoded)
        turns = backend.diarize(Path("audio.wav"))

        self.assertIs(pipeline.audio, decoded)
        self.assertEqual(
            turns,
            (
                DiarizationTurn(0.0, 1.5, "SPEAKER_00"),
                DiarizationTurn(1.5, 3.0, "SPEAKER_01"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
