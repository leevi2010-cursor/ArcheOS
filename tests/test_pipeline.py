from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import unittest
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import archeos.pipeline as pipeline_module
from archeos.analysis import (
    AnalysisResult,
    AtomicInformationCandidate,
    MeetingSummary,
    ResidueItem,
)
from archeos.atomic_information import (
    JsonlAtomicInformationStore,
    ingest_processing_package,
)
from archeos.pipeline import ARTIFACTS, ProcessingError, process_managed_audio
from archeos.source import (
    LocalManagedSourceRepository,
    ManagedSource,
    VerificationResult,
)
from archeos.transcription import Transcript, TranscriptSegment

FIXED_TIME = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
SOURCE_ID = "src_" + "a" * 32


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
            atomic_information_candidates=(
                AtomicInformationCandidate(
                    "团队决定先验证通用流程。",
                    "decision",
                    ("通用流程",),
                    (1,),
                    "讨论同时强调保留上下文。",
                    0.94,
                ),
                AtomicInformationCandidate(
                    "通用流程必须保留上下文。",
                    "requirement",
                    ("通用流程", "上下文"),
                    (1,),
                    "该要求与流程验证决定同时提出。",
                    0.91,
                ),
                AtomicInformationCandidate(
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


class FailingTranscriptionProvider:
    def transcribe(self, audio: Path) -> Transcript:
        del audio
        raise RuntimeError("transcription unavailable")


class MutatingAnalysisProvider:
    name = "mutating-analysis"

    def __init__(self, audio: Path) -> None:
        self.audio = audio

    def analyze(self, transcript: Transcript) -> AnalysisResult:
        self.audio.write_bytes(self.audio.read_bytes() + b"changed")
        return StubAnalysisProvider().analyze(transcript)


class MutatingTranscriptionProvider:
    def transcribe(self, audio: Path) -> Transcript:
        self.audio = audio
        os.chmod(audio, 0o600)
        audio.write_bytes(b"tampered runtime bytes")
        return StubTranscriptionProvider().transcribe(audio)


class IncompleteAnalysisProvider:
    name = "incomplete-analysis"

    def analyze(self, transcript: Transcript) -> AnalysisResult:
        result = StubAnalysisProvider().analyze(transcript)
        return AnalysisResult(
            result.meeting_summary,
            result.atomic_information_candidates,
            (),
        )


class OverlappingAnalysisProvider:
    name = "overlapping-analysis"

    def analyze(self, transcript: Transcript) -> AnalysisResult:
        result = StubAnalysisProvider().analyze(transcript)
        return AnalysisResult(
            result.meeting_summary,
            result.atomic_information_candidates,
            (
                ResidueItem(
                    (1, 2, 3),
                    "第一段仍有部分上下文无法安全吸收。",
                    "人工复核后可能补充原子信息的适用范围。",
                ),
            ),
        )


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


class TestManagedSourceAccess:
    def __init__(self, audio: Path, *, source_id: str = SOURCE_ID) -> None:
        self.audio = audio
        self.source = ManagedSource(
            source_id=source_id,
            content_hash=f"sha256:{hashlib.sha256(audio.read_bytes()).hexdigest()}",
            size_bytes=audio.stat().st_size,
            media_type="audio/wav",
            filename_hint=audio.name,
            managed_locator=f"sources/{source_id}/original.wav",
            archived_at="2026-08-10T00:00:00Z",
            availability="available",
        )
        self.closed = False

    def get(self, source_id: str) -> ManagedSource:
        if source_id != self.source.source_id:
            raise RuntimeError("Managed Source was not found")
        return self.source

    def verify(self, source_id: str) -> VerificationResult:
        source = self.get(source_id)
        observed_hash = f"sha256:{hashlib.sha256(self.audio.read_bytes()).hexdigest()}"
        observed_size = self.audio.stat().st_size
        verified = (
            source.availability == "available"
            and observed_hash == source.content_hash
            and observed_size == source.size_bytes
        )
        return VerificationResult(
            source_id=source.source_id,
            verified=verified,
            expected_content_hash=source.content_hash,
            observed_content_hash=observed_hash,
            expected_size_bytes=source.size_bytes,
            observed_size_bytes=observed_size,
            checked_at="2026-08-10T00:00:00Z",
            reason=None if verified else "managed bytes do not match the immutable Manifest",
        )

    @contextmanager
    def materialize(self, source_id: str):
        self.get(source_id)
        try:
            yield self.audio
        finally:
            self.closed = True


class FinalVerificationFailureAccess(TestManagedSourceAccess):
    def __init__(self, audio: Path) -> None:
        super().__init__(audio)
        self.verify_calls = 0

    def verify(self, source_id: str) -> VerificationResult:
        self.verify_calls += 1
        result = super().verify(source_id)
        if self.verify_calls == 3:
            return VerificationResult(
                source_id=result.source_id,
                verified=False,
                expected_content_hash=result.expected_content_hash,
                observed_content_hash=result.observed_content_hash,
                expected_size_bytes=result.expected_size_bytes,
                observed_size_bytes=result.observed_size_bytes,
                checked_at=result.checked_at,
                reason="synthetic final verification failure",
            )
        return result


class InvalidReturnedSourceAccess(TestManagedSourceAccess):
    def get(self, source_id: str) -> ManagedSource:
        del source_id
        return self.source


def process_audio(
    audio: Path,
    output: Path,
    transcription_provider: object,
    speaker_provider: object,
    analysis_provider: object,
    *,
    processed_at: datetime | None = None,
) -> Path:
    """Legacy-shaped test helper; production has no external-path entrypoint."""

    access = TestManagedSourceAccess(audio)
    return process_managed_audio(
        SOURCE_ID,
        access,
        output,
        transcription_provider,  # type: ignore[arg-type]
        speaker_provider,  # type: ignore[arg-type]
        analysis_provider,  # type: ignore[arg-type]
        processed_at=processed_at,
    )


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
            before = (
                audio.stat().st_mtime_ns,
                hashlib.sha256(audio.read_bytes()).hexdigest(),
            )

            package = run_pipeline(audio, root / "02_processing")

            self.assertEqual(set(ARTIFACTS), {item.name for item in package.iterdir()})
            after = (
                audio.stat().st_mtime_ns,
                hashlib.sha256(audio.read_bytes()).hexdigest(),
            )
            self.assertEqual(before, after)

            manifest = json.loads((package / "manifest.json").read_text())
            self.assertEqual(manifest["artifacts"], list(ARTIFACTS))
            self.assertEqual(manifest["schema_version"], "1.2")
            self.assertEqual(manifest["source"]["id"], SOURCE_ID)
            self.assertEqual(manifest["source"]["filename_hint"], "讨论.wav")
            self.assertNotIn("path", manifest["source"])
            self.assertNotIn(str(audio), json.dumps(manifest, ensure_ascii=False))
            self.assertEqual(
                manifest["downstream"],
                {
                    "atomic_information_ingestion": (
                        "automatic_after_contract_validation"
                    ),
                    "world_model_write": "governed",
                },
            )
            self.assertNotIn("review", manifest)
            self.assertEqual(
                manifest["speaker_attribution"]["provider"], "stub-speakers"
            )
            self.assertFalse(manifest["speaker_attribution"]["identity_matching"])
            self.assertEqual(
                manifest["counts"],
                {
                    "transcript_segments": 4,
                    "atomic_information_candidates": 3,
                    "atomic_information_candidate_segments": 2,
                    "residue_items": 1,
                    "residue_segments": 2,
                },
            )

            candidates = [
                json.loads(line)
                for line in (package / "atomic_information_candidates.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertTrue(all(item["status"] == "candidate" for item in candidates))
            self.assertEqual(
                [item["semantic_type"] for item in candidates],
                ["decision", "requirement", "action"],
            )
            self.assertEqual(candidates[0]["source_evidence"][0]["segment"], 1)
            self.assertEqual(
                candidates[0]["source_evidence"][0]["speaker"], "Speaker_1"
            )
            self.assertEqual(
                candidates[0]["source_evidence"][0]["start"], "00:00:00.000"
            )
            self.assertEqual(candidates[0]["source_evidence"][0]["end"], "00:00:02.000")
            self.assertEqual(
                candidates[0]["source_evidence"][0]["excerpt"],
                "我们决定先验证通用流程，同时要求保留上下文。",
            )
            self.assertEqual(
                candidates[0]["source_evidence"][0]["source_id"],
                manifest["source"]["id"],
            )
            self.assertEqual(candidates[1]["source_evidence"][0]["segment"], 1)
            self.assertEqual(
                [evidence["segment"] for evidence in candidates[2]["source_evidence"]],
                [1, 4],
            )
            self.assertTrue(all(item["concerns"] for item in candidates))

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
            self.assertFalse((root / "03_information").exists())
            self.assertFalse((root / "04_core").exists())
            self.assertFalse((root / "05_decisions").exists())

    def test_generated_package_is_ingestible_as_durable_information(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "synthetic.wav"
            write_silent_wav(audio)
            package = run_pipeline(audio, root / "02_processing")
            store = JsonlAtomicInformationStore(
                root / "03_information" / "atomic_information.jsonl"
            )

            result = ingest_processing_package(package, store)

            self.assertEqual(result.created, 3)
            self.assertEqual(len(store.list_atomic_information()), 3)
            self.assertTrue(
                all(
                    item.revision_number == 1
                    for item in store.list_atomic_information()
                )
            )
            self.assertTrue(
                all(item.source_evidence for item in store.list_atomic_information())
            )
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

    def test_reports_full_transcript_digestion_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)

            package = run_pipeline(audio, root / "out")
            manifest = json.loads((package / "manifest.json").read_text())

            self.assertEqual(
                manifest["digestion_coverage"],
                {
                    "total_segments": 4,
                    "accounted_segments": 4,
                    "unaccounted_segments": 0,
                    "overlap_segments": 0,
                },
            )
            self.assertEqual(
                manifest["counts"]["atomic_information_candidate_segments"]
                + manifest["counts"]["residue_segments"]
                - manifest["digestion_coverage"]["overlap_segments"],
                manifest["digestion_coverage"]["accounted_segments"],
            )

    def test_reports_overlapping_candidate_and_residue_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)

            package = process_audio(
                audio,
                root / "out",
                StubTranscriptionProvider(),
                StubSpeakerProvider(),
                OverlappingAnalysisProvider(),
                processed_at=FIXED_TIME,
            )
            manifest = json.loads((package / "manifest.json").read_text())

            self.assertEqual(
                manifest["counts"]["atomic_information_candidate_segments"], 2
            )
            self.assertEqual(manifest["counts"]["residue_segments"], 3)
            self.assertEqual(manifest["digestion_coverage"]["overlap_segments"], 1)
            self.assertEqual(manifest["digestion_coverage"]["accounted_segments"], 4)

    def test_missing_digestion_coverage_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)
            output = root / "out"

            with self.assertRaisesRegex(
                ProcessingError, "unaccounted transcript segments: 2, 3"
            ):
                process_audio(
                    audio,
                    output,
                    StubTranscriptionProvider(),
                    StubSpeakerProvider(),
                    IncompleteAnalysisProvider(),
                )

            self.assertFalse(output.exists())

    def test_meeting_time_is_not_inferred_from_source_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "discussion.wav"
            write_silent_wav(audio)

            package = run_pipeline(audio, root / "out")
            summary = (package / "meeting_summary.md").read_text()

            self.assertIn("Time: 待人工确认", summary)
            self.assertNotIn("Last modified", (package / "transcript.md").read_text())

    def test_rejects_unsupported_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "discussion.mp4"
            audio.write_bytes(b"not used")
            with self.assertRaisesRegex(ProcessingError, "unsupported Managed Source audio"):
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

    def test_transcription_failure_and_source_change_during_processing_do_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for label, transcriber, analysis in (
                (
                    "transcription-failure",
                    FailingTranscriptionProvider(),
                    StubAnalysisProvider(),
                ),
                (
                    "source-changed",
                    StubTranscriptionProvider(),
                    MutatingAnalysisProvider(root / "source-changed.wav"),
                ),
            ):
                with self.subTest(label=label):
                    audio = root / f"{label}.wav"
                    write_silent_wav(audio)
                    access = TestManagedSourceAccess(audio)
                    if label == "source-changed":
                        analysis = MutatingAnalysisProvider(audio)
                    with self.assertRaises(ProcessingError):
                        process_managed_audio(
                            SOURCE_ID,
                            access,
                            root / label,
                            transcriber,
                            StubSpeakerProvider(),
                            analysis,
                        )
                    self.assertFalse((root / label).exists())

    def test_real_local_materialization_is_isolated_and_cleanup_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root / "synthetic.wav"
            write_silent_wav(external)
            repository = LocalManagedSourceRepository(
                root / "managed", id_factory=lambda: SOURCE_ID
            )
            source = repository.admit(external).source
            canonical = root / "managed" / source.managed_locator
            before = (canonical.read_bytes(), canonical.stat().st_size)
            transcriber = StubTranscriptionProvider()

            package = process_managed_audio(
                source.source_id,
                repository,
                root / "out",
                transcriber,
                StubSpeakerProvider(),
                StubAnalysisProvider(),
                processed_at=FIXED_TIME,
            )

            self.assertNotEqual(transcriber.audio, canonical)
            self.assertFalse(transcriber.audio.exists())
            self.assertTrue(package.is_dir())
            self.assertTrue(repository.verify(source.source_id).verified)
            self.assertEqual(before, (canonical.read_bytes(), canonical.stat().st_size))

    def test_real_local_provider_mutation_fails_and_preserves_canonical_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root / "synthetic.wav"
            write_silent_wav(external)
            repository = LocalManagedSourceRepository(
                root / "managed", id_factory=lambda: SOURCE_ID
            )
            source = repository.admit(external).source
            canonical = root / "managed" / source.managed_locator
            before = (canonical.read_bytes(), canonical.stat().st_size)
            transcriber = MutatingTranscriptionProvider()

            with self.assertRaisesRegex(ProcessingError, "materialized audio changed"):
                process_managed_audio(
                    source.source_id,
                    repository,
                    root / "out",
                    transcriber,
                    StubSpeakerProvider(),
                    StubAnalysisProvider(),
                )

            self.assertFalse((root / "out" / source.source_id).exists())
            self.assertFalse(transcriber.audio.exists())
            self.assertTrue(repository.verify(source.source_id).verified)
            self.assertEqual(before, (canonical.read_bytes(), canonical.stat().st_size))

    def test_final_verification_failure_before_publish_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "synthetic.wav"
            write_silent_wav(audio)
            access = FinalVerificationFailureAccess(audio)
            output = root / "out"

            with self.assertRaisesRegex(ProcessingError, "final verification"):
                process_managed_audio(
                    SOURCE_ID,
                    access,
                    output,
                    StubTranscriptionProvider(),
                    StubSpeakerProvider(),
                    StubAnalysisProvider(),
                )

            self.assertFalse((output / SOURCE_ID).exists())
            self.assertEqual(list(output.glob(f".{SOURCE_ID}-*")), [])

    def test_atomic_publish_race_never_replaces_any_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for target_kind in ("directory", "file", "broken-symlink"):
                with self.subTest(target_kind=target_kind):
                    audio = root / f"{target_kind}.wav"
                    write_silent_wav(audio)
                    access = TestManagedSourceAccess(audio)
                    output = root / target_kind
                    target = output / SOURCE_ID
                    real_publish = pipeline_module.publish_directory_no_replace

                    def race_publish(staging: Path, final: Path) -> None:
                        final.parent.mkdir(parents=True, exist_ok=True)
                        if target_kind == "directory":
                            final.mkdir()
                            (final / "marker").write_text("keep", encoding="utf-8")
                        elif target_kind == "file":
                            final.write_text("keep", encoding="utf-8")
                        else:
                            final.symlink_to("missing-target")
                        real_publish(staging, final)

                    with patch.object(
                        pipeline_module,
                        "publish_directory_no_replace",
                        side_effect=race_publish,
                    ), self.assertRaises(ProcessingError):
                        process_managed_audio(
                            SOURCE_ID,
                            access,
                            output,
                            StubTranscriptionProvider(),
                            StubSpeakerProvider(),
                            StubAnalysisProvider(),
                        )

                    self.assertEqual(list(output.glob(f".{SOURCE_ID}-*")), [])
                    if target_kind == "directory":
                        self.assertEqual((target / "marker").read_text(), "keep")
                    elif target_kind == "file":
                        self.assertEqual(target.read_text(), "keep")
                    else:
                        self.assertTrue(target.is_symlink())
                        self.assertFalse(target.exists())

    def test_invalid_managed_source_ids_fail_before_access_or_output_creation(self) -> None:
        invalid_ids = (
            "",
            "..",
            "../escape",
            "/absolute/path",
            "src_x/child",
            "src_" + "a" * 31 + "\\",
            "src_" + "A" * 32,
            "src_" + "a" * 31,
            "src_" + "a" * 33,
            "src_" + "g" * 32,
            "a" * 32,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "synthetic.wav"
            write_silent_wav(audio)
            for invalid_id in invalid_ids:
                with self.subTest(source_id=invalid_id):
                    access = TestManagedSourceAccess(audio)
                    output = root / hashlib.sha256(invalid_id.encode()).hexdigest()
                    with self.assertRaises(ProcessingError):
                        process_managed_audio(
                            invalid_id,
                            access,
                            output,
                            StubTranscriptionProvider(),
                            StubSpeakerProvider(),
                            StubAnalysisProvider(),
                        )
                    self.assertFalse(output.exists())

    def test_invalid_source_id_returned_by_access_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "synthetic.wav"
            write_silent_wav(audio)
            access = InvalidReturnedSourceAccess(audio)
            object.__setattr__(access.source, "source_id", "../escape")

            with self.assertRaises(ProcessingError):
                process_managed_audio(
                    SOURCE_ID,
                    access,
                    root / "out",
                    StubTranscriptionProvider(),
                    StubSpeakerProvider(),
                    StubAnalysisProvider(),
                )
            self.assertFalse((root / "out").exists())

    def test_managed_source_access_is_closed_and_never_leaks_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "synthetic.wav"
            write_silent_wav(audio)
            access = TestManagedSourceAccess(audio)

            package = process_managed_audio(
                SOURCE_ID,
                access,
                root / "out",
                StubTranscriptionProvider(),
                StubSpeakerProvider(),
                StubAnalysisProvider(),
                processed_at=FIXED_TIME,
            )

            self.assertTrue(access.closed)
            rendered = "\n".join(
                path.read_text(encoding="utf-8")
                for path in package.iterdir()
                if path.suffix in {".md", ".json", ".jsonl"}
            )
            self.assertNotIn(str(audio), rendered)
            self.assertNotIn("ingested_from", rendered)

    def test_unknown_unavailable_or_changed_managed_source_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "synthetic.wav"
            write_silent_wav(audio)

            for label, prepare in (
                ("unknown", lambda access: "src_" + "b" * 32),
                (
                    "unavailable",
                    lambda access: object.__setattr__(
                        access.source, "availability", "unavailable"
                    ),
                ),
                ("changed", lambda access: audio.write_bytes(b"changed")),
            ):
                with self.subTest(label=label):
                    access = TestManagedSourceAccess(audio)
                    requested = SOURCE_ID
                    result = prepare(access)
                    if isinstance(result, str):
                        requested = result
                    with self.assertRaises(ProcessingError):
                        process_managed_audio(
                            requested,
                            access,
                            root / label,
                            StubTranscriptionProvider(),
                            StubSpeakerProvider(),
                            StubAnalysisProvider(),
                        )
                    self.assertFalse((root / label).exists())

    def test_all_supported_audio_suffixes_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for suffix in (".wav", ".mp3", ".m4a"):
                with self.subTest(suffix=suffix):
                    audio = root / f"synthetic{suffix}"
                    write_silent_wav(audio)
                    package = run_pipeline(audio, root / suffix[1:])
                    self.assertEqual(package.name, SOURCE_ID)

    def test_pipeline_depends_on_source_access_not_local_storage_or_tos(self) -> None:
        import archeos.pipeline as pipeline

        source = inspect.getsource(pipeline)
        self.assertIn("ManagedSourceAccess", source)
        self.assertNotIn("LocalManagedSourceRepository", source)
        self.assertNotIn("tos", source.lower())


if __name__ == "__main__":
    unittest.main()
