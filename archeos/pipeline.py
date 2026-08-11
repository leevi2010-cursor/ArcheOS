from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .analysis import AnalysisProvider, AnalysisResult
from .speakers import SpeakerProvider
from .transcription import Transcript, TranscriptionProvider, TranscriptSegment

SUPPORTED_AUDIO = {".m4a", ".mp3", ".wav"}
ARTIFACTS = (
    "manifest.json",
    "transcript.md",
    "meeting_summary.md",
    "atomic_notes.jsonl",
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


def _source_id(path: Path, digest: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem).strip("-").lower()
    return f"{stem or 'audio'}-{digest[:12]}"


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
    atomic_note_segments = {
        reference
        for item in analysis.atomic_notes
        for reference in item.evidence_segments
        if reference in expected
    }
    residue_segments = {
        reference
        for item in analysis.residue
        for reference in item.evidence_segments
        if reference in expected
    }
    accounted = atomic_note_segments | residue_segments
    unaccounted = sorted(expected - accounted)
    if unaccounted:
        references = ", ".join(str(reference) for reference in unaccounted)
        raise ValueError(f"unaccounted transcript segments: {references}")
    return {
        "total_segments": len(expected),
        "atomic_note_segments": len(atomic_note_segments),
        "residue_segments": len(residue_segments),
        "accounted_segments": len(accounted),
        "unaccounted_segments": 0,
        "overlap_segments": len(atomic_note_segments & residue_segments),
    }


def _transcript_markdown(source: dict[str, object], transcript: Transcript) -> str:
    has_speakers = any(segment.speaker for segment in transcript.segments)
    lines = [
        "# Transcript",
        "",
        "## Source Metadata",
        "",
        f"- Source ID: `{source['id']}`",
        f"- File: `{source['path']}`",
        f"- SHA-256: `{source['sha256']}`",
        f"- Size: {source['size_bytes']} bytes",
        f"- Last modified: {source['modified_at']}",
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


def _atomic_notes_jsonl(
    source: dict[str, object],
    transcript: Transcript,
    analysis: AnalysisResult,
) -> str:
    notes: list[str] = []
    for index, note in enumerate(analysis.atomic_notes, start=1):
        payload = {
            "id": f"{source['id']}-{index:04d}",
            "statement": note.statement,
            "semantic_type": note.semantic_type,
            "concerns": list(note.concerns),
            "source_evidence": _evidence(
                str(source["id"]), transcript.segments, note.evidence_segments
            ),
            "context": note.context,
            "confidence": note.confidence,
            "processing_time": source["processed_at"],
            "status": "candidate",
        }
        notes.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(notes) + ("\n" if notes else "")


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


def process_audio(
    audio: Path,
    output_root: Path,
    transcription_provider: TranscriptionProvider,
    speaker_provider: SpeakerProvider,
    analysis_provider: AnalysisProvider,
    *,
    processed_at: datetime | None = None,
) -> Path:
    audio = audio.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not audio.is_file():
        raise ProcessingError(f"audio file not found: {audio}")
    if audio.suffix.lower() not in SUPPORTED_AUDIO:
        supported = ", ".join(sorted(SUPPORTED_AUDIO))
        raise ProcessingError(f"unsupported audio format; expected one of: {supported}")
    _validate_audio(audio)

    before = (audio.stat().st_size, audio.stat().st_mtime_ns, _sha256(audio))
    digest = before[2]
    source_id = _source_id(audio, digest)
    package = output_root / source_id
    if package.exists():
        raise ProcessingError(f"processing package already exists: {package}")

    try:
        transcript = transcription_provider.transcribe(audio)
        transcript = speaker_provider.attribute(audio, transcript)
        if not transcript.segments:
            raise ValueError("transcription did not contain any segments")
        analysis = analysis_provider.analyze(transcript)
        digestion_coverage = _digestion_coverage(transcript, analysis)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ProcessingError(str(exc)) from exc

    processing_time = processed_at or datetime.now(timezone.utc)
    source = {
        "id": source_id,
        "path": str(audio),
        "sha256": digest,
        "size_bytes": before[0],
        "modified_at": datetime.fromtimestamp(
            audio.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "processed_at": processing_time.astimezone(timezone.utc).isoformat(),
        "media_type": audio.suffix.lower().lstrip("."),
    }
    manifest = {
        "schema_version": "1.1",
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
            "atomic_notes": len(analysis.atomic_notes),
            "atomic_note_segments": digestion_coverage["atomic_note_segments"],
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
            "note_ingestion": "automatic_after_contract_validation",
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
        (staging / "atomic_notes.jsonl").write_text(
            _atomic_notes_jsonl(source, transcript, analysis), encoding="utf-8"
        )
        (staging / "residue.md").write_text(
            _residue_markdown(source_id, transcript, analysis), encoding="utf-8"
        )
        after = (audio.stat().st_size, audio.stat().st_mtime_ns, _sha256(audio))
        if after != before:
            raise ProcessingError("source audio changed during processing")
        os.rename(staging, package)
    return package
