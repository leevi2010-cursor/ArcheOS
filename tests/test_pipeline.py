from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path

from archeos.pipeline import ARTIFACTS, ProcessingError, process_audio
from archeos.transcription import Transcript, TranscriptSegment


class StubTranscriber:
    def transcribe(self, audio: Path) -> Transcript:
        self.audio = audio
        return Transcript(
            text="我们决定先验证通用流程。这个结论是否缺少证据？嗯。下一步需要人工复核。",
            segments=(
                TranscriptSegment("我们决定先验证通用流程。", 0.0, 2.0),
                TranscriptSegment("这个结论是否缺少证据？", 2.0, 4.0),
                TranscriptSegment("嗯。", 4.0, 4.5),
                TranscriptSegment("下一步需要人工复核。", 4.5, 7.0),
            ),
            engine="stub",
            model="test-model",
            language="zh",
        )


def write_silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 1_600)


class PipelineTest(unittest.TestCase):
    def test_creates_review_package_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "讨论.wav"
            write_silent_wav(audio)
            before = (audio.stat().st_mtime_ns, hashlib.sha256(audio.read_bytes()).hexdigest())

            package = process_audio(audio, root / "02_processing", StubTranscriber())

            self.assertEqual(set(ARTIFACTS), {item.name for item in package.iterdir()})
            after = (audio.stat().st_mtime_ns, hashlib.sha256(audio.read_bytes()).hexdigest())
            self.assertEqual(before, after)

            manifest = json.loads((package / "manifest.json").read_text())
            self.assertEqual(manifest["artifacts"], list(ARTIFACTS))
            self.assertEqual(manifest["review"]["status"], "awaiting_human_review")
            self.assertFalse(manifest["review"]["automatic_core_write"])
            self.assertEqual(manifest["counts"], {"atomic_notes": 3, "residue_items": 1})

            notes = [
                json.loads(line)
                for line in (package / "atomic_notes.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [note["semantic_type"] for note in notes],
                ["decision", "question", "action"],
            )
            self.assertTrue(all(note["source_evidence"]["excerpt"] for note in notes))
            self.assertTrue(all(note["context"] for note in notes))
            self.assertTrue(all(0 <= note["confidence"] <= 1 for note in notes))
            self.assertTrue(all(note["status"] == "proposed" for note in notes))

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
            self.assertIn("这个结论是否缺少证据？", summary)
            residue = (package / "residue.md").read_text()
            self.assertIn("嗯。", residue)
            self.assertFalse((root / "03_notes").exists())
            self.assertFalse((root / "04_core").exists())
            self.assertFalse((root / "05_decisions").exists())

    def test_same_input_produces_byte_identical_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)

            first = process_audio(audio, root / "first", StubTranscriber())
            second = process_audio(audio, root / "second", StubTranscriber())

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
                process_audio(audio, Path(temp) / "out", StubTranscriber())

    def test_refuses_to_overwrite_existing_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)
            output = root / "out"
            process_audio(audio, output, StubTranscriber())
            with self.assertRaisesRegex(ProcessingError, "already exists"):
                process_audio(audio, output, StubTranscriber())

    def test_rejects_invalid_audio_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "discussion.wav"
            audio.write_bytes(b"not audio")
            with self.assertRaisesRegex(ProcessingError, "invalid audio input"):
                process_audio(audio, Path(temp) / "out", StubTranscriber())


if __name__ == "__main__":
    unittest.main()
