from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from .transcription import Transcript, TranscriptSegment


class SpeakerProvider(Protocol):
    name: str

    def attribute(self, audio: Path, transcript: Transcript) -> Transcript: ...


class PreserveSpeakerProvider:
    name = "preserve-transcript-labels"

    def attribute(self, audio: Path, transcript: Transcript) -> Transcript:
        del audio
        return transcript


class FileSpeakerProvider:
    name = "speaker-map"

    def __init__(self, speaker_map: Path) -> None:
        self.speaker_map = speaker_map

    def attribute(self, audio: Path, transcript: Transcript) -> Transcript:
        del audio
        if not self.speaker_map.is_file():
            raise RuntimeError(f"speaker map not found: {self.speaker_map}")
        try:
            payload = json.loads(self.speaker_map.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("speaker map is not valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
            raise RuntimeError("speaker map must contain a segments array")

        labels: dict[int, str] = {}
        for item in payload["segments"]:
            if not isinstance(item, dict):
                raise RuntimeError("speaker map segment must be an object")
            segment = item.get("segment")
            speaker = item.get("speaker")
            if not isinstance(segment, int) or isinstance(segment, bool):
                raise RuntimeError("speaker map segment must be an integer")
            if not isinstance(speaker, str) or not re.fullmatch(r"Speaker_[1-9]\d*", speaker):
                raise RuntimeError("speaker labels must use the neutral Speaker_N form")
            if segment in labels or segment < 1 or segment > len(transcript.segments):
                raise RuntimeError("speaker map contains a duplicate or invalid segment")
            labels[segment] = speaker

        segments = tuple(
            TranscriptSegment(
                text=segment.text,
                start=segment.start,
                end=segment.end,
                speaker=labels.get(index, segment.speaker),
            )
            for index, segment in enumerate(transcript.segments, start=1)
        )
        return Transcript(
            text=transcript.text,
            segments=segments,
            engine=transcript.engine,
            model=transcript.model,
            language=transcript.language,
        )
