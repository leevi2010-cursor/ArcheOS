from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start: float | None = None
    end: float | None = None


@dataclass(frozen=True)
class Transcript:
    text: str
    segments: tuple[TranscriptSegment, ...]
    engine: str
    model: str | None = None
    language: str | None = None


class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> Transcript: ...


def _from_json(payload: object, *, engine: str) -> Transcript:
    if not isinstance(payload, dict):
        raise ValueError("transcript JSON must be an object")

    raw_segments = payload.get("segments", [])
    segments: list[TranscriptSegment] = []
    if isinstance(raw_segments, list):
        for item in raw_segments:
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                continue
            start = item.get("start")
            end = item.get("end")
            segments.append(
                TranscriptSegment(
                    text=str(item["text"]).strip(),
                    start=float(start) if isinstance(start, (int, float)) else None,
                    end=float(end) if isinstance(end, (int, float)) else None,
                )
            )

    text = str(payload.get("text", "")).strip()
    if not text and segments:
        text = " ".join(segment.text for segment in segments)
    if not segments and text:
        segments = [
            TranscriptSegment(line.strip())
            for line in text.splitlines()
            if line.strip()
        ]
    if not text:
        raise ValueError("transcript is empty")

    return Transcript(
        text=text,
        segments=tuple(segments),
        engine=engine,
        model=str(payload.get("model")) if payload.get("model") else None,
        language=str(payload.get("language")) if payload.get("language") else None,
    )


class FileTranscriber:
    def __init__(self, transcript_file: Path) -> None:
        self.transcript_file = transcript_file

    def transcribe(self, audio: Path) -> Transcript:
        del audio
        if not self.transcript_file.is_file():
            raise RuntimeError(f"transcript file not found: {self.transcript_file}")
        content = self.transcript_file.read_text(encoding="utf-8")
        if self.transcript_file.suffix.lower() == ".json":
            return _from_json(json.loads(content), engine="provided-json")
        text = content.strip()
        if not text:
            raise RuntimeError("transcript is empty")
        segments = tuple(
            TranscriptSegment(line.strip()) for line in text.splitlines() if line.strip()
        )
        return Transcript(text=text, segments=segments, engine="provided-text")


class MlxWhisperTranscriber:
    def __init__(
        self,
        *,
        model: str,
        language: str | None = None,
        executable: str = "mlx_whisper",
    ) -> None:
        self.model = model
        self.language = language
        self.executable = executable

    def transcribe(self, audio: Path) -> Transcript:
        executable = shutil.which(self.executable)
        if not executable:
            raise RuntimeError(
                "mlx_whisper is not installed; install it or pass --transcript"
            )

        with tempfile.TemporaryDirectory(prefix="archeos-transcript-") as temp_dir:
            command = [
                executable,
                str(audio),
                "--model",
                self.model,
                "--output-dir",
                temp_dir,
                "--output-name",
                "transcript",
                "--output-format",
                "json",
                "--temperature",
                "0",
                "--verbose",
                "False",
            ]
            if self.language:
                command.extend(["--language", self.language])
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"mlx_whisper failed: {detail}")

            output = Path(temp_dir) / "transcript.json"
            if not output.is_file():
                raise RuntimeError("mlx_whisper did not create transcript.json")
            transcript = _from_json(
                json.loads(output.read_text(encoding="utf-8")),
                engine="mlx_whisper",
            )
            return Transcript(
                text=transcript.text,
                segments=transcript.segments,
                engine=transcript.engine,
                model=self.model,
                language=transcript.language or self.language,
            )
