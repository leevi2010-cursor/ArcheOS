from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path

from archeos.analysis import (
    AnalysisResult,
    AtomicNoteCandidate,
    MeetingSummary,
    ResidueItem,
)
from archeos.pipeline import ARTIFACTS, ProcessingError, process_audio
from archeos.transcription import Transcript, TranscriptSegment


FIXED_TIME = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class StubTranscriptionProvider:
    def transcribe(self, audio: Path) -> Transcript:
        self.audio = audio
        return Transcript(
            text=(
                "我们决定先验证通用流程，同时要求保留上下文。"
                "这个结论引用了旧方案。"
                "但旧方案指哪个版本并不清楚。"
                "下一步需要人工复核。"
            ),
            segments=(
                TranscriptSegment(
                    "我们决定先验证通用流程，同时要求保留上下文。", 0.0, 2.0
                ),
                TranscriptSegment("这个结论引用了旧方案。", 2.0, 4.0),
                TranscriptSegment("但旧方案指哪个版本并不清楚。", 4.0, 6.0),
                TranscriptSegment("下一步需要人工复核。", 6.0, 8.0),
            ),
            engine="stub",
            model="test-model",
            language="zh",
        )


class StubSpeakerProvider:
    name = "stub-speakers"

    def attribute(self, audio: Path, transcript: Transcript) -> Transcript:
        self.audio = audio
        segments = tuple(
            TranscriptSegment(
                segment.text,
                segment.start,
                segment.end,
                "Speaker_1" if index < 3 else "Speaker_2",
            )
            for index, segment in enumerate(transcript.segments, start=1)
        )
        return Transcript(
            transcript.text,
            segments,
            transcript.engine,
            transcript.model,
            transcript.language,
        )


class StubAnalysisProvider:
    name = "stub-analysis"
    model = "deterministic-test"

    def analyze(self, transcript: Transcript) -> AnalysisResult:
        self.transcript = transcript
        return AnalysisResult(
            meeting_summary=MeetingSummary(
                topic="通用信息处理验证",
                participants=("Speaker_1", "Speaker_2"),
                discussion_goal="确认处理流程能保留上下文并进入人工审核。",
                main_discussion=("团队讨论了通用处理流程及人工复核。",),
                key_viewpoints=("上下文必须保留。",),
                agreements=("先验证通用流程。",),
                disagreements=(),
                unresolved_questions=("旧方案具体指哪个版本？",),
                next_actions=("由 Speaker_2 进行人工复核。",),
            ),
            atomic_notes=(
                AtomicNoteCandidate(
                    "团队决定先验证通用流程。",
                    "decision",
                    ("通用流程",),
                    (1,),
                    "讨论同时强调保留上下文。",
                    0.94,
                ),
                AtomicNoteCandidate(
                    "通用流程必须保留上下文。",
                    "requirement",
                    ("通用流程", "上下文"),
                    (1,),
                    "该要求与流程验证决定同时提出。",
                    0.91,
                ),
                AtomicNoteCandidate(
                    "团队决定验证流程并安排人工复核。",
                    "action",
                    ("团队", "人工复核"),
                    (1, 4),
                    "决定和后续动作跨越两个转写片段。",
                    0.88,
                ),
            ),
            residue=(
                ResidueItem(
                    (2, 3),
                    "旧方案的指代不明确，无法安全形成独立陈述。",
                    "确认版本后可能形成关于方案依据的原子信息。",
                ),
            ),
        )


class FailingAnalysisProvider:
    name = "failing-analysis"

    def analyze(self, transcript: Transcript) -> AnalysisResult:
        del transcript
        raise RuntimeError("runtime unavailable")


class FailingSpeakerProvider:
    name = "failing-speakers"

    def attribute(self, audio: Path, transcript: Transcript) -> Transcript:
        del audio, transcript
        raise RuntimeError("diarization unavailable")


def write_silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 1_600)


def run_pipeline(audio: Path, output: Path) -> Path:
    return process_audio(
        audio,
        output,
        StubTranscriptionProvider(),
        StubSpeakerProvider(),
        StubAnalysisProvider(),
        processed_at=FIXED_TIME,
    )


class PipelineTest(unittest.TestCase):
    def test_creates_review_package_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "讨论.wav"
            write_silent_wav(audio)
            before = (audio.stat().st_mtime_ns, hashlib.sha256(audio.read_bytes()).hexdigest())

            package = run_pipeline(audio, root / "02_processing")

            self.assertEqual(set(ARTIFACTS), {item.name for item in package.iterdir()})
            after = (audio.stat().st_mtime_ns, hashlib.sha256(audio.read_bytes()).hexdigest())
            self.assertEqual(before, after)

            manifest = json.loads((package / "manifest.json").read_text())
            self.assertEqual(manifest["artifacts"], list(ARTIFACTS))
            self.assertEqual(manifest["review"]["status"], "awaiting_human_review")
            self.assertFalse(manifest["review"]["automatic_core_write"])
            self.assertEqual(manifest["speaker_attribution"]["provider"], "stub-speakers")
            self.assertFalse(manifest["speaker_attribution"]["identity_matching"])
            self.assertEqual(
                manifest["counts"],
                {"transcript_segments": 4, "atomic_notes": 3, "residue_items": 1},
            )

            notes = [
                json.loads(line)
                for line in (package / "atomic_notes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [note["semantic_type"] for note in notes],
                ["decision", "requirement", "action"],
            )
            self.assertEqual(notes[0]["source_evidence"][0]["segment"], 1)
            self.assertEqual(notes[0]["source_evidence"][0]["speaker"], "Speaker_1")
            self.assertEqual(notes[0]["source_evidence"][0]["start"], "00:00:00.000")
            self.assertEqual(notes[0]["source_evidence"][0]["end"], "00:00:02.000")
            self.assertEqual(
                notes[0]["source_evidence"][0]["excerpt"],
                "我们决定先验证通用流程，同时要求保留上下文。",
            )
            self.assertEqual(
                notes[0]["source_evidence"][0]["source_id"],
                manifest["source"]["id"],
            )
            self.assertEqual(notes[1]["source_evidence"][0]["segment"], 1)
            self.assertEqual(
                [evidence["segment"] for evidence in notes[2]["source_evidence"]],
                [1, 4],
            )
            self.assertTrue(all(note["concerns"] for note in notes))
            self.assertTrue(all(note["status"] == "proposed" for note in notes))

            transcript = (package / "transcript.md").read_text()
            self.assertIn("Speaker_1", transcript)
            self.assertIn("Speaker_2", transcript)
            summary = (package / "meeting_summary.md").read_text()
            for heading in (
                "# Basic Information",
                "# Discussion Goal",
                "# Main Discussion",
                "# Key Viewpoints",
                "# Agreements / Consensus",
                "# Disagreements",
                "# Unresolved Questions",
                "# Next Actions",
            ):
                self.assertIn(heading, summary)
            self.assertIn("旧方案具体指哪个版本？", summary)
            residue = (package / "residue.md").read_text()
            self.assertIn("这个结论引用了旧方案。", residue)
            self.assertIn("指代不明确", residue)
            self.assertFalse((root / "03_notes").exists())
            self.assertFalse((root / "04_core").exists())
            self.assertFalse((root / "05_decisions").exists())

    def test_deterministic_providers_produce_byte_identical_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)
            first = run_pipeline(audio, root / "first")
            second = run_pipeline(audio, root / "second")
            for artifact in ARTIFACTS:
                self.assertEqual(
                    (first / artifact).read_bytes(),
                    (second / artifact).read_bytes(),
                    artifact,
                )

    def test_rejects_unsupported_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "discussion.mp4"
            audio.write_bytes(b"not used")
            with self.assertRaisesRegex(ProcessingError, "unsupported audio format"):
                run_pipeline(audio, Path(temp) / "out")

    def test_refuses_to_overwrite_existing_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)
            output = root / "out"
            run_pipeline(audio, output)
            with self.assertRaisesRegex(ProcessingError, "already exists"):
                run_pipeline(audio, output)

    def test_rejects_invalid_audio_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "discussion.wav"
            audio.write_bytes(b"not audio")
            with self.assertRaisesRegex(ProcessingError, "invalid audio input"):
                run_pipeline(audio, Path(temp) / "out")

    def test_runtime_failure_does_not_create_partial_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)
            output = root / "out"
            with self.assertRaisesRegex(ProcessingError, "runtime unavailable"):
                process_audio(
                    audio,
                    output,
                    StubTranscriptionProvider(),
                    StubSpeakerProvider(),
                    FailingAnalysisProvider(),
                )
            self.assertFalse(output.exists())

    def test_diarization_failure_does_not_create_partial_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)
            output = root / "out"
            with self.assertRaisesRegex(ProcessingError, "diarization unavailable"):
                process_audio(
                    audio,
                    output,
                    StubTranscriptionProvider(),
                    FailingSpeakerProvider(),
                    StubAnalysisProvider(),
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
