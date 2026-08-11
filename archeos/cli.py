from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .analysis import FileAnalysisProvider
from .codex_app_server import CodexAnalysisProvider
from .notes import JsonlNoteStore, ingest_processing_package
from .pipeline import ProcessingError, process_audio
from .pyannote_speakers import PyannoteSpeakerProvider
from .speakers import FileSpeakerProvider
from .transcription import FileTranscriptionProvider, MlxWhisperTranscriptionProvider
from .world_model import ObjectResolver, SQLiteWorldModelRepository

DEFAULT_WORLD_MODEL_DATABASE = Path("04_core/archeos.sqlite3")
DEFAULT_NOTE_STORE = Path("03_notes/notes.jsonl")


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
    process.add_argument(
        "--speaker-map",
        type=Path,
        help="Optional JSON diarization map using neutral Speaker_N labels.",
    )
    process.add_argument(
        "--analysis-file",
        type=Path,
        help="Use an existing schema-compliant analysis JSON instead of Codex.",
    )
    process.add_argument(
        "--note-store",
        type=Path,
        default=DEFAULT_NOTE_STORE,
        help="Durable Note JSONL path (default: 03_notes/notes.jsonl).",
    )

    notes = subparsers.add_parser(
        "note", help="Ingest and inspect durable local Notes."
    )
    notes.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_NOTE_STORE,
        help="Durable Note JSONL path (default: 03_notes/notes.jsonl).",
    )
    note_commands = notes.add_subparsers(dest="note_command", required=True)
    ingest = note_commands.add_parser(
        "ingest", help="Ingest a processing package idempotently."
    )
    ingest.add_argument("processing_package", type=Path)

    objects = subparsers.add_parser(
        "object", help="Create and inspect local World Model Objects."
    )
    objects.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_WORLD_MODEL_DATABASE,
        help="SQLite World Model path (default: 04_core/archeos.sqlite3).",
    )
    object_commands = objects.add_subparsers(dest="object_command", required=True)

    create = object_commands.add_parser("create", help="Create a stable Object.")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--role",
        action="append",
        default=[],
        help="Initial accepted Role; may be repeated.",
    )

    show = object_commands.add_parser("show", help="Resolve an Object for humans.")
    show.add_argument("object_id")

    rename = object_commands.add_parser("rename", help="Rename an Object.")
    rename.add_argument("object_id")
    rename.add_argument("--name", required=True)

    add_role = object_commands.add_parser(
        "add-role", help="Add an accepted Role to an Object."
    )
    add_role.add_argument("object_id")
    add_role.add_argument("role")
    return parser


def _process_command(args: argparse.Namespace) -> int:
    transcriber = (
        FileTranscriptionProvider(args.transcript)
        if args.transcript
        else MlxWhisperTranscriptionProvider(
            model=args.model,
            language=args.language,
        )
    )
    speaker_provider = (
        FileSpeakerProvider(args.speaker_map)
        if args.speaker_map
        else PyannoteSpeakerProvider()
    )
    analysis_provider = (
        FileAnalysisProvider(args.analysis_file)
        if args.analysis_file
        else CodexAnalysisProvider()
    )
    try:
        package = process_audio(
            args.audio,
            args.output_root,
            transcriber,
            speaker_provider,
            analysis_provider,
        )
    except ProcessingError as exc:
        print(f"error: {exc}")
        return 1

    try:
        result = ingest_processing_package(
            package,
            JsonlNoteStore(args.note_store),
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: processing package created at {package}")
        print(f"error: durable Note ingestion failed: {exc}")
        return 1

    print(package)
    print(
        json.dumps(
            asdict(result),
            ensure_ascii=False,
        )
    )
    return 0


def _note_command(args: argparse.Namespace) -> int:
    if args.note_command != "ingest":  # pragma: no cover - argparse enforces this
        return 2
    try:
        result = ingest_processing_package(
            args.processing_package,
            JsonlNoteStore(args.store),
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


def _print_object(repository: SQLiteWorldModelRepository, object_id: str) -> None:
    resolved = ObjectResolver(repository).resolve(object_id)
    print(json.dumps(asdict(resolved), ensure_ascii=False, indent=2))


def _object_command(args: argparse.Namespace) -> int:
    try:
        with SQLiteWorldModelRepository(args.database) as repository:
            if args.object_command == "create":
                record = repository.create_object(args.name, roles=tuple(args.role))
            elif args.object_command == "show":
                record = repository.get_object(args.object_id)
            elif args.object_command == "rename":
                repository.rename_object(args.object_id, args.name)
                record = repository.get_object(args.object_id)
            elif args.object_command == "add-role":
                repository.add_role(args.object_id, args.role)
                record = repository.get_object(args.object_id)
            else:  # pragma: no cover - argparse enforces this
                return 2
            _print_object(repository, record.object_id)
    except (sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "process":
        return _process_command(args)
    if args.command == "object":
        return _object_command(args)
    if args.command == "note":
        return _note_command(args)
    return 2  # pragma: no cover - argparse enforces this
