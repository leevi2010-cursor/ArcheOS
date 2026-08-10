from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import warnings
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .transcription import Transcript, TranscriptSegment

MODEL_ID = "pyannote/speaker-diarization-community-1"
NEUTRAL_SPEAKER = re.compile(r"Speaker_[1-9]\d*")


@dataclass(frozen=True)
class DiarizationTurn:
    start: float
    end: float
    speaker: str


class DiarizationBackend(Protocol):
    def diarize(self, audio: Path) -> tuple[DiarizationTurn, ...]: ...


def _decode_audio(audio: Path) -> dict[str, object]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for local pyannote diarization")
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(audio),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode or not result.stdout:
        raise RuntimeError("ffmpeg could not decode audio for local diarization")

    samples = array("f")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("pyannote.audio requires PyTorch") from exc
    waveform = torch.tensor(samples, dtype=torch.float32).unsqueeze(0)
    return {"waveform": waveform, "sample_rate": 16000}


class _PyannoteBackend:
    def __init__(
        self,
        pipeline: object,
        *,
        decoder: Callable[[Path], dict[str, object]] = _decode_audio,
    ) -> None:
        self.pipeline = pipeline
        self.decoder = decoder

    def diarize(self, audio: Path) -> tuple[DiarizationTurn, ...]:
        try:
            output = self.pipeline(self.decoder(audio))  # type: ignore[operator]
            annotation = output.exclusive_speaker_diarization
            return tuple(
                DiarizationTurn(float(turn.start), float(turn.end), str(speaker))
                for turn, _, speaker in annotation.itertracks(yield_label=True)
            )
        except Exception as exc:
            raise RuntimeError("local pyannote diarization failed") from exc


def _load_backend() -> DiarizationBackend:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"\s*torchcodec is not installed correctly.*",
                category=UserWarning,
            )
            from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError(
            "pyannote.audio==4.0.7 is required for automatic speaker diarization"
        ) from exc

    try:
        pipeline = Pipeline.from_pretrained(MODEL_ID, token=True)
    except Exception as exc:
        raise RuntimeError(
            "pyannote model unavailable; accept the community-1 model conditions "
            "and configure local Hugging Face authentication"
        ) from exc
    if pipeline is None:
        raise RuntimeError("pyannote did not load the community-1 model")
    return _PyannoteBackend(pipeline)


def _complete_neutral_labels(transcript: Transcript) -> bool:
    return bool(transcript.segments) and all(
        segment.speaker is not None
        and NEUTRAL_SPEAKER.fullmatch(segment.speaker) is not None
        for segment in transcript.segments
    )


def _normalize_turns(
    turns: tuple[DiarizationTurn, ...],
) -> tuple[DiarizationTurn, ...]:
    ordered = sorted(turns, key=lambda turn: (turn.start, turn.end))
    labels: dict[str, str] = {}
    normalized: list[DiarizationTurn] = []
    for turn in ordered:
        if turn.end <= turn.start or not turn.speaker:
            continue
        label = labels.setdefault(turn.speaker, f"Speaker_{len(labels) + 1}")
        normalized.append(DiarizationTurn(turn.start, turn.end, label))
    return tuple(normalized)


def _speaker_for_segment(
    segment: TranscriptSegment,
    turns: tuple[DiarizationTurn, ...],
) -> str | None:
    if segment.start is None or segment.end is None or segment.end <= segment.start:
        raise RuntimeError(
            "automatic speaker alignment requires transcript segments with valid "
            "start/end timestamps; provide timestamped transcription or --speaker-map"
        )

    overlap_by_speaker: dict[str, float] = {}
    for turn in turns:
        overlap = min(segment.end, turn.end) - max(segment.start, turn.start)
        if overlap > 0:
            overlap_by_speaker[turn.speaker] = (
                overlap_by_speaker.get(turn.speaker, 0.0) + overlap
            )
    if not overlap_by_speaker:
        return None

    ranked = sorted(overlap_by_speaker.items(), key=lambda item: item[1], reverse=True)
    dominant_speaker, dominant_overlap = ranked[0]
    if len(ranked) > 1 and dominant_overlap <= ranked[1][1]:
        return None
    if dominant_overlap <= (segment.end - segment.start) / 2:
        return None
    return dominant_speaker


def attribute_speakers(
    transcript: Transcript,
    turns: tuple[DiarizationTurn, ...],
) -> Transcript:
    normalized = _normalize_turns(turns)
    segments = tuple(
        TranscriptSegment(
            text=segment.text,
            start=segment.start,
            end=segment.end,
            speaker=_speaker_for_segment(segment, normalized),
        )
        for segment in transcript.segments
    )
    return Transcript(
        text=transcript.text,
        segments=segments,
        engine=transcript.engine,
        model=transcript.model,
        language=transcript.language,
    )


class PyannoteSpeakerProvider:
    name = "pyannote-community-1"

    def __init__(
        self,
        *,
        backend_factory: Callable[[], DiarizationBackend] = _load_backend,
    ) -> None:
        self.backend_factory = backend_factory

    def attribute(self, audio: Path, transcript: Transcript) -> Transcript:
        if _complete_neutral_labels(transcript):
            return transcript
        os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")
        turns = self.backend_factory().diarize(audio)
        return attribute_speakers(transcript, turns)
