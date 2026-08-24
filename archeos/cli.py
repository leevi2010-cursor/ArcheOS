from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from .analysis import FileAnalysisProvider
from .atomic_information import JsonlAtomicInformationStore, ingest_processing_package
from .codex_app_server import CodexAnalysisProvider, _load_sdk
from .context import ContextBuilder, ContextRequest
from .digestion import (
    AtomicInformationDigestionService,
    BusinessLanguageHumanJudgmentPort,
    CodexAtomicInformationInterpretationProvider,
    FileAtomicInformationInterpretationProvider,
    JsonlChangeJournal,
    JsonlChangeProposalStore,
)
from .pipeline import ProcessingError, process_managed_audio
from .pyannote_speakers import PyannoteSpeakerProvider
from .representation import (
    LocalRepresentationRepository,
    RepresentationError,
    RepresentationService,
    WechatConversationError,
    WechatConversationRepresentationAdapter,
    wechat_conversation_metrics,
)
from .representation.registry import production_adapter
from .representation_information import (
    DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
    DEFAULT_SEMANTIC_MODEL,
    DEFAULT_SEMANTIC_REASONING_EFFORT,
    CodexCliRepresentationAnalysisProvider,
    FileRepresentationAnalysisProvider,
    RepresentationInformationError,
    RepresentationInformationService,
)
from .semantic_handoff import ExternalAgentSemanticHandoffService, SemanticHandoffError
from .source import (
    HandoffMarkerService,
    LocalManagedSourceRepository,
    ManagedSourceService,
    SourceError,
)
from .speakers import FileSpeakerProvider
from .timeline import (
    CodexTimelineProvider,
    TimelineError,
    build_timelines,
    evidence_view_refs,
    load_selection,
)
from .transcription import FileTranscriptionProvider, MlxWhisperTranscriptionProvider
from .wechat_digest import (
    ExistingSemanticHandoff,
    WechatCliCaptureProvider,
    WechatDigestError,
    WechatDigestService,
    detect_codex_provider_version,
)
from .workspace import (
    codex_integration_status,
    doctor,
    initialize_workspace,
    install_codex_integration,
    load_workspace_config,
    remove_codex_integration,
    require_workspace,
    resolve_storage_path,
)
from .world_model import ObjectResolver, SQLiteWorldModelRepository

DEFAULT_WORLD_MODEL_DATABASE = Path("04_core/archeos.sqlite3")
DEFAULT_ATOMIC_INFORMATION_STORE = Path("03_information/atomic_information.jsonl")
DEFAULT_CHANGE_PROPOSAL_STORE = Path("03_information/change_proposals.jsonl")
DEFAULT_CHANGE_JOURNAL = Path("03_information/change_journal.jsonl")
DEFAULT_WECHAT_DIGEST_MAX_ITEMS = 3
DEFAULT_MANAGED_SOURCE_ROOT = Path("01_inbox")
DEFAULT_REPRESENTATION_ROOT = Path("02_processing/representations")
DEFAULT_REPRESENTATION_INFORMATION_ROOT = Path("02_processing/information")
DEFAULT_SEMANTIC_HANDOFF_AUDIT_ROOT = Path("02_processing/semantic_handoff_runs")


def _add_workspace_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", type=Path, help="ArcheOS local Workspace configuration path."
    )


def _resolve_durable_paths(args: argparse.Namespace) -> None:
    defaults: dict[str, Path] = {}
    if args.command == "process":
        defaults = {
            "managed_root": DEFAULT_MANAGED_SOURCE_ROOT,
            "output_root": Path("02_processing"),
            "information_store": DEFAULT_ATOMIC_INFORMATION_STORE,
        }
    elif args.command == "information":
        defaults = {"store": DEFAULT_ATOMIC_INFORMATION_STORE}
        if args.information_command == "extract":
            defaults.update(
                {
                    "managed_root": DEFAULT_MANAGED_SOURCE_ROOT,
                    "representation_root": DEFAULT_REPRESENTATION_ROOT,
                    "output_root": DEFAULT_REPRESENTATION_INFORMATION_ROOT,
                }
            )
            if args.external_agent_route is not None:
                defaults["audit_root"] = DEFAULT_SEMANTIC_HANDOFF_AUDIT_ROOT
    elif args.command == "object":
        defaults = {"database": DEFAULT_WORLD_MODEL_DATABASE}
    elif args.command in {"digest", "context"}:
        defaults = {
            "database": DEFAULT_WORLD_MODEL_DATABASE,
            "information_store": DEFAULT_ATOMIC_INFORMATION_STORE,
            "proposal_store": DEFAULT_CHANGE_PROPOSAL_STORE,
            "journal": DEFAULT_CHANGE_JOURNAL,
        }
    elif args.command == "stage1-review":
        defaults = {
            "database": DEFAULT_WORLD_MODEL_DATABASE,
            "information_store": DEFAULT_ATOMIC_INFORMATION_STORE,
            "proposal_store": DEFAULT_CHANGE_PROPOSAL_STORE,
            "journal": DEFAULT_CHANGE_JOURNAL,
        }
    elif args.command == "source":
        defaults = {"managed_root": DEFAULT_MANAGED_SOURCE_ROOT}
    elif args.command in {"representation", "conversation"}:
        defaults = {
            "managed_root": DEFAULT_MANAGED_SOURCE_ROOT,
            "representation_root": DEFAULT_REPRESENTATION_ROOT,
        }
    if not defaults:
        return
    workspace = (
        require_workspace(args.config)
        if any(getattr(args, name) is None for name in defaults)
        else None
    )
    for name, relative_default in defaults.items():
        setattr(
            args,
            name,
            resolve_storage_path(getattr(args, name), workspace, relative_default),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archeos")
    from . import __version__

    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser(
        "init", help="Initialize a private local ArcheOS Workspace."
    )
    init.add_argument("workspace_path", type=Path, nargs="?", default=Path.cwd())
    init.add_argument("--config", type=Path, help="ArcheOS local configuration path.")

    doctor_command = subparsers.add_parser(
        "doctor", help="Check the local core installation and Workspace."
    )
    doctor_command.add_argument(
        "--config", type=Path, help="ArcheOS local configuration path."
    )

    config = subparsers.add_parser(
        "config", help="Inspect local ArcheOS configuration."
    )
    config.add_argument("--config", type=Path, help="ArcheOS local configuration path.")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="Show non-secret local configuration.")

    mcp = subparsers.add_parser(
        "mcp", help="Run the local read-only ArcheOS MCP server."
    )
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser(
        "serve", help="Serve canonical read tools over stdio."
    )
    mcp_serve.add_argument(
        "--workspace", type=Path, help="Initialized ArcheOS Workspace."
    )
    mcp_serve.add_argument(
        "--config", type=Path, help="ArcheOS local configuration path."
    )

    integration = subparsers.add_parser(
        "integration", help="Manage supported local Agent integrations."
    )
    integration_commands = integration.add_subparsers(
        dest="integration_target", required=True
    )
    codex = integration_commands.add_parser(
        "codex", help="Manage the local Codex MCP registration."
    )
    codex_commands = codex.add_subparsers(dest="integration_command", required=True)
    for command_name, help_text in (
        ("install", "Install the ArcheOS-managed local Codex MCP entry."),
        ("status", "Show whether Codex can discover the managed MCP entry."),
        ("remove", "Remove only the ArcheOS-managed local Codex MCP entry."),
    ):
        command = codex_commands.add_parser(command_name, help=help_text)
        command.add_argument(
            "--config", type=Path, help="ArcheOS local configuration path."
        )
        command.add_argument(
            "--codex-config",
            type=Path,
            help="Codex config.toml path (for explicit local scope).",
        )

    process = subparsers.add_parser(
        "process", help="Process one verified Managed Source audio input."
    )
    _add_workspace_config_argument(process)
    process.add_argument("source_id", help="Verified Managed Source ID to process.")
    process.add_argument(
        "--managed-root",
        type=Path,
        default=None,
        help="Managed Source root (default: 01_inbox).",
    )
    process.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Parent directory for processing packages (default: 02_processing).",
    )
    process.add_argument(
        "--transcript",
        type=Path,
        help="Development/testing only: use an existing transcript fixture.",
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
        help="Development/testing only: JSON diarization map using neutral Speaker_N labels.",
    )
    process.add_argument(
        "--analysis-file",
        type=Path,
        help="Development/testing only: schema-compliant analysis fixture instead of Codex.",
    )
    process.add_argument(
        "--information-store",
        type=Path,
        default=None,
        help=(
            "Durable Atomic Information JSONL path "
            "(default: 03_information/atomic_information.jsonl)."
        ),
    )

    information = subparsers.add_parser(
        "information", help="Ingest durable local Atomic Information."
    )
    _add_workspace_config_argument(information)
    information.add_argument(
        "--store",
        type=Path,
        default=None,
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
    extract = information_commands.add_parser(
        "extract", help="Extract one verified Representation into Atomic Information."
    )
    extract.add_argument("representation_id")
    extract.add_argument("--managed-root", type=Path, default=None)
    extract.add_argument("--representation-root", type=Path, default=None)
    extract.add_argument("--output-root", type=Path, default=None)
    provider = extract.add_mutually_exclusive_group(required=True)
    provider.add_argument(
        "--analysis-file",
        type=Path,
        help=(
            "Required explicit deterministic fixture or reviewed structured-result "
            "handoff."
        ),
    )
    provider.add_argument(
        "--external-agent-route",
        choices=("codex-cli",),
        help="Explicit approved External Agent route; no automatic fallback.",
    )
    extract.add_argument(
        "--provider-version",
        help="Expected Codex version assertion; the executable remains the fact source.",
    )
    extract.add_argument(
        "--codex-bin",
        default="codex",
        help="Explicit Codex CLI executable for the approved route.",
    )
    extract.add_argument("--timeout-seconds", type=float, default=120.0)
    extract.add_argument("--model", default=DEFAULT_SEMANTIC_MODEL)
    extract.add_argument(
        "--reasoning-effort", default=DEFAULT_SEMANTIC_REASONING_EFFORT
    )
    extract.add_argument("--audit-root", type=Path, default=None)
    extract.add_argument(
        "--batch-size", type=int, default=DEFAULT_EXTERNAL_AGENT_BATCH_SIZE
    )

    objects = subparsers.add_parser(
        "object", help="Create and inspect local World Model Objects."
    )
    _add_workspace_config_argument(objects)
    objects.add_argument(
        "--database",
        type=Path,
        default=None,
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
    _add_workspace_config_argument(digest)
    digest.add_argument(
        "--database",
        type=Path,
        default=None,
    )
    digest.add_argument(
        "--information-store",
        type=Path,
        default=None,
    )
    digest.add_argument(
        "--proposal-store",
        type=Path,
        default=None,
    )
    digest.add_argument(
        "--journal",
        type=Path,
        default=None,
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
    _add_workspace_config_argument(context)
    context.add_argument("--database", type=Path, default=None)
    context.add_argument("--information-store", type=Path, default=None)
    context.add_argument("--proposal-store", type=Path, default=None)
    context.add_argument("--journal", type=Path, default=None)
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

    review = subparsers.add_parser(
        "stage1-review",
        help="Build a read-only Stage 1 Object Timeline review package.",
    )
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_build = review_commands.add_parser(
        "build", help="Build local JSON and business Markdown projections."
    )
    review_build.add_argument("--selection-file", type=Path, required=True)
    review_build.add_argument("--output-root", type=Path, required=True)
    review_build.add_argument("--resume", action="store_true")
    review_build.add_argument("--config", type=Path)
    review_build.add_argument("--database", type=Path, default=None)
    review_build.add_argument("--information-store", type=Path, default=None)
    review_build.add_argument("--proposal-store", type=Path, default=None)
    review_build.add_argument("--journal", type=Path, default=None)

    source = subparsers.add_parser(
        "source", help="Admit, inspect, verify, and restore local Managed Sources."
    )
    _add_workspace_config_argument(source)
    source.add_argument(
        "--managed-root",
        type=Path,
        default=None,
        help="Managed Source root (default: 01_inbox).",
    )
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_admit = source_commands.add_parser(
        "admit", help="Explicitly admit one external file as a Managed Source."
    )
    source_admit.add_argument("external_file", type=Path)
    source_admit.add_argument(
        "--source-id", help="Inject a deterministic Source ID for tests."
    )
    source_admit.add_argument("--media-type")
    source_admit.add_argument(
        "--managed-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="Managed Source root (default: 01_inbox).",
    )
    for command_name, help_text in (
        ("show", "Show one immutable Managed Source Manifest."),
        ("verify", "Verify Managed Source bytes against its Manifest."),
    ):
        command = source_commands.add_parser(command_name, help=help_text)
        command.add_argument("source_id")
        command.add_argument(
            "--managed-root",
            type=Path,
            default=argparse.SUPPRESS,
            help="Managed Source root (default: 01_inbox).",
        )
    source_list = source_commands.add_parser("list", help="List local Managed Sources.")
    source_list.add_argument(
        "--managed-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="Managed Source root (default: 01_inbox).",
    )
    source_restore = source_commands.add_parser(
        "restore", help="Restore a verified Managed Source without overwriting."
    )
    source_restore.add_argument("source_id")
    source_restore.add_argument("target_file", type=Path)
    source_restore.add_argument(
        "--managed-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="Managed Source root (default: 01_inbox).",
    )
    source_handoff = source_commands.add_parser(
        "handoff", help="Write or inspect an explicit external Handoff Marker."
    )
    source_handoff_commands = source_handoff.add_subparsers(
        dest="handoff_command", required=True
    )
    handoff_write = source_handoff_commands.add_parser(
        "write", help="Write a file-level Handoff Marker next to an external old file."
    )
    handoff_write.add_argument("source_id")
    handoff_write.add_argument("--target-file", type=Path)
    handoff_write.add_argument(
        "--managed-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="Managed Source root (default: 01_inbox).",
    )
    handoff_show = source_handoff_commands.add_parser(
        "show", help="Read a strict ArcheOS Handoff Marker."
    )
    handoff_show.add_argument("marker_path", type=Path)
    handoff_show.add_argument(
        "--managed-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="Managed Source root (default: 01_inbox).",
    )

    representation = subparsers.add_parser(
        "representation", help="Inspect or build a Normalized Representation."
    )
    _add_workspace_config_argument(representation)
    representation.add_argument(
        "--managed-root",
        type=Path,
        default=None,
        help="Managed Source root (default: 01_inbox).",
    )
    representation.add_argument(
        "--representation-root",
        type=Path,
        default=None,
        help="Representation root (default: 02_processing/representations).",
    )
    representation_commands = representation.add_subparsers(
        dest="representation_command", required=True
    )
    representation_build = representation_commands.add_parser(
        "build", help="Build with a registered production Representation Adapter."
    )
    representation_build.add_argument("source_id")
    representation_build.add_argument("--adapter", required=True)
    representation_build.add_argument(
        "--privacy-route",
        choices=("unknown", "restricted", "standard"),
        help="Image only: unknown, restricted, or standard (default: unknown).",
    )
    representation_show = representation_commands.add_parser(
        "show", help="Show one persisted Normalized Representation."
    )
    representation_show.add_argument("representation_id")
    representation_list = representation_commands.add_parser(
        "list", help="List persisted Representations for one Managed Source."
    )
    representation_list.add_argument("--source", required=True)
    representation_verify = representation_commands.add_parser(
        "verify", help="Verify a persisted Normalized Representation."
    )
    representation_verify.add_argument("representation_id")
    for command in (
        representation_build,
        representation_show,
        representation_list,
        representation_verify,
    ):
        command.add_argument(
            "--managed-root",
            type=Path,
            default=argparse.SUPPRESS,
            help="Managed Source root (default: 01_inbox).",
        )
        command.add_argument(
            "--representation-root",
            type=Path,
            default=argparse.SUPPRESS,
            help="Representation root (default: 02_processing/representations).",
        )

    conversation = subparsers.add_parser(
        "conversation", help="Build provider-specific Conversation Representations."
    )
    _add_workspace_config_argument(conversation)
    conversation_providers = conversation.add_subparsers(
        dest="conversation_provider", required=True
    )
    wechat = conversation_providers.add_parser(
        "wechat", help="Build the strict local WeChat Conversation Representation."
    )
    wechat_commands = wechat.add_subparsers(dest="conversation_command", required=True)
    wechat_represent = wechat_commands.add_parser(
        "represent", help="Represent one verified WeChat Managed Source."
    )
    wechat_represent.add_argument("source_id")
    wechat_represent.add_argument("--managed-root", type=Path, default=None)
    wechat_represent.add_argument("--representation-root", type=Path, default=None)

    wechat_product = subparsers.add_parser(
        "wechat", help="消化从上次成功位置以来的微信信息。"
    )
    _add_workspace_config_argument(wechat_product)
    wechat_product_commands = wechat_product.add_subparsers(
        dest="wechat_command", required=True
    )
    wechat_digest = wechat_product_commands.add_parser(
        "digest", help="增量保存并消化新增微信消息和附件。"
    )
    bootstrap = wechat_digest.add_mutually_exclusive_group()
    bootstrap.add_argument("--since", help="首次从 ISO 日期或时间开始。")
    bootstrap.add_argument(
        "--from-now", action="store_true", help="首次从当前微信位置开始。"
    )
    bootstrap.add_argument(
        "--all-history", action="store_true", help="首次处理全部可读历史。"
    )
    wechat_digest.add_argument("--wechat-cli-bin", default="wechat-cli")
    wechat_digest.add_argument("--wechat-config", type=Path)
    wechat_digest.add_argument("--codex-bin", default="codex")
    wechat_digest.add_argument(
        "--provider-version",
        help="Expected Codex version assertion; actual version is read from --codex-bin.",
    )
    wechat_digest.add_argument("--timeout-seconds", type=float, default=300.0)
    wechat_digest.add_argument("--model", default=DEFAULT_SEMANTIC_MODEL)
    wechat_digest.add_argument(
        "--reasoning-effort", default=DEFAULT_SEMANTIC_REASONING_EFFORT
    )
    wechat_digest.add_argument(
        "--batch-size", type=int, default=DEFAULT_EXTERNAL_AGENT_BATCH_SIZE
    )
    wechat_digest.add_argument(
        "--semantic-parallelism",
        type=int,
        choices=(1, 2, 3, 4),
        default=2,
        help="不同完整会话的只读语义分析并行度；长期写入仍保持串行。",
    )
    wechat_digest.add_argument(
        "--max-items-per-run",
        type=int,
        default=DEFAULT_WECHAT_DIGEST_MAX_ITEMS,
        help="每次最多完整处理的业务项数量；仅在项目终态边界停止。",
    )
    wechat_digest.add_argument(
        "--prepare-next-semantic",
        action="store_true",
        help="仅准备下一 semantic batch；不新增 Semantic Handoff 调用。",
    )
    wechat_digest.add_argument(
        "--upgrade-active-v1",
        action="store_true",
        help="零 Provider 升级当前 active v1 run 到当前计划版本。",
    )
    wechat_digest.add_argument(
        "--upgrade-active-v2-all-history",
        action="store_true",
        help="零 Provider 为当前 active v2 全历史运行冻结全局上界。",
    )
    wechat_digest.add_argument(
        "--install-semantic-authority",
        action="store_true",
        help="零 Provider 安装一次性 frozen-global-upper Semantic 调用授权。",
    )
    wechat_digest.add_argument(
        "--install-semantic-authority-extension",
        action="store_true",
        help="零 Provider 安装已批准的 append-only cap-1000 Semantic 扩权。",
    )
    wechat_digest.add_argument(
        "--install-semantic-maintenance-continuation",
        action="store_true",
        help="零 Provider 安装一次性已批准维护版本续接。",
    )
    wechat_digest.add_argument(
        "--install-semantic-reviewed-head-continuation",
        action="store_true",
        help="零 Provider 安装通用、可重复续接的已审核版本绑定。",
    )
    wechat_digest.add_argument(
        "--install-semantic-gate-c-continuation",
        action="store_true",
        help="零 Provider 安装 Issue #146 Gate C 版本续接。",
    )
    wechat_digest.add_argument(
        "--install-semantic-segmented-gate-c-continuation",
        action="store_true",
        help="零 Provider 安装 Issue #148 短执行段版本续接。",
    )
    wechat_digest.add_argument(
        "--activate-batch-governance",
        action="store_true",
        help="零 Provider 安装 Issue #135 整批治理迁移与版本续接。",
    )
    wechat_digest.add_argument(
        "--resolve-semantic-unknown",
        action="store_true",
        help="零 Provider 把已批准的 Semantic unknown 收敛为 failed_closed。",
    )
    wechat_digest.add_argument(
        "--resolve-semantic-timeout-212",
        action="store_true",
        help="零 Provider 把已批准的 ordinal212 timeout 收敛为 failed_closed。",
    )
    wechat_digest.add_argument(
        "--seal-governance-timeout",
        action="store_true",
        help="零 Provider 封存 Governance timeout 并允许后续项目继续。",
    )
    wechat_digest.add_argument(
        "--resolve-governance-startup-failure",
        action="store_true",
        help="零 Provider 恢复 Issue #150 唯一 Governance 启动失败。",
    )
    wechat_digest.add_argument(
        "--resolve-failed-closed-continuation",
        action="store_true",
        help="零 Provider 恢复 Issue #154 历史失败校验阻塞。",
    )
    wechat_digest.add_argument(
        "--resolve-multi-governance-startup-failure",
        action="store_true",
        help="零 Provider 安装 Issue #168 当前项目的独立 Governance 启动恢复。",
    )
    wechat_digest.add_argument(
        "--inventory-authority-file",
        type=Path,
        help="Private 0600 Lead-approved historical inventory authority manifest.",
    )
    wechat_digest.add_argument(
        "--semantic-unknown-authority-file",
        type=Path,
        help="Private 0600 Lead-approved ordinal-166 resolution authority manifest.",
    )
    wechat_digest.add_argument(
        "--semantic-timeout-212-authority-file",
        type=Path,
        help="Private 0600 Lead-approved ordinal-212 resolution authority manifest.",
    )
    wechat_digest.add_argument(
        "--batch-governance-authority-file",
        type=Path,
        help="Private 0600 Lead-approved Issue #135 migration authority manifest.",
    )
    wechat_digest.add_argument(
        "--governance-startup-authority-file",
        type=Path,
        help="Private 0600 Lead-approved Issue #150 startup authority manifest.",
    )
    wechat_digest.add_argument(
        "--failed-closed-authority-file",
        type=Path,
        help="Private 0600 Lead-approved Issue #154 recovery authority manifest.",
    )
    wechat_digest.add_argument(
        "--multi-governance-startup-authority-file",
        type=Path,
        help="Private 0600 Lead-approved Issue #168 startup authority manifest.",
    )
    wechat_digest.add_argument(
        "--authority-ref",
        help="Lead-approved GitHub Issue comment reference.",
    )
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
        package = process_managed_audio(
            args.source_id,
            LocalManagedSourceRepository(args.managed_root),
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


def _workspace_command(args: argparse.Namespace) -> int:
    try:
        if args.command == "init":
            config, changed = initialize_workspace(
                args.workspace_path, config_path=args.config
            )
            print(
                json.dumps(
                    {**config.to_dict(), "created_or_updated": changed},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "doctor":
            report = doctor(args.config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("healthy") else 1
        if args.command == "config":
            if (
                args.config_command != "show"
            ):  # pragma: no cover - argparse enforces this
                return 2
            print(
                json.dumps(
                    load_workspace_config(args.config).to_dict(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "integration":
            if (
                args.integration_target != "codex"
            ):  # pragma: no cover - argparse enforces this
                return 2
            if args.integration_command == "remove":
                print(
                    json.dumps(
                        {"result": remove_codex_integration(args.codex_config)},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            config = load_workspace_config(args.config)
            if args.integration_command == "install":
                print(
                    json.dumps(
                        {
                            "result": "installed",
                            "config_path": install_codex_integration(
                                config, args.codex_config
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.integration_command == "status":
                print(
                    json.dumps(
                        codex_integration_status(config, args.codex_config),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


def _mcp_command(args: argparse.Namespace) -> int:
    if args.mcp_command != "serve":  # pragma: no cover - argparse enforces this
        return 2
    try:
        workspace = args.workspace or load_workspace_config(args.config).workspace
        from .mcp_server import serve

        serve(workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _information_command(args: argparse.Namespace) -> int:
    try:
        if args.information_command == "ingest":
            result = ingest_processing_package(
                args.processing_package,
                JsonlAtomicInformationStore(args.store),
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return 0
        if args.information_command == "extract":
            service = RepresentationInformationService(
                LocalManagedSourceRepository(args.managed_root),
                LocalRepresentationRepository(args.representation_root),
                args.output_root,
                batch_size=args.batch_size,
            )
            if args.analysis_file is not None:
                package = service.extract(
                    args.representation_id,
                    FileRepresentationAnalysisProvider(args.analysis_file),
                )
                result = ingest_processing_package(
                    package, JsonlAtomicInformationStore(args.store)
                )
            else:
                if args.provider_version is None:
                    raise ValueError(
                        "--provider-version is required with --external-agent-route"
                    )
                provider = CodexCliRepresentationAnalysisProvider(
                    codex_binary=args.codex_bin,
                    provider_version=args.provider_version,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    timeout_seconds=args.timeout_seconds,
                )
                handoff = ExternalAgentSemanticHandoffService(
                    service, JsonlAtomicInformationStore(args.store), args.audit_root
                ).execute(args.representation_id, provider)
                package = handoff.package
                result = handoff.ingestion
            print(package)
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 0
        return 2  # pragma: no cover - argparse enforces this
    except (
        OSError,
        RepresentationError,
        RepresentationInformationError,
        SemanticHandoffError,
        SourceError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}")
        return 1


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
        if not args.database.is_file():
            raise ValueError(f"World Model database does not exist: {args.database}")
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


def _source_command(args: argparse.Namespace) -> int:
    repository = LocalManagedSourceRepository(args.managed_root)
    service = ManagedSourceService(repository)
    try:
        if args.source_command == "admit":
            metadata: dict[str, object] = {}
            if args.media_type:
                metadata["media_type"] = args.media_type
            result = service.admit(
                args.external_file,
                source_id=args.source_id,
                metadata=metadata,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.source_command == "show":
            result = service.show(args.source_id)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.source_command == "list":
            print(
                json.dumps(
                    [source.to_dict() for source in service.list()],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.source_command == "verify":
            result = service.verify(args.source_id)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0 if result.verified else 1
        if args.source_command == "restore":
            result = service.restore(args.source_id, args.target_file)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.source_command == "handoff":
            handoff = HandoffMarkerService(repository, args.managed_root)
            if args.handoff_command == "write":
                result = handoff.write(args.source_id, target_file=args.target_file)
            elif args.handoff_command == "show":
                result = handoff.show(args.marker_path)
            else:  # pragma: no cover - argparse enforces this
                return 2
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
        return 2  # pragma: no cover - argparse enforces this
    except (OSError, SourceError, TypeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


def _stage1_review_command(args: argparse.Namespace) -> int:
    try:
        selections = load_selection(args.selection_file)
        if not args.database.is_file():
            raise TimelineError(f"World Model database does not exist: {args.database}")
        contexts: dict[str, dict[str, object]] = {}
        with SQLiteWorldModelRepository(args.database) as repository:
            store = JsonlAtomicInformationStore(args.information_store)
            builder = ContextBuilder(
                repository,
                ObjectResolver(repository),
                store,
                JsonlChangeJournal(args.journal),
                JsonlChangeProposalStore(args.proposal_store),
            )
            for selection in selections:
                bundle = builder.build(
                    ContextRequest(scope="object", object_id=selection.object_id)
                )
                bundle_payload = asdict(bundle)
                bounded_ids = [
                    item.atomic_information_id for item in bundle.atomic_information
                ]
                supplemental_payloads = []
                for atomic_id in selection.supplemental_atomic_information_ids:
                    current = store.get_current(atomic_id)
                    if atomic_id not in bounded_ids:
                        supplemental_payloads.append(asdict(current))

                evidence_references: dict[str, object] = {}
                evidence_by_atomic: dict[str, list[str]] = {}
                atomic_payloads = [
                    *bundle_payload["atomic_information"],
                    *supplemental_payloads,
                ]
                for atomic_payload in atomic_payloads:
                    atomic_id = atomic_payload["atomic_information_id"]
                    references = evidence_view_refs(
                        atomic_id,
                        atomic_payload["source_evidence"],
                    )
                    evidence_references.update(references)
                    evidence_by_atomic[atomic_id] = list(references)

                allowed_object_ids = {bundle.root.object_id}
                allowed_object_ids.update(
                    relationship.neighbor.object_id
                    for relationship in bundle.relationships
                )
                contexts[selection.object_id] = {
                    "context": bundle_payload,
                    "supplemental_atomic_information": supplemental_payloads,
                    "atomic_information_ids": [
                        *bounded_ids,
                        *(
                            atomic_id
                            for atomic_id in selection.supplemental_atomic_information_ids
                            if atomic_id not in bounded_ids
                        ),
                    ],
                    "allowed_object_ids": sorted(allowed_object_ids),
                    "evidence_view_refs": evidence_references,
                    "evidence_by_atomic": evidence_by_atomic,
                    "required_incomplete_reasons": list(
                        bundle.metadata.incomplete_reasons
                    ),
                }
        result = build_timelines(
            selections,
            contexts,
            CodexTimelineProvider(sdk_loader=_load_sdk),
            args.output_root,
            args.resume,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["complete"] else 1
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        TimelineError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: stage1-review build failed: {exc}")
        return 1


def _representation_command(args: argparse.Namespace) -> int:
    repository = LocalRepresentationRepository(args.representation_root)
    service = RepresentationService(
        LocalManagedSourceRepository(args.managed_root), repository
    )
    try:
        if args.representation_command == "build":
            adapter = production_adapter(args.adapter)
            configuration: dict[str, object] = {}
            if adapter.name == "image-preflight":
                configuration["privacy_route"] = args.privacy_route or "unknown"
            elif args.privacy_route is not None:
                raise RepresentationError(
                    "--privacy-route is only supported by image-preflight"
                )
            result = service.build(args.source_id, adapter, configuration)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.representation_command == "show":
            print(
                json.dumps(
                    service.show(args.representation_id).to_dict(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.representation_command == "list":
            print(
                json.dumps(
                    [item.to_dict() for item in service.list(args.source)],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.representation_command == "verify":
            result = service.verify(args.representation_id)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0 if result.verified else 1
        return 2  # pragma: no cover - argparse enforces this
    except (OSError, RepresentationError, SourceError, TypeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


def _conversation_command(args: argparse.Namespace) -> int:
    if (
        args.conversation_provider != "wechat"
        or args.conversation_command != "represent"
    ):
        return 2  # pragma: no cover - argparse enforces this
    repository = LocalRepresentationRepository(args.representation_root)
    try:
        result = RepresentationService(
            LocalManagedSourceRepository(args.managed_root), repository
        ).build(
            args.source_id,
            WechatConversationRepresentationAdapter(),
            {},
        )
        artifact = result.representation.artifacts[0]
        payload = json.loads(
            repository.read_artifact(
                result.representation.representation_id, artifact.artifact_id
            ).decode("utf-8")
        )
        output = {
            "status": result.status,
            "representation_id": result.representation.representation_id,
            "representation_kind": result.representation.kind,
            "metrics": wechat_conversation_metrics(payload),
            "semantic_provider_called": False,
            "atomic_information_written": False,
            "world_model_written": False,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RepresentationError,
        SourceError,
        WechatConversationError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}")
        return 1


def _wechat_product_command(args: argparse.Namespace) -> int:
    if args.wechat_command != "digest":  # pragma: no cover - argparse enforces this
        return 2
    maintenance_count = sum(
        (
            args.prepare_next_semantic,
            args.upgrade_active_v1,
            args.upgrade_active_v2_all_history,
            args.install_semantic_authority,
            args.install_semantic_authority_extension,
            args.install_semantic_maintenance_continuation,
            args.install_semantic_reviewed_head_continuation,
            args.install_semantic_gate_c_continuation,
            args.install_semantic_segmented_gate_c_continuation,
            args.activate_batch_governance,
            args.resolve_semantic_unknown,
            args.resolve_semantic_timeout_212,
            args.seal_governance_timeout,
            args.resolve_governance_startup_failure,
            args.resolve_failed_closed_continuation,
            args.resolve_multi_governance_startup_failure,
        )
    )
    if maintenance_count > 1 or (
        maintenance_count
        and any((args.since is not None, args.from_now, args.all_history))
    ):
        print("error: 恢复维护入口不能与首次起点或其他维护入口同时使用。")
        return 2
    if args.install_semantic_authority:
        if args.inventory_authority_file is None:
            print(
                "error: 安装 Semantic authority 必须指定私有 inventory authority file。"
            )
            return 2
    elif args.inventory_authority_file is not None:
        print("error: inventory authority file 只能与安装入口一起使用。")
        return 2
    if args.resolve_semantic_unknown:
        if args.semantic_unknown_authority_file is None:
            print("error: 恢复 Semantic unknown 必须指定私有 authority file。")
            return 2
    elif args.semantic_unknown_authority_file is not None:
        print("error: Semantic unknown authority file 只能与恢复入口一起使用。")
        return 2
    if args.resolve_semantic_timeout_212:
        if args.semantic_timeout_212_authority_file is None:
            print("error: 恢复 Semantic ordinal212 必须指定私有 authority file。")
            return 2
    elif args.semantic_timeout_212_authority_file is not None:
        print("error: Semantic ordinal212 authority file 只能与恢复入口一起使用。")
        return 2
    if (
        args.install_semantic_maintenance_continuation
        or args.install_semantic_reviewed_head_continuation
        or args.install_semantic_gate_c_continuation
        or args.install_semantic_segmented_gate_c_continuation
        or args.activate_batch_governance
        or args.resolve_governance_startup_failure
        or args.resolve_failed_closed_continuation
        or args.resolve_multi_governance_startup_failure
    ):
        if args.authority_ref is None:
            print("error: 安装 continuation 必须指定 authority ref。")
            return 2
    elif args.authority_ref is not None:
        print("error: authority ref 只能与 maintenance continuation 入口一起使用。")
        return 2
    if args.activate_batch_governance:
        if args.batch_governance_authority_file is None:
            print("error: Batch Governance activation 必须指定私有 authority file。")
            return 2
    elif args.batch_governance_authority_file is not None:
        print("error: Batch Governance authority file 只能与 activation 一起使用。")
        return 2
    if args.resolve_governance_startup_failure:
        if args.governance_startup_authority_file is None:
            print("error: Governance 启动恢复必须指定私有 authority file。")
            return 2
    elif args.governance_startup_authority_file is not None:
        print("error: Governance 启动恢复 authority file 只能与恢复入口一起使用。")
        return 2
    if args.resolve_failed_closed_continuation:
        if args.failed_closed_authority_file is None:
            print("error: 历史失败恢复必须指定私有 authority file。")
            return 2
    elif args.failed_closed_authority_file is not None:
        print("error: 历史失败恢复 authority file 只能与恢复入口一起使用。")
        return 2
    if args.resolve_multi_governance_startup_failure:
        if args.multi_governance_startup_authority_file is None:
            print("error: 多项目 Governance 启动恢复必须指定私有 authority file。")
            return 2
    elif args.multi_governance_startup_authority_file is not None:
        print("error: 多项目 Governance authority file 只能与恢复入口一起使用。")
        return 2
    try:
        workspace = require_workspace(args.config).workspace
        capture = WechatCliCaptureProvider(
            wechat_cli_binary=args.wechat_cli_bin,
            config_path=args.wechat_config,
        )

        def semantic_handoff() -> ExistingSemanticHandoff:
            provider_version = args.provider_version or detect_codex_provider_version(
                args.codex_bin
            )
            source_repository = LocalManagedSourceRepository(
                workspace / DEFAULT_MANAGED_SOURCE_ROOT
            )
            representation_repository = LocalRepresentationRepository(
                workspace / DEFAULT_REPRESENTATION_ROOT
            )
            return ExistingSemanticHandoff(
                source_repository=source_repository,
                representation_repository=representation_repository,
                information_root=workspace / DEFAULT_REPRESENTATION_INFORMATION_ROOT,
                information_store=JsonlAtomicInformationStore(
                    workspace / DEFAULT_ATOMIC_INFORMATION_STORE
                ),
                audit_root=workspace / DEFAULT_SEMANTIC_HANDOFF_AUDIT_ROOT,
                codex_binary=args.codex_bin,
                provider_version=provider_version,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                timeout_seconds=args.timeout_seconds,
                batch_size=args.batch_size,
            )

        service = WechatDigestService(
            workspace=workspace,
            capture_provider=capture,
            semantic_handoff_factory=semantic_handoff,
            interpretation_provider=CodexAtomicInformationInterpretationProvider(),
            semantic_batch_size=args.batch_size,
            semantic_parallelism=args.semantic_parallelism,
        )
        if args.prepare_next_semantic:
            prepared = service.prepare_next_semantic(batch_size=args.batch_size)
            print(
                json.dumps(
                    {
                        "run_id": prepared.run_id,
                        "representation_id": prepared.representation_id,
                        "anchor_unit_ids": list(prepared.anchor_unit_ids),
                        "semantic_provider_calls": prepared.semantic_provider_calls,
                        "governance_provider_calls": prepared.governance_provider_calls
                        if prepared.governance_provider_calls is not None
                        else "unavailable",
                        "checkpoint_published": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.upgrade_active_v1:
            print(
                json.dumps(
                    {
                        "run_id": service.upgrade_active_v1(),
                        "semantic_provider_calls": 0,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.upgrade_active_v2_all_history:
            print(
                json.dumps(
                    {
                        "run_id": service.upgrade_active_v2_all_history(),
                        "semantic_provider_calls": 0,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.install_semantic_authority:
            grant = service.install_semantic_authority(
                inventory_authority_file=args.inventory_authority_file,
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "baseline_total": grant["baseline_total"],
                        "max_new": grant["max_new"],
                        "absolute_cap": grant["absolute_cap"],
                        "global_authority_fingerprint": grant[
                            "global_authority_fingerprint"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.install_semantic_authority_extension:
            extension = service.install_semantic_authority_extension()
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "activation_total": extension["activation_total"],
                        "previous_absolute_cap": extension["previous_absolute_cap"],
                        "new_absolute_cap": extension["new_absolute_cap"],
                        "first_authorized_ordinal": extension[
                            "first_authorized_ordinal"
                        ],
                        "last_authorized_ordinal": extension["last_authorized_ordinal"],
                        "extension_fingerprint": extension["extension_fingerprint"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.install_semantic_maintenance_continuation:
            continuation = service.install_semantic_maintenance_continuation(
                authority_ref=args.authority_ref,
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "global_attempt_total": continuation["activation_total"],
                        "global_unknown": continuation["activation_unknown_count"],
                        "next_global_ordinal": continuation["next_global_ordinal"],
                        "absolute_cap": continuation["absolute_cap"],
                        "previous_reviewed_git_head": continuation[
                            "previous_reviewed_git_head"
                        ],
                        "reviewed_git_head": continuation["reviewed_git_head"],
                        "continuation_fingerprint": continuation[
                            "continuation_fingerprint"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.install_semantic_reviewed_head_continuation:
            continuation = (
                service.install_semantic_reviewed_head_continuation(
                    authority_ref=args.authority_ref,
                )
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "global_attempt_total": continuation[
                            "activation_total"
                        ],
                        "global_unknown": continuation[
                            "activation_unknown_count"
                        ],
                        "next_global_ordinal": continuation[
                            "next_global_ordinal"
                        ],
                        "absolute_cap": continuation["absolute_cap"],
                        "previous_reviewed_git_head": continuation[
                            "previous_reviewed_git_head"
                        ],
                        "reviewed_git_head": continuation[
                            "reviewed_git_head"
                        ],
                        "continuation_fingerprint": continuation[
                            "continuation_fingerprint"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.install_semantic_gate_c_continuation:
            continuation = service.install_semantic_gate_c_continuation(
                authority_ref=args.authority_ref,
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "global_attempt_total": continuation["activation_total"],
                        "global_unknown": continuation["activation_unknown_count"],
                        "next_global_ordinal": continuation["next_global_ordinal"],
                        "absolute_cap": continuation["absolute_cap"],
                        "previous_reviewed_git_head": continuation[
                            "previous_reviewed_git_head"
                        ],
                        "reviewed_git_head": continuation["reviewed_git_head"],
                        "continuation_fingerprint": continuation[
                            "continuation_fingerprint"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.install_semantic_segmented_gate_c_continuation:
            continuation = service.install_semantic_segmented_gate_c_continuation(
                authority_ref=args.authority_ref,
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "global_attempt_total": continuation["activation_total"],
                        "global_unknown": continuation["activation_unknown_count"],
                        "next_global_ordinal": continuation["next_global_ordinal"],
                        "absolute_cap": continuation["absolute_cap"],
                        "previous_reviewed_git_head": continuation[
                            "previous_reviewed_git_head"
                        ],
                        "reviewed_git_head": continuation["reviewed_git_head"],
                        "continuation_fingerprint": continuation[
                            "continuation_fingerprint"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.resolve_governance_startup_failure:
            resolution = service.resolve_governance_startup_failure(
                authority_ref=args.authority_ref,
                authority_manifest_file=(args.governance_startup_authority_file),
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "durable_information_preserved": 4,
                        "previous_governance_model_turns": 0,
                        "safe_restart_attempts_available": 1,
                        "objects_created": 0,
                        "checkpoint_published": False,
                        "resolution_receipt_fingerprint": resolution[
                            "receipt_fingerprint"
                        ],
                        "message": (
                            "已保留4条长期信息；上次治理未实际开始模型判断；"
                            "已允许一次安全续接；未创建对象、未推进checkpoint。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.resolve_failed_closed_continuation:
            resolution = service.resolve_failed_closed_continuation(
                authority_ref=args.authority_ref,
                authority_manifest_file=args.failed_closed_authority_file,
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "global_attempt_total": 298,
                        "global_unknown": 0,
                        "next_global_ordinal": 299,
                        "checkpoint_published": False,
                        "resolution_receipt_fingerprint": resolution[
                            "receipt_fingerprint"
                        ],
                        "message": (
                            "历史失败回执已重新纳入完整授权链；"
                            "当前项目已恢复为可续接状态，未处理下一项。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.resolve_multi_governance_startup_failure:
            progress_messages = {
                "capture_skipped": "恢复安装：复用已持久化证据，无需重新读取微信历史。",
                "verify": "恢复安装：正在核对持久化证据。",
                "write": "恢复安装：证据已通过，正在写入恢复许可。",
                "write_skipped": "恢复安装：恢复许可已存在，无需重复写入。",
                "readback": "恢复安装：正在读回并确认恢复状态。",
            }
            resolution = service.resolve_multi_governance_startup_failure(
                authority_ref=args.authority_ref,
                authority_manifest_file=(
                    args.multi_governance_startup_authority_file
                ),
                progress=lambda stage: print(
                    progress_messages[stage], file=sys.stderr, flush=True
                ),
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "durable_information_preserved": 3,
                        "previous_governance_model_turns": 0,
                        "safe_restart_attempts_available": 1,
                        "objects_created": 0,
                        "checkpoint_published": False,
                        "resolution_receipt_fingerprint": resolution[
                            "receipt_fingerprint"
                        ],
                        "message": (
                            "已保留现有长期信息；上次治理未进入模型判断；"
                            "已允许一次安全续接；未推进checkpoint。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.activate_batch_governance:
            migration = service.activate_batch_governance(
                authority_ref=args.authority_ref,
                authority_manifest_file=args.batch_governance_authority_file,
            )
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "completed_legacy": len(
                            migration["completed_atomic_information_ids"]
                        ),
                        "remaining_batch": len(
                            migration["remaining_atomic_information_ids"]
                        ),
                        "migration_fingerprint": migration["migration_fingerprint"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.resolve_semantic_unknown:
            resolution = service.resolve_semantic_unknown(
                authority_manifest_file=args.semantic_unknown_authority_file,
            )
            continuation = resolution["continuation"]
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "global_attempt_total": 166,
                        "global_unknown": 0,
                        "absolute_cap": 1000,
                        "remaining": 834,
                        "next_global_ordinal": continuation["next_global_ordinal"],
                        "failed_closed": 1,
                        "preserved_but_unabsorbed": 1,
                        "resolution_receipt_fingerprint": resolution[
                            "resolution_receipt_fingerprint"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.resolve_semantic_timeout_212:
            resolution = service.resolve_semantic_timeout_212(
                authority_manifest_file=(args.semantic_timeout_212_authority_file),
            )
            continuation = resolution["continuation"]
            print(
                json.dumps(
                    {
                        "semantic_provider_calls": 0,
                        "governance_provider_calls": 0,
                        "global_attempt_total": 212,
                        "global_unknown": 0,
                        "absolute_cap": 1000,
                        "remaining": 788,
                        "next_global_ordinal": continuation["next_global_ordinal"],
                        "failed_closed": 1,
                        "preserved_but_unabsorbed": 1,
                        "resolution_receipt_fingerprint": resolution[
                            "resolution_receipt_fingerprint"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.seal_governance_timeout:
            resolution = service.seal_governance_timeout()
            print(json.dumps(resolution, ensure_ascii=False, indent=2))
            return 0
        result = service.run(
            since=args.since,
            from_now=args.from_now,
            all_history=args.all_history,
            max_terminal_items=args.max_items_per_run,
        )
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
        WechatDigestError,
    ) as exc:
        print(f"error: {exc}")
        return 1
    checkpoint = "已推进" if result.checkpoint_published else "未推进"
    print(f"新增消息：{result.new_messages}")
    print(f"新增附件：{result.new_attachments}")
    print(f"已形成长期信息：{result.durable_information}")
    print(f"本地保留（隐私）：{result.local_only}")
    print(f"暂不支持：{result.unsupported}")
    print(f"待你判断：{result.pending_human}")
    print(f"已保留但未形成长期信息：{result.semantic_preserved_but_unabsorbed}")
    print(
        f"已形成长期信息但治理未完整确认：{result.governance_preserved_but_incomplete}"
    )
    print(f"更新了 {result.context_objects} 个长期对象的 Context")
    print(f"治理 app-server 启动：{result.governance_app_server_starts}")
    print(
        f"治理 thread / turn：{result.governance_threads} / {result.governance_turns}"
    )
    print(
        "治理耗时（ms）："
        f"startup={result.governance_startup_wall_ms}, "
        f"turn_sum={result.governance_turn_wall_ms_sum}, "
        f"turn_max={result.governance_turn_wall_ms_max}, "
        f"total={result.governance_wall_ms}"
    )
    print(
        "治理 timeout / failure："
        f"{result.governance_timeouts} / {result.governance_failures}"
    )
    print(
        "性能指标："
        + json.dumps(
            {
                "upper_bound_probe_calls": result.upper_bound_probe_calls,
                "capture_provider_calls": result.capture_provider_calls,
                "completed_window_connector_replays": (
                    result.completed_window_connector_replays
                ),
                "snapshot_bytes": result.snapshot_bytes,
                "capture_ms": result.capture_ms,
                "snapshot_publish_ms": result.snapshot_publish_ms,
                "snapshot_readback_ms": result.snapshot_readback_ms,
                "slice_build_ms": result.slice_build_ms,
                "semantic_parallelism": result.semantic_parallelism,
                "semantic_peak_concurrency": result.semantic_peak_concurrency,
                "semantic_wall_ms": result.semantic_wall_ms,
                "semantic_serial_estimate_ms": (
                    result.semantic_serial_estimate_ms
                ),
                "governance_peak_concurrency": (
                    result.governance_peak_concurrency
                ),
                "resume_provider_calls": result.resume_provider_calls,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    print(f"checkpoint：{checkpoint}")
    if result.segment_safe_stopped:
        print(
            "本段已安全完成："
            f"{result.segment_items_completed} 项；"
            f"当前窗口剩余 {result.segment_remaining_items} 项。"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"init", "doctor", "config", "integration"}:
        return _workspace_command(args)
    if args.command == "mcp":
        return _mcp_command(args)
    try:
        _resolve_durable_paths(args)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
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
    if args.command == "stage1-review":
        return _stage1_review_command(args)
    if args.command == "source":
        return _source_command(args)
    if args.command == "representation":
        return _representation_command(args)
    if args.command == "conversation":
        return _conversation_command(args)
    if args.command == "wechat":
        return _wechat_product_command(args)
    return 2  # pragma: no cover - argparse enforces this
