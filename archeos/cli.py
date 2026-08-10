from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import ProcessingError, process_audio
from .transcription import FileTranscriber, MlxWhisperTranscriber


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archeos")
    subparsers = parser.add_subparsers(dest="command", required=True)

    process = subparsers.add_parser(
        "process", help="Create a reviewable processing package from an audio file."
    )
    process.add_argument("audio", type=Path)
    process.add_argument(
        "--output-root",
        type=Path,
        default=Path("02_processing"),
        help="Parent directory for processing packages (default: 02_processing).",
    )
    process.add_argument(
        "--transcript",
        type=Path,
        help="Use an existing .json or text transcript instead of running mlx_whisper.",
    )
    process.add_argument(
        "--model",
        default="mlx-community/whisper-tiny",
        help="mlx_whisper model name or local path.",
    )
    process.add_argument("--language", help="Optional transcription language code.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "process":  # pragma: no cover - argparse enforces this
        return 2

    transcriber = (
        FileTranscriber(args.transcript)
        if args.transcript
        else MlxWhisperTranscriber(model=args.model, language=args.language)
    )
    try:
        package = process_audio(args.audio, args.output_root, transcriber)
    except ProcessingError as exc:
        print(f"error: {exc}")
        return 1

    print(package)
    return 0
