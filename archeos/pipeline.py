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
from .transcription import Transcript, TranscriptSegment, Transcriber


SUPPORTED_AUDIO = {".m4a", ".mp3", ".wav"}
ARTIFACTS = (
    "manifest.json",
    "transcript.md",
    "meeting_summary.md",
    "atomic_notes.jsonl",
    "residue.md",
)
SEMANTIC_TYPES = {
    "observation",
    "preference",
    "requirement",
    "judgment",
    "decision",
    "commitment",
    "action",
    "question",
    "other",
}


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


def _semantic_type(statement: str) -> tuple[str, float]:
    text = statement.casefold()
    rules = (
        ("question", ("?", "？", "为什么", "如何", "是否", "what", "why", "how"), 0.9),
        (
            "commitment",
            ("我会", "我们会", "我們會", "承诺", "承諾", "promise", "will deliver"),
            0.86,
        ),
        (
            "decision",
            ("决定", "決定", "确定", "確定", "达成一致", "達成一致", "agreed", "decided"),
            0.86,
        ),
        ("action", ("下一步", "需要跟进", "待办", "action item", "follow up"), 0.82),
        ("requirement", ("必须", "需要", "应当", "require", "must", "should"), 0.8),
        ("preference", ("希望", "偏好", "更喜欢", "更喜歡", "prefer", "would like"), 0.8),
        (
            "judgment",
            ("认为", "認為", "判断", "判斷", "可能", "风险", "風險", "think", "believe", "risk"),
            0.72,
        ),
        (
            "observation",
            ("目前", "发现", "發現", "显示", "顯示", "发生", "發生", "observed", "currently"),
            0.76,
        ),
    )
    for semantic_type, markers, confidence in rules:
        if any(marker in text for marker in markers):
            return semantic_type, confidence
    return "other", 0.55


def _is_residue(statement: str) -> tuple[bool, str]:
    compact = re.sub(r"\s+", "", statement)
    if len(compact) < 4:
        return True, "片段过短，缺少可独立理解的上下文。"
    if re.fullmatch(r"[嗯啊哦呃唔]+[。.!！?？]*", compact, flags=re.IGNORECASE):
        return True, "仅包含语气词，无法安全形成原子信息。"
    return False, ""


def _context(segments: tuple[TranscriptSegment, ...], index: int) -> str:
    nearby = segments[max(0, index - 1) : min(len(segments), index + 2)]
    return " ".join(segment.text.strip() for segment in nearby)


def _transcript_markdown(source: dict[str, object], transcript: Transcript) -> str:
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
        f"- Transcription engine: `{transcript.engine}`",
        f"- Model: `{transcript.model or 'not reported'}`",
        f"- Language: `{transcript.language or 'not reported'}`",
        "- Speaker labels: unavailable; identities were not inferred",
        "",
        "## Original Transcription",
        "",
    ]
    for segment in transcript.segments:
        start, end = _timestamp(segment.start), _timestamp(segment.end)
        prefix = f"[{start} → {end}] " if start is not None and end is not None else ""
        lines.append(f"{prefix}{segment.text.strip()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _summary_markdown(
    source: dict[str, object],
    transcript: Transcript,
    atomic_notes: list[dict[str, object]],
) -> str:
    grouped: dict[str, list[str]] = {semantic_type: [] for semantic_type in SEMANTIC_TYPES}
    for note in atomic_notes:
        grouped[str(note["semantic_type"])].append(str(note["statement"]))

    def bullets(items: list[str], empty: str = "待人工确认") -> str:
        return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"

    main = [segment.text.strip() for segment in transcript.segments]
    topic = main[0][:80] if main else "待人工确认"
    disagreements = [
        statement
        for statement in main
        if any(
            marker in statement.casefold()
            for marker in ("不同意", "但是", "冲突", "disagree", "however")
        )
    ]
    viewpoints = grouped["preference"] + grouped["judgment"] + grouped["requirement"]
    agreements = grouped["decision"] + grouped["commitment"]
    actions = grouped["action"] + grouped["commitment"]

    return f"""# Basic Information

Topic: {topic}
Time: {source['modified_at']}
Participants: 待人工确认（未推断说话人身份）

# Discussion Goal

- 待人工确认

# Main Discussion

{bullets(main)}

# Key Viewpoints

{bullets(viewpoints)}

# Agreements / Consensus

{bullets(agreements)}

# Disagreements

{bullets(disagreements)}

# Unresolved Questions

{bullets(grouped['question'])}

# Next Actions

{bullets(actions)}
"""


def _residue_markdown(source_id: str, residue: list[dict[str, str]]) -> str:
    lines = [
        "# Residue",
        "",
        f"Source ID: `{source_id}`",
        "",
        "Residue is retained for human review and digestion-rule improvement.",
        "",
    ]
    if not residue:
        lines.append("No residue was identified by the current deterministic rules.")
    else:
        for index, item in enumerate(residue, start=1):
            lines.extend(
                [
                    f"## Item {index}",
                    "",
                    f"- Original excerpt: {item['excerpt']}",
                    f"- Reason not absorbed: {item['reason']}",
                    f"- Possible future value or uncertainty: {item['future_value']}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def process_audio(audio: Path, output_root: Path, transcriber: Transcriber) -> Path:
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
        transcript = transcriber.transcribe(audio)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ProcessingError(str(exc)) from exc
    if not transcript.segments:
        raise ProcessingError("transcription did not contain any segments")

    source = {
        "id": source_id,
        "path": str(audio),
        "sha256": digest,
        "size_bytes": before[0],
        "modified_at": datetime.fromtimestamp(
            audio.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "media_type": audio.suffix.lower().lstrip("."),
    }
    atomic_notes: list[dict[str, object]] = []
    residue: list[dict[str, str]] = []
    for index, segment in enumerate(transcript.segments):
        statement = segment.text.strip()
        is_residue, reason = _is_residue(statement)
        if is_residue:
            residue.append(
                {
                    "excerpt": statement,
                    "reason": reason,
                    "future_value": "保留原文，等待更多上下文或人工判断。",
                }
            )
            continue
        semantic_type, confidence = _semantic_type(statement)
        atomic_notes.append(
            {
                "id": f"{source_id}-{index + 1:04d}",
                "statement": statement,
                "semantic_type": semantic_type,
                "source_evidence": {
                    "source_id": source_id,
                    "artifact": "transcript.md",
                    "segment": index + 1,
                    "start": _timestamp(segment.start),
                    "end": _timestamp(segment.end),
                    "excerpt": statement,
                },
                "context": _context(transcript.segments, index),
                "confidence": confidence,
                "status": "proposed",
            }
        )

    manifest = {
        "schema_version": "1.0",
        "pipeline_version": __version__,
        "source": source,
        "transcription": {
            "engine": transcript.engine,
            "model": transcript.model,
            "language": transcript.language,
            "speaker_labels_available": False,
        },
        "artifacts": list(ARTIFACTS),
        "counts": {"atomic_notes": len(atomic_notes), "residue_items": len(residue)},
        "review": {
            "status": "awaiting_human_review",
            "automatic_core_write": False,
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
            _summary_markdown(source, transcript, atomic_notes), encoding="utf-8"
        )
        (staging / "atomic_notes.jsonl").write_text(
            "".join(json.dumps(note, ensure_ascii=False) + "\n" for note in atomic_notes),
            encoding="utf-8",
        )
        (staging / "residue.md").write_text(
            _residue_markdown(source_id, residue), encoding="utf-8"
        )
        after = (audio.stat().st_size, audio.stat().st_mtime_ns, _sha256(audio))
        if after != before:
            raise ProcessingError("source audio changed during processing")
        os.rename(staging, package)
    return package
