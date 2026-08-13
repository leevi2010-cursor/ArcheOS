from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .analysis import AnalysisProvider, AnalysisResult
from .filesystem import publish_directory_no_replace
from .source import ManagedSource, ManagedSourceAccess
from .source.identity import require_managed_source_id
from .speakers import SpeakerProvider
from .transcription import Transcript, TranscriptionProvider, TranscriptSegment

SUPPORTED_AUDIO = {".m4a", ".mp3", ".wav"}
ARTIFACTS = (
    "manifest.json",
    "transcript.md",
    "meeting_summary.md",
    "atomic_information_candidates.jsonl",
    "residue.md",
)


class ProcessingError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_audio(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ProcessingError("ffmpeg is required to validate audio input")
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-t",
            "0.1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or "no decodable audio stream"
        raise ProcessingError(f"invalid audio input: {detail}")


def _timestamp(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _excerpt(segments: tuple[TranscriptSegment, ...], refs: tuple[int, ...]) -> str:
    return " ".join(segments[ref - 1].text.strip() for ref in refs)


def _evidence(
    source_id: str,
    segments: tuple[TranscriptSegment, ...],
    refs: tuple[int, ...],
) -> list[dict[str, object]]:
    return [
        {
            "source_id": source_id,
            "artifact": "transcript.md",
            "segment": ref,
            "speaker": segments[ref - 1].speaker,
            "start": _timestamp(segments[ref - 1].start),
            "end": _timestamp(segments[ref - 1].end),
            "excerpt": segments[ref - 1].text.strip(),
        }
        for ref in refs
    ]


def _digestion_coverage(
    transcript: Transcript,
    analysis: AnalysisResult,
) -> dict[str, int]:
    expected = {
        index
        for index, segment in enumerate(transcript.segments, start=1)
        if segment.text.strip()
    }
    atomic_information_candidate_segments = {
        reference
        for item in analysis.atomic_information_candidates
        for reference in item.evidence_segments
        if reference in expected
    }
    residue_segments = {
        reference
        for item in analysis.residue
        for reference in item.evidence_segments
        if reference in expected
    }
    accounted = atomic_information_candidate_segments | residue_segments
    unaccounted = sorted(expected - accounted)
    if unaccounted:
        references = ", ".join(str(reference) for reference in unaccounted)
        raise ValueError(f"unaccounted transcript segments: {references}")
    return {
        "total_segments": len(expected),
        "atomic_information_candidate_segments": len(
            atomic_information_candidate_segments
        ),
        "residue_segments": len(residue_segments),
        "accounted_segments": len(accounted),
        "unaccounted_segments": 0,
        "overlap_segments": len(
            atomic_information_candidate_segments & residue_segments
        ),
    }


def _transcript_markdown(source: dict[str, object], transcript: Transcript) -> str:
    has_speakers = any(segment.speaker for segment in transcript.segments)
    lines = [
        "# Transcript",
        "",
        "## Source Metadata",
        "",
        f"- Source ID: `{source['id']}`",
        f"- Filename hint: `{source['filename_hint']}`",
        f"- SHA-256: `{source['content_hash']}`",
        f"- Size: {source['size_bytes']} bytes",
        f"- Media type: `{source['media_type']}`",
        f"- Processed at: {source['processed_at']}",
        f"- Transcription engine: `{transcript.engine}`",
        f"- Model: `{transcript.model or 'not reported'}`",
        f"- Language: `{transcript.language or 'not reported'}`",
        f"- Speaker labels: {'available' if has_speakers else 'unavailable'}",
        "- Speaker identities: not inferred",
        "",
        "## Original Transcription",
        "",
    ]
    for index, segment in enumerate(transcript.segments, start=1):
        start, end = _timestamp(segment.start), _timestamp(segment.end)
        timing = (
            f"{start} → {end}"
            if start is not None and end is not None
            else "time unavailable"
        )
        speaker = f" {segment.speaker}" if segment.speaker else ""
        lines.append(f"[{index} | {timing}]{speaker}: {segment.text.strip()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bullets(items: tuple[str, ...], *, empty: str = "待人工确认") -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def _summary_markdown(analysis: AnalysisResult) -> str:
    summary = analysis.meeting_summary
    participants = (
        "、".join(summary.participants) if summary.participants else "待人工确认"
    )
    return f"""# Basic Information

Topic: {summary.topic}
Time: 待人工确认
Participants: {participants}

# Discussion Goal

{summary.discussion_goal}

# Main Discussion

{_bullets(summary.main_discussion)}

# Key Viewpoints

{_bullets(summary.key_viewpoints)}

# Agreements / Consensus

{_bullets(summary.agreements)}

# Disagreements

{_bullets(summary.disagreements)}

# Unresolved Questions

{_bullets(summary.unresolved_questions)}

# Next Actions

{_bullets(summary.next_actions)}
"""


def _atomic_information_candidates_jsonl(
    source: dict[str, object],
    transcript: Transcript,
    analysis: AnalysisResult,
) -> str:
    candidates: list[str] = []
    for index, candidate in enumerate(analysis.atomic_information_candidates, start=1):
        payload = {
            "id": f"{source['id']}-{index:04d}",
            "statement": candidate.statement,
            "semantic_type": candidate.semantic_type,
            "concerns": list(candidate.concerns),
            "source_evidence": _evidence(
                str(source["id"]),
                transcript.segments,
                candidate.evidence_segments,
            ),
            "context": candidate.context,
            "confidence": candidate.confidence,
            "processing_time": source["processed_at"],
            "status": "candidate",
        }
        candidates.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(candidates) + ("\n" if candidates else "")


def _residue_markdown(
    source_id: str,
    transcript: Transcript,
    analysis: AnalysisResult,
) -> str:
    lines = [
        "# Residue",
        "",
        f"Source ID: `{source_id}`",
        "",
        "Residue is retained for human review and digestion-model improvement.",
        "",
    ]
    if not analysis.residue:
        lines.append("No residue was identified by the analysis provider.")
    else:
        for index, item in enumerate(analysis.residue, start=1):
            refs = ", ".join(str(ref) for ref in item.evidence_segments)
            lines.extend(
                [
                    f"## Item {index}",
                    "",
                    f"- Source segments: {refs}",
                    f"- Original excerpt: {_excerpt(transcript.segments, item.evidence_segments)}",
                    f"- Reason not absorbed: {item.reason_not_absorbed}",
                    (
                        "- Possible future value or uncertainty: "
                        f"{item.future_value_or_uncertainty}"
                    ),
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _source_snapshot(audio: Path) -> tuple[int, str]:
    if not audio.is_file():
        raise ProcessingError("materialized audio is missing or not a regular file")
    return audio.stat().st_size, f"sha256:{_sha256(audio)}"


def _validate_managed_source(source: ManagedSource) -> None:
    if source.availability != "available":
        raise ProcessingError("Managed Source is unavailable")
    if (
        len(source.content_hash) != len("sha256:") + 64
        or not source.content_hash.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in source.content_hash[7:])
        or source.size_bytes < 0
    ):
        raise ProcessingError("Managed Source Manifest has invalid byte metadata")
    filename_hint = Path(source.filename_hint)
    if (
        filename_hint.is_absolute()
        or filename_hint.name != source.filename_hint
        or "\\" in source.filename_hint
    ):
        raise ProcessingError("Managed Source filename hint must not contain a path")
    suffix = filename_hint.suffix.lower()
    if suffix not in SUPPORTED_AUDIO or not source.media_type.startswith("audio/"):
        supported = ", ".join(sorted(SUPPORTED_AUDIO))
        raise ProcessingError(
            f"unsupported Managed Source audio; expected audio media type and one of: {supported}"
        )


def process_managed_audio(
    source_id: str,
    source_access: ManagedSourceAccess,
    output_root: Path,
    transcription_provider: TranscriptionProvider,
    speaker_provider: SpeakerProvider,
    analysis_provider: AnalysisProvider,
    *,
    processed_at: datetime | None = None,
) -> Path:
    output_root = output_root.expanduser().resolve()
    try:
        source_id = require_managed_source_id(source_id)
        source = source_access.get(source_id)
        require_managed_source_id(
            source.source_id, field="Managed Source access result source_id"
        )
        if source.source_id != source_id:
            raise ValueError("Managed Source access returned a different source_id")
        _validate_managed_source(source)
        before_verification = source_access.verify(source_id)
        if not before_verification.verified:
            raise ValueError("Managed Source failed verification")
        package = output_root / source.source_id
        if os.path.lexists(package):
            raise ValueError(f"processing package already exists: {package}")
        with source_access.materialize(source.source_id) as audio:
            before = _source_snapshot(audio)
            if before != (source.size_bytes, source.content_hash):
                raise ValueError("materialized bytes do not match the Managed Source")
            _validate_audio(audio)
            transcript = transcription_provider.transcribe(audio)
            transcript = speaker_provider.attribute(audio, transcript)
            if not transcript.segments:
                raise ValueError("transcription did not contain any segments")
            analysis = analysis_provider.analyze(transcript)
            digestion_coverage = _digestion_coverage(transcript, analysis)
            after = _source_snapshot(audio)
            if after != before:
                raise ValueError("materialized audio changed during processing")
        after_verification = source_access.verify(source.source_id)
        if not after_verification.verified:
            raise ValueError("Managed Source changed during processing")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ProcessingError(str(exc)) from exc

    processing_time = processed_at or datetime.now(timezone.utc)
    source = {
        "id": source.source_id,
        "content_hash": source.content_hash,
        "size_bytes": source.size_bytes,
        "media_type": source.media_type,
        "filename_hint": source.filename_hint,
        "processed_at": processing_time.astimezone(timezone.utc).isoformat(),
    }
    manifest = {
        "schema_version": "1.2",
        "pipeline_version": __version__,
        "source": source,
        "transcription": {
            "provider": transcription_provider.__class__.__name__,
            "engine": transcript.engine,
            "model": transcript.model,
            "language": transcript.language,
        },
        "speaker_attribution": {
            "provider": speaker_provider.name,
            "labels_available": any(segment.speaker for segment in transcript.segments),
            "identity_matching": False,
        },
        "analysis": {
            "provider": analysis_provider.name,
        },
        "artifacts": list(ARTIFACTS),
        "counts": {
            "transcript_segments": len(transcript.segments),
            "atomic_information_candidates": len(
                analysis.atomic_information_candidates
            ),
            "atomic_information_candidate_segments": digestion_coverage[
                "atomic_information_candidate_segments"
            ],
            "residue_items": len(analysis.residue),
            "residue_segments": digestion_coverage["residue_segments"],
        },
        "digestion_coverage": {
            key: digestion_coverage[key]
            for key in (
                "total_segments",
                "accounted_segments",
                "unaccounted_segments",
                "overlap_segments",
            )
        },
        "downstream": {
            "atomic_information_ingestion": ("automatic_after_contract_validation"),
            "world_model_write": "governed",
        },
    }

    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{source_id}-", dir=output_root) as temp:
        staging = Path(temp)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / "transcript.md").write_text(
            _transcript_markdown(source, transcript), encoding="utf-8"
        )
        (staging / "meeting_summary.md").write_text(
            _summary_markdown(analysis), encoding="utf-8"
        )
        (staging / "atomic_information_candidates.jsonl").write_text(
            _atomic_information_candidates_jsonl(source, transcript, analysis),
            encoding="utf-8",
        )
        (staging / "residue.md").write_text(
            _residue_markdown(source_id, transcript, analysis), encoding="utf-8"
        )
        final_verification = source_access.verify(source_id)
        if not final_verification.verified:
            raise ProcessingError("Managed Source failed final verification")
        try:
            publish_directory_no_replace(staging, package)
        except (FileExistsError, OSError) as exc:
            raise ProcessingError("processing package already exists or could not publish safely") from exc
    return package
