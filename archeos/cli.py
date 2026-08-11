from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .analysis import FileAnalysisProvider
from .atomic_information import JsonlAtomicInformationStore, ingest_processing_package
from .codex_app_server import CodexAnalysisProvider
from .context import ContextBuilder, ContextRequest
from .digestion import (
    AtomicInformationDigestionService,
    BusinessLanguageHumanJudgmentPort,
    CodexAtomicInformationInterpretationProvider,
    FileAtomicInformationInterpretationProvider,
    JsonlChangeJournal,
    JsonlChangeProposalStore,
)
from .pipeline import ProcessingError, process_audio
from .pyannote_speakers import PyannoteSpeakerProvider
from .speakers import FileSpeakerProvider
from .transcription import FileTranscriptionProvider, MlxWhisperTranscriptionProvider
from .world_model import ObjectResolver, SQLiteWorldModelRepository

DEFAULT_WORLD_MODEL_DATABASE = Path("04_core/archeos.sqlite3")
DEFAULT_ATOMIC_INFORMATION_STORE = Path("03_information/atomic_information.jsonl")
DEFAULT_CHANGE_PROPOSAL_STORE = Path("03_information/change_proposals.jsonl")
DEFAULT_CHANGE_JOURNAL = Path("03_information/change_journal.jsonl")


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
        "--information-store",
        type=Path,
        default=DEFAULT_ATOMIC_INFORMATION_STORE,
        help=(
            "Durable Atomic Information JSONL path "
            "(default: 03_information/atomic_information.jsonl)."
        ),
    )

    information = subparsers.add_parser(
        "information", help="Ingest durable local Atomic Information."
    )
    information.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_ATOMIC_INFORMATION_STORE,
        help=(
            "Durable Atomic Information JSONL path "
            "(default: 03_information/atomic_information.jsonl)."
        ),
    )
    information_commands = information.add_subparsers(
        dest="information_command", required=True
    )
    ingest = information_commands.add_parser(
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

    digest = subparsers.add_parser(
        "digest", help="Digest Atomic Information into the governed World Model."
    )
    digest.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_WORLD_MODEL_DATABASE,
    )
    digest.add_argument(
        "--information-store",
        type=Path,
        default=DEFAULT_ATOMIC_INFORMATION_STORE,
    )
    digest.add_argument(
        "--proposal-store",
        type=Path,
        default=DEFAULT_CHANGE_PROPOSAL_STORE,
    )
    digest.add_argument(
        "--journal",
        type=Path,
        default=DEFAULT_CHANGE_JOURNAL,
    )
    digest_commands = digest.add_subparsers(dest="digest_command", required=True)
    digest_information = digest_commands.add_parser(
        "information", help="Interpret and digest one Atomic Information item."
    )
    digest_information.add_argument("atomic_information_id")
    digest_information.add_argument(
        "--interpretation-file",
        type=Path,
        help="Use deterministic structured interpretation JSON instead of Codex.",
    )
    digest_commands.add_parser(
        "pending", help="Show pending decisions in business language."
    )
    decide = digest_commands.add_parser(
        "decide", help="Approve, reject, or defer a pending suggestion."
    )
    decide.add_argument("proposal_id")
    decide.add_argument("decision", choices=("approve", "reject", "defer"))

    context = subparsers.add_parser(
        "context", help="Build a read-only bounded context for an Object."
    )
    context.add_argument(
        "--database", type=Path, default=DEFAULT_WORLD_MODEL_DATABASE
    )
    context.add_argument(
        "--information-store", type=Path, default=DEFAULT_ATOMIC_INFORMATION_STORE
    )
    context.add_argument(
        "--proposal-store", type=Path, default=DEFAULT_CHANGE_PROPOSAL_STORE
    )
    context.add_argument("--journal", type=Path, default=DEFAULT_CHANGE_JOURNAL)
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_build = context_commands.add_parser(
        "build", help="Build a bounded Context Bundle."
    )
    context_build.add_argument("--scope", required=True, choices=("object",))
    context_build.add_argument("object_id")
    context_build.add_argument("--max-relationships", type=int, default=50)
    context_build.add_argument("--max-information", type=int, default=50)
    context_build.add_argument("--max-changes", type=int, default=50)
    context_build.add_argument("--max-pending", type=int, default=20)
    context_build.add_argument("--max-evidence", type=int, default=5)
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
            JsonlAtomicInformationStore(args.information_store),
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: processing package created at {package}")
        print(f"error: durable Atomic Information ingestion failed: {exc}")
        return 1

    print(package)
    print(
        json.dumps(
            asdict(result),
            ensure_ascii=False,
        )
    )
    return 0


def _information_command(args: argparse.Namespace) -> int:
    if (
        args.information_command != "ingest"
    ):  # pragma: no cover - argparse enforces this
        return 2
    try:
        result = ingest_processing_package(
            args.processing_package,
            JsonlAtomicInformationStore(args.store),
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


def _digest_command(args: argparse.Namespace) -> int:
    provider = None
    if args.digest_command == "information":
        provider = (
            FileAtomicInformationInterpretationProvider(args.interpretation_file)
            if args.interpretation_file
            else CodexAtomicInformationInterpretationProvider()
        )
    try:
        with SQLiteWorldModelRepository(args.database) as repository:
            service = AtomicInformationDigestionService(
                JsonlAtomicInformationStore(args.information_store),
                repository,
                ObjectResolver(repository),
                provider,
                JsonlChangeProposalStore(args.proposal_store),
                JsonlChangeJournal(args.journal),
                BusinessLanguageHumanJudgmentPort(),
            )
            if args.digest_command == "information":
                result = service.digest(args.atomic_information_id)
                print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            elif args.digest_command == "pending":
                rendered = service.render_pending()
                print("\n\n".join(rendered) if rendered else "没有待决定的建议。")
            elif args.digest_command == "decide":
                result = service.decide(args.proposal_id, args.decision)
                print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            else:  # pragma: no cover - argparse enforces this
                return 2
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


def _context_command(args: argparse.Namespace) -> int:
    if args.context_command != "build":  # pragma: no cover - argparse enforces this
        return 2
    try:
        with SQLiteWorldModelRepository(args.database) as repository:
            bundle = ContextBuilder(
                repository,
                ObjectResolver(repository),
                JsonlAtomicInformationStore(args.information_store),
                JsonlChangeJournal(args.journal),
                JsonlChangeProposalStore(args.proposal_store),
            ).build(
                ContextRequest(
                    scope=args.scope,
                    object_id=args.object_id,
                    max_relationships=args.max_relationships,
                    max_atomic_information=args.max_information,
                    max_changes=args.max_changes,
                    max_pending_judgments=args.max_pending,
                    max_evidence_per_information=args.max_evidence,
                )
            )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(asdict(bundle), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "process":
        return _process_command(args)
    if args.command == "object":
        return _object_command(args)
    if args.command == "information":
        return _information_command(args)
    if args.command == "digest":
        return _digest_command(args)
    if args.command == "context":
        return _context_command(args)
    return 2  # pragma: no cover - argparse enforces this
