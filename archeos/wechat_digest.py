"""Incremental, replay-safe WeChat digestion orchestration.

The module is a Processing workflow.  Its run ledger and checkpoint are
technical ``Processing Run`` state; they do not introduce Conversation,
Message, Attachment, or checkpoint as Core business concepts.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import mimetypes
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .atomic_information import JsonlAtomicInformationStore
from .atomic_information.ingestion import _load_candidates
from .consolidation import BoundedInformationCandidateRetriever
from .context import ContextBuilder, ContextRequest
from .digestion import (
    AtomicInformationDigestionService,
    AtomicInformationInterpretationProvider,
    BusinessLanguageHumanJudgmentPort,
    InterpretationResult,
    JsonlChangeJournal,
    JsonlChangeProposalStore,
    WorldModelOperation,
)
from .digestion.providers import interpretation_to_dict, parse_interpretation
from .emergence import IdentityEvidence, IdentityGateService
from .representation import (
    LocalRepresentationRepository,
    RepresentationError,
    RepresentationService,
    WechatConversationV2RepresentationAdapter,
)
from .representation.identity import (
    canonical_configuration_fingerprint,
    representation_id,
)
from .representation.registry import production_adapter
from .representation_information import (
    DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
    DEFAULT_SEMANTIC_MODEL,
    DEFAULT_SEMANTIC_REASONING_EFFORT,
    CodexCliRepresentationAnalysisProvider,
    RepresentationInformationError,
    RepresentationInformationService,
    _analysis_batches,
    _units_from_representation,
    resolve_codex_executable_identity,
    validate_representation_information_package,
)
from .semantic_handoff import (
    ExternalAgentSemanticHandoffService,
    SemanticCampaignAuthorityBinding,
    SemanticCompletedWindowBinding,
    SemanticHandoffError,
    SemanticPrivacyBinding,
    SemanticResultOnlyRequest,
    SemanticWindowAuthorityBinding,
    _package_fingerprint,
    validate_completed_published_audits,
)
from .source import LocalManagedSourceRepository, ManagedSourceService
from .source.local_repository import SourceNotFoundError
from .world_model import ObjectResolver, SQLiteWorldModelRepository

CHECKPOINT_SCHEMA_VERSION = "wechat-digest-checkpoint/1.0"
RUN_PLAN_SCHEMA_VERSION = "wechat-digest-run-plan/3.0"
SNAPSHOT_RUN_PLAN_SCHEMA_VERSION = "wechat-digest-run-plan/4.0"
PREVIOUS_RUN_PLAN_SCHEMA_VERSION = "wechat-digest-run-plan/2.0"
LEGACY_RUN_PLAN_SCHEMA_VERSION = "wechat-digest-run-plan/1.0"
RUN_STATUS_SCHEMA_VERSION = "wechat-digest-run-status/1.0"
RUN_PLAN_RECEIPT_SCHEMA_VERSION = "wechat-digest-run-plan-receipt/2.0"
LEGACY_RUN_PLAN_RECEIPT_SCHEMA_VERSION = "wechat-digest-run-plan-receipt/1.0"
RUN_SEGMENT_RECEIPT_SCHEMA_VERSION = "wechat-digest-run-segment-receipt/1.0"
GOVERNANCE_RECEIPT_SCHEMA_VERSION = "wechat-governance-receipt/2.0"
LEGACY_GOVERNANCE_RECEIPT_SCHEMA_VERSION = "wechat-governance-receipt/1.0"
GOVERNANCE_MIGRATION_SCHEMA_VERSION = "wechat-governance-migration/1.0"
GOVERNANCE_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION = (
    "wechat-governance-startup-recovery-authority/1.0"
)
GOVERNANCE_ITEM_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION = (
    "wechat-governance-startup-recovery-authority/2.0"
)
GOVERNANCE_MULTI_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION = (
    "wechat-governance-multi-startup-recovery-authority/1.0"
)
GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION = (
    "wechat-governance-startup-recovery/1.0"
)
GOVERNANCE_STARTUP_RETRY_SCHEMA_VERSION = (
    "wechat-governance-startup-retry/1.0"
)
GOVERNANCE_STARTUP_RECOVERY_DIRECTORY = "governance-startup-recoveries"
GOVERNANCE_STARTUP_RETRY_DIRECTORY = "governance-startup-retries"
FAILED_CLOSED_RECOVERY_MANIFEST_SCHEMA_VERSION = (
    "wechat-failed-closed-recovery-authority/1.0"
)
FAILED_CLOSED_RECOVERY_SCHEMA_VERSION = (
    "wechat-failed-closed-recovery/1.0"
)
BATCH_GOVERNANCE_AUTHORITY_SCHEMA_VERSION = (
    "wechat-batch-governance-migration-authority/1.0"
)
CAPTURE_SCHEMA_VERSION = "wechat-cli-capture/1.0"
SUPPORTED_WECHAT_CLI_VERSION = "0.5.0"
DEFAULT_CAPTURE_WINDOW_DAYS = 30
DEFAULT_CAPTURE_WINDOW_MESSAGES = 1000
TERMINAL_ITEM_STATES = frozenset(
    {"processed", "local_only", "unsupported", "pending_human", "failed_closed"}
)
SEMANTIC_ATTACHMENT_ADAPTERS = {
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/plain": "markdown",
    "application/pdf": "pdf-text",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": "pptx",
}
IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif"})
SEMANTIC_PRIVACY_POLICY = "wechat-local-deterministic-privacy-gate"
SEMANTIC_PRIVACY_POLICY_VERSION = "1.0"
CAPTURE_SNAPSHOT_SCHEMA_VERSION = "wechat-digest-capture-snapshot/1.0"
CAPTURE_INDEX_SCHEMA_VERSION = "wechat-digest-capture-index/1.0"
CAPTURE_SUMMARY_SCHEMA_VERSION = "wechat-digest-capture-summary/1.0"
CAPTURE_RECEIPT_SCHEMA_VERSION = "wechat-digest-capture-receipt/1.0"
CAPTURE_PENDING_SCHEMA_VERSION = "wechat-digest-capture-pending/1.0"


GOVERNANCE_METRIC_COUNTERS = (
    "app_server_start_count",
    "thread_count",
    "turn_count",
    "startup_wall_ms",
    "turn_wall_ms_sum",
    "turn_wall_ms_max",
    "governance_wall_ms",
    "timeout_count",
    "failure_count",
)


class WechatDigestError(RuntimeError):
    """The bounded run did not safely converge; checkpoint remains unchanged."""


@dataclass(frozen=True)
class WechatSemanticPreparation:
    """A local-only handoff point for the next deterministic semantic batch."""

    run_id: str
    representation_id: str
    anchor_unit_ids: tuple[str, ...]
    semantic_provider_calls: int = 0
    governance_provider_calls: int | None = None


@dataclass(frozen=True, order=True)
class WechatCursor:
    timestamp: int
    conversation_key: str
    message_key: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, int)
            or self.timestamp < 0
            or not isinstance(self.conversation_key, str)
            or not isinstance(self.message_key, str)
        ):
            raise ValueError("WeChat cursor is invalid")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object, field: str = "cursor") -> WechatCursor:
        if not isinstance(value, dict) or set(value) != {
            "timestamp",
            "conversation_key",
            "message_key",
        }:
            raise ValueError(f"{field} is invalid")
        return cls(
            value["timestamp"], value["conversation_key"], value["message_key"]
        )  # type: ignore[arg-type]


ZERO_CURSOR = WechatCursor(0, "", "")


@dataclass(frozen=True)
class CapturedAttachment:
    attachment_key: str
    status: str
    filename_hint: str
    media_type: str
    path: Path | None
    content_hash: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class CapturedMessage:
    conversation_key: str
    provider_conversation_id: str
    conversation_label: str
    is_group: bool
    message_key: str
    cursor: WechatCursor
    sender_label: str
    message_type: str
    timestamp: int
    sent_at: str
    visible_content: str
    structured_payload: str
    attachments: tuple[CapturedAttachment, ...]


@dataclass(frozen=True)
class WechatCapture:
    provider_version: str
    after_cursor: WechatCursor
    upper_bound: WechatCursor
    messages: tuple[CapturedMessage, ...]


class WechatCaptureProvider(Protocol):
    provider_version: str

    def capture(
        self,
        after_cursor: WechatCursor,
        *,
        upper_bound: WechatCursor | None = None,
        all_history_upper_bound: WechatCursor | None = None,
        observe_only: bool = False,
    ) -> WechatCapture: ...


class SemanticHandoffPort(Protocol):
    provider: CodexCliRepresentationAnalysisProvider
    reviewed_git_head: str

    def execute(
        self,
        representation_id: str,
        *,
        privacy_binding: SemanticPrivacyBinding,
        authority_binding: SemanticWindowAuthorityBinding | None,
    ): ...

    def prepare_results(
        self,
        requests: Sequence[SemanticResultOnlyRequest],
        *,
        parallelism: int,
    ) -> dict[str, int]: ...

    def inspect_recovery_wave(
        self,
        requests: Sequence[SemanticResultOnlyRequest],
    ) -> tuple[dict[str, object], ...]: ...

    def validate_pre_attempt_inventory(
        self,
        representation_id: str,
        *,
        privacy_binding: SemanticPrivacyBinding,
    ) -> dict[str, object]: ...

    def install_global_authority(
        self,
        *,
        inventory_authority_file: Path,
        window_binding: SemanticWindowAuthorityBinding,
    ) -> dict[str, object]: ...

    def install_global_authority_extension(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
    ) -> dict[str, object]: ...

    def install_maintenance_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]: ...

    def install_reviewed_head_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        active_run_binding: Mapping[str, object],
    ) -> dict[str, object]: ...

    def install_batch_governance_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]: ...

    def install_gate_c_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]: ...

    def install_segmented_gate_c_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]: ...

    def install_governance_startup_recovery_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object]: ...

    def governance_startup_recovery_continuation(
        self,
        *,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object] | None: ...

    def install_multi_governance_startup_recovery_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object]: ...

    def multi_governance_startup_recovery_continuation(
        self,
        *,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object] | None: ...

    def install_failed_closed_recovery_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object]: ...

    def failed_closed_recovery_continuation(
        self,
        *,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object] | None: ...

    def resolve_unknown(
        self,
        *,
        authority_manifest_file: Path,
        digest_binding: Mapping[str, object],
        commit_failed_closed_status: Callable[
            [str], tuple[str, Mapping[str, object]]
        ],
    ) -> dict[str, object]: ...

    def validate_unknown_resolution_digest(
        self,
        *,
        digest_binding: Mapping[str, object],
        failed_closed_status_fingerprint: str,
        resolution_id: str,
    ) -> dict[str, object]: ...

    def resolve_timeout_212(
        self,
        *,
        authority_manifest_file: Path,
        digest_binding: Mapping[str, object],
        commit_failed_closed_status: Callable[
            [str], tuple[str, Mapping[str, object]]
        ],
    ) -> dict[str, object]: ...

    def resolve_attempt(
        self,
        *,
        authority_manifest_file: Path,
        digest_binding: Mapping[str, object],
        commit_failed_closed_status: Callable[
            [str, int], tuple[str, Mapping[str, object]]
        ],
    ) -> dict[str, object]: ...

    def build_attempt_resolution_manifest(
        self,
        *,
        candidate_file: Path,
        authority_ref: str,
        observed_at: str,
        digest_binding: Mapping[str, object],
    ) -> dict[str, object]: ...

    def validate_attempt_resolution_digest(
        self,
        *,
        digest_binding: Mapping[str, object],
        failed_closed_status_fingerprint: str,
        resolution_id: str,
    ) -> dict[str, object]: ...

    def validate_timeout_212_resolution_digest(
        self,
        *,
        digest_binding: Mapping[str, object],
        failed_closed_status_fingerprint: str,
        resolution_id: str,
    ) -> dict[str, object]: ...

    def global_campaign_binding(
        self,
    ) -> SemanticCampaignAuthorityBinding | None: ...

    def global_attempt_summary(self, representation_id: str) -> dict[str, int]: ...

    def governance_startup_recovery_snapshot(
        self, representation_id: str
    ) -> dict[str, object]: ...


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_private_json_manifest(path: Path) -> tuple[dict[str, object], str]:
    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink():
            raise WechatDigestError("私有 authority manifest 不得使用符号链接。")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

        def read_once() -> bytes:
            descriptor = os.open(candidate, flags)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise WechatDigestError(
                        "私有 authority manifest 必须是 0600 普通文件。"
                    )
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

        raw = read_once()
        if read_once() != raw:
            raise WechatDigestError("私有 authority manifest 读回不一致。")
        parsed = json.loads(raw.decode("utf-8"))
    except WechatDigestError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WechatDigestError("私有 authority manifest 不可读。") from exc
    if not isinstance(parsed, dict):
        raise WechatDigestError("私有 authority manifest 结构损坏。")
    return dict(parsed), _sha256_bytes(raw)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _hash_file(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise WechatDigestError("微信附件无法精确读取；未推进 checkpoint。")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_openai_codex_sdk() -> None:
    """Fail before recovery writes when the pinned Governance SDK is unavailable."""

    try:
        installed = importlib.metadata.version("openai-codex")
        if installed != "0.144.4":
            raise RuntimeError(
                "openai-codex==0.144.4 is required for Governance recovery"
            )
        importlib.import_module("openai_codex")
    except (importlib.metadata.PackageNotFoundError, ImportError) as exc:
        raise RuntimeError(
            "openai-codex==0.144.4 is required for Governance recovery"
        ) from exc
def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


class WechatCliCaptureProvider:
    """Pinned adapter over the local provider runtime without progress writes."""

    def __init__(
        self,
        *,
        wechat_cli_binary: str = "wechat-cli",
        config_path: Path | None = None,
        timeout_seconds: float = 300.0,
        window_days: int = DEFAULT_CAPTURE_WINDOW_DAYS,
        window_message_limit: int = DEFAULT_CAPTURE_WINDOW_MESSAGES,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not wechat_cli_binary.strip():
            raise ValueError("wechat_cli_binary must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("capture timeout must be positive")
        if (
            isinstance(window_days, bool)
            or not isinstance(window_days, int)
            or not 1 <= window_days <= 366
        ):
            raise ValueError("window_days must be between 1 and 366")
        if (
            isinstance(window_message_limit, bool)
            or not isinstance(window_message_limit, int)
            or window_message_limit < 1
        ):
            raise ValueError("window_message_limit must be positive")
        self.wechat_cli_binary = wechat_cli_binary
        self.config_path = None if config_path is None else Path(config_path)
        self.timeout_seconds = float(timeout_seconds)
        self.window_days = window_days
        self.window_message_limit = window_message_limit
        self.runner = runner
        self._executable = self._resolve_executable(wechat_cli_binary)
        self._python = self._resolve_python(self._executable)
        self.provider_version = self._read_version()
        self.capture_attempts = 0
        self.capture_successes = 0
        self.last_capture_metrics = {
            "materialized_cursor_rows": 0,
            "cursor_discovery_ms": 0,
        }
        if self.provider_version != SUPPORTED_WECHAT_CLI_VERSION:
            raise WechatDigestError(
                "微信只读连接器版本未经验证；未读取消息。"
            )

    @staticmethod
    def _resolve_executable(value: str) -> Path:
        from shutil import which

        resolved = which(value)
        if resolved is None:
            raise WechatDigestError("未找到本机微信只读连接器。")
        path = Path(resolved).resolve()
        if not path.is_file():
            raise WechatDigestError("本机微信只读连接器不可执行。")
        return path

    @staticmethod
    def _resolve_python(executable: Path) -> Path:
        try:
            first_line = executable.read_bytes().splitlines()[0].decode("utf-8")
        except (OSError, UnicodeDecodeError, IndexError) as exc:
            raise WechatDigestError("无法验证微信只读连接器 runtime。") from exc
        if not first_line.startswith("#!"):
            raise WechatDigestError("无法验证微信只读连接器 runtime。")
        interpreter = Path(first_line[2:].strip())
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise WechatDigestError("无法验证微信只读连接器 runtime。")
        return interpreter

    def _read_version(self) -> str:
        try:
            result = self.runner(
                [str(self._executable), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WechatDigestError("无法验证微信只读连接器版本。") from exc
        match = re.search(r"version\s+([0-9]+(?:\.[0-9]+)+)", result.stdout)
        if result.returncode != 0 or match is None:
            raise WechatDigestError("无法验证微信只读连接器版本。")
        return match.group(1)

    def capture(
        self,
        after_cursor: WechatCursor,
        *,
        upper_bound: WechatCursor | None = None,
        all_history_upper_bound: WechatCursor | None = None,
        observe_only: bool = False,
    ) -> WechatCapture:
        if upper_bound is not None and all_history_upper_bound is not None:
            raise WechatDigestError("微信捕获边界冲突；未推进 checkpoint。")
        request = {
            "config_path": None if self.config_path is None else str(self.config_path),
            "after_cursor": after_cursor.to_dict(),
            "upper_bound": None if upper_bound is None else upper_bound.to_dict(),
            "all_history_upper_bound": (
                None
                if all_history_upper_bound is None
                else all_history_upper_bound.to_dict()
            ),
            "observe_only": observe_only,
            "window_days": self.window_days,
            "window_message_limit": self.window_message_limit,
        }
        helper = Path(__file__).with_name("wechat_capture_helper.py")
        self.capture_attempts += 1
        try:
            result = self.runner(
                [str(self._python), str(helper)],
                input=_canonical_json(request),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WechatDigestError("微信只读捕获失败；未推进 checkpoint。") from exc
        if result.returncode != 0:
            raise WechatDigestError("微信只读捕获失败；未推进 checkpoint。")
        try:
            payload = json.loads(result.stdout)
            capture = self._parse_capture(payload, after_cursor)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise WechatDigestError("微信只读捕获结果不完整；未推进 checkpoint。") from exc
        if upper_bound is not None and capture.upper_bound != upper_bound:
            raise WechatDigestError("微信重放范围发生变化；未推进 checkpoint。")
        if (
            all_history_upper_bound is not None
            and capture.upper_bound > all_history_upper_bound
        ):
            raise WechatDigestError("微信全历史边界发生漂移；未推进 checkpoint。")
        if observe_only and capture.messages:
            raise WechatDigestError("微信只读边界观察返回了消息内容；未推进 checkpoint。")
        self.capture_successes += 1
        return capture

    def _parse_capture(
        self, payload: object, after_cursor: WechatCursor
    ) -> WechatCapture:
        required_fields = {
            "schema_version",
            "observed_upper",
            "messages",
        }
        if (
            not isinstance(payload, dict)
            or not required_fields <= set(payload)
            or set(payload) - required_fields not in (set(), {"metrics"})
        ):
            raise ValueError("capture payload is invalid")
        if payload["schema_version"] != CAPTURE_SCHEMA_VERSION:
            raise ValueError("capture schema is unsupported")
        upper = WechatCursor.from_dict(payload["observed_upper"], "observed_upper")
        if upper < after_cursor:
            raise ValueError("capture upper bound precedes checkpoint")
        values = payload["messages"]
        if not isinstance(values, list):
            raise TypeError("capture messages are invalid")
        messages = tuple(self._parse_message(item) for item in values)
        if tuple(sorted(message.cursor for message in messages)) != tuple(
            message.cursor for message in messages
        ):
            raise ValueError("capture messages are not strictly ordered")
        if len({message.message_key for message in messages}) != len(messages):
            raise ValueError("capture messages contain duplicate identities")
        if any(not after_cursor < message.cursor <= upper for message in messages):
            raise ValueError("capture message escaped its fixed range")
        metrics = payload.get(
            "metrics",
            {"materialized_cursor_rows": 0, "cursor_discovery_ms": 0},
        )
        if (
            not isinstance(metrics, dict)
            or set(metrics)
            != {"materialized_cursor_rows", "cursor_discovery_ms"}
            or any(
                isinstance(metrics[field], bool)
                or not isinstance(metrics[field], int)
                or metrics[field] < 0
                for field in metrics
            )
        ):
            raise ValueError("capture metrics are invalid")
        self.last_capture_metrics = dict(metrics)
        return WechatCapture(
            self.provider_version, after_cursor, upper, messages
        )

    @staticmethod
    def _parse_message(value: object) -> CapturedMessage:
        fields = {
            "conversation_key",
            "provider_conversation_id",
            "conversation_label",
            "is_group",
            "message_key",
            "cursor",
            "sender_label",
            "message_type",
            "timestamp",
            "sent_at",
            "visible_content",
            "structured_payload",
            "attachments",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("capture message is invalid")
        strings = (
            value["conversation_key"],
            value["provider_conversation_id"],
            value["conversation_label"],
            value["message_key"],
            value["sender_label"],
            value["message_type"],
            value["sent_at"],
            value["visible_content"],
            value["structured_payload"],
        )
        if any(not isinstance(item, str) for item in strings):
            raise ValueError("capture message strings are invalid")
        if not isinstance(value["is_group"], bool):
            raise TypeError("capture conversation type is invalid")
        if isinstance(value["timestamp"], bool) or not isinstance(
            value["timestamp"], int
        ):
            raise TypeError("capture timestamp is invalid")
        attachments = value["attachments"]
        if not isinstance(attachments, list):
            raise TypeError("capture attachments are invalid")
        parsed_attachments = tuple(
            WechatCliCaptureProvider._parse_attachment(item) for item in attachments
        )
        return CapturedMessage(
            conversation_key=value["conversation_key"],
            provider_conversation_id=value["provider_conversation_id"],
            conversation_label=value["conversation_label"],
            is_group=value["is_group"],
            message_key=value["message_key"],
            cursor=WechatCursor.from_dict(value["cursor"], "message.cursor"),
            sender_label=value["sender_label"],
            message_type=value["message_type"],
            timestamp=value["timestamp"],
            sent_at=value["sent_at"],
            visible_content=value["visible_content"],
            structured_payload=value["structured_payload"],
            attachments=parsed_attachments,
        )

    @staticmethod
    def _parse_attachment(value: object) -> CapturedAttachment:
        if not isinstance(value, dict) or set(value) != {
            "attachment_key",
            "status",
            "filename_hint",
            "media_type",
            "path",
        }:
            raise ValueError("capture attachment is invalid")
        if value["status"] not in {"available", "missing", "ambiguous"}:
            raise ValueError("capture attachment status is invalid")
        for field in ("attachment_key", "filename_hint", "media_type"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError("capture attachment metadata is invalid")
        raw_path = value["path"]
        if raw_path is not None and not isinstance(raw_path, str):
            raise ValueError("capture attachment path is invalid")
        path = None if raw_path is None else Path(raw_path)
        if value["status"] == "available":
            if path is None or not path.is_absolute():
                raise ValueError("available attachment has no exact path")
            content_hash, size_bytes = _hash_file(path)
        else:
            if path is not None:
                raise ValueError("unavailable attachment must not expose a path")
            content_hash, size_bytes = None, None
        return CapturedAttachment(
            value["attachment_key"],
            value["status"],
            value["filename_hint"],
            value["media_type"],
            path,
            content_hash,
            size_bytes,
        )


@dataclass(frozen=True)
class PrivacyDecision:
    route: str
    categories: tuple[str, ...]


class DeterministicPrivacyGate:
    """Local non-LLM deny gate; it never returns matched private text."""

    _PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "credential_or_secret",
            re.compile(
                r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
                r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password)\b\s*[:=]",
                re.IGNORECASE,
            ),
        ),
        (
            "government_identity_document",
            re.compile(
                r"身份证|护照号码|居民身份证|\b\d{17}[0-9Xx]\b"
            ),
        ),
        (
            "bank_or_payment_credential",
            re.compile(
                r"银行卡号|信用卡号|支付密码|\bCVV\b|\bCVC\b",
                re.IGNORECASE,
            ),
        ),
        (
            "health",
            re.compile(r"病历|诊断证明|体检报告|处方|住院记录|medical record", re.IGNORECASE),
        ),
        (
            "minors",
            re.compile(r"未成年人|未满十八|儿童病历|学生身份证|minor child", re.IGNORECASE),
        ),
        (
            "high_sensitive_hr",
            re.compile(r"薪资明细|工资单|绩效处分|辞退材料|背景调查|人事档案", re.IGNORECASE),
        ),
        (
            "privileged",
            re.compile(r"律师保密|律师客户特权|法律意见书|attorney.client privilege", re.IGNORECASE),
        ),
        (
            "external_processing_prohibited",
            re.compile(
                r"保密协议|不得外传|禁止上传|禁止外部处理|仅限内部|confidential.only|\bNDA\b",
                re.IGNORECASE,
            ),
        ),
        (
            "unresolved_high_sensitivity",
            re.compile(r"绝密|机密材料|高度敏感|最高密级", re.IGNORECASE),
        ),
    )

    def evaluate(
        self, texts: Sequence[str], *, semantic_completeness_known: bool = True
    ) -> PrivacyDecision:
        categories = {
            category
            for text in texts
            for category, pattern in self._PATTERNS
            if pattern.search(text)
        }
        if not semantic_completeness_known:
            categories.add("unresolved_high_sensitivity")
        return PrivacyDecision(
            "approved" if not categories else "local_only",
            tuple(sorted(categories)),
        )


class WechatDigestRunStore:
    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], str] = _utc_now,
        before_checkpoint_publish: Callable[[], None] | None = None,
        before_upgrade_status_write: Callable[[], None] | None = None,
        after_upgrade_pending_receipt_write: Callable[[], None] | None = None,
        before_upgrade_commit_receipt_write: Callable[[], None] | None = None,
        after_upgrade_receipt_write: Callable[[], None] | None = None,
        before_create_status_write: Callable[[], None] | None = None,
        before_create_receipt_write: Callable[[], None] | None = None,
        before_create_active_write: Callable[[], None] | None = None,
        before_segment_receipt_write: Callable[[], None] | None = None,
        after_capture_snapshot_write: Callable[[], None] | None = None,
        after_capture_index_write: Callable[[], None] | None = None,
        after_capture_summary_write: Callable[[], None] | None = None,
        after_capture_receipt_write: Callable[[], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.runs_root = self.root / "runs"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.active_path = self.root / "active.json"
        self.lock_path = self.root / ".digest.lock"
        self.clock = clock
        self.before_checkpoint_publish = before_checkpoint_publish
        self.before_upgrade_status_write = before_upgrade_status_write
        self.after_upgrade_pending_receipt_write = (
            after_upgrade_pending_receipt_write
        )
        self.before_upgrade_commit_receipt_write = (
            before_upgrade_commit_receipt_write
        )
        self.after_upgrade_receipt_write = after_upgrade_receipt_write
        self.before_create_status_write = before_create_status_write
        self.before_create_receipt_write = before_create_receipt_write
        self.before_create_active_write = before_create_active_write
        self.before_segment_receipt_write = before_segment_receipt_write
        self.after_capture_snapshot_write = after_capture_snapshot_write
        self.after_capture_index_write = after_capture_index_write
        self.after_capture_summary_write = after_capture_summary_write
        self.after_capture_receipt_write = after_capture_receipt_write

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WechatDigestError("已有微信消化任务正在运行。") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def checkpoint(self) -> WechatCursor | None:
        if not self.checkpoint_path.exists():
            return None
        value = self._read_json(self.checkpoint_path)
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "cursor",
            "published_at",
            "run_id",
        } or value["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
            raise WechatDigestError("微信 checkpoint 损坏；未读取新消息。")
        return WechatCursor.from_dict(value["cursor"], "checkpoint.cursor")

    def active_run_id(self) -> str | None:
        if not self.active_path.exists():
            return None
        value = self._read_json(self.active_path)
        if not isinstance(value, dict) or set(value) != {"active_run_id"}:
            raise WechatDigestError("微信运行恢复状态损坏。")
        run_id = value["active_run_id"]
        if run_id is not None and (
            not isinstance(run_id, str) or not re.fullmatch(r"run_[0-9a-f]{32}", run_id)
        ):
            raise WechatDigestError("微信运行恢复状态损坏。")
        return run_id

    def plan(self, run_id: str) -> dict[str, object]:
        value = self._read_json(self.runs_root / run_id / "plan.json")
        if not isinstance(value, dict) or value.get("schema_version") not in {
            SNAPSHOT_RUN_PLAN_SCHEMA_VERSION,
            RUN_PLAN_SCHEMA_VERSION,
            PREVIOUS_RUN_PLAN_SCHEMA_VERSION,
            LEGACY_RUN_PLAN_SCHEMA_VERSION,
        }:
            raise WechatDigestError("微信运行计划损坏。")
        return value

    def plan_receipt(self, run_id: str) -> dict[str, object]:
        value = self._read_json(self.runs_root / run_id / "run-plan-receipt.json")
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise WechatDigestError("微信运行计划 receipt 损坏。")
        if value.get("schema_version") == LEGACY_RUN_PLAN_RECEIPT_SCHEMA_VERSION:
            if set(value) != {"schema_version", "run_id", "plan_fingerprint"}:
                raise WechatDigestError("微信运行计划 receipt 损坏。")
        elif value.get("schema_version") == RUN_PLAN_RECEIPT_SCHEMA_VERSION:
            phase = value.get("phase")
            expected = (
                {"schema_version", "run_id", "phase", "plan_fingerprint"}
                if phase == "committed"
                else {
                    "schema_version",
                    "run_id",
                    "phase",
                    "previous_plan_fingerprint",
                    "all_history_upper_bound",
                    "target_plan_fingerprint",
                }
            )
            if phase not in {"pending", "committed"} or set(value) != expected:
                raise WechatDigestError("微信运行计划 receipt 损坏。")
            if phase == "pending":
                try:
                    WechatCursor.from_dict(
                        value["all_history_upper_bound"],
                        "receipt.all_history_upper_bound",
                    )
                except (TypeError, ValueError) as exc:
                    raise WechatDigestError(
                        "微信运行计划 receipt 损坏。"
                    ) from exc
        else:
            raise WechatDigestError("微信运行计划 receipt 损坏。")
        fingerprints = (
            (value.get("plan_fingerprint"),)
            if value.get("schema_version")
            == LEGACY_RUN_PLAN_RECEIPT_SCHEMA_VERSION
            or value.get("phase") == "committed"
            else (
                value.get("previous_plan_fingerprint"),
                value.get("target_plan_fingerprint"),
            )
        )
        if any(
            (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
            )
            for fingerprint in fingerprints
        ):
            raise WechatDigestError("微信运行计划 receipt 损坏。")
        return value

    def has_plan_receipt(self, run_id: str) -> bool:
        return (self.runs_root / run_id / "run-plan-receipt.json").exists()

    def status(self, run_id: str) -> dict[str, object]:
        value = self._read_json(self.runs_root / run_id / "status.json")
        if not isinstance(value, dict) or value.get("schema_version") != RUN_STATUS_SCHEMA_VERSION:
            raise WechatDigestError("微信运行状态损坏。")
        return value

    def capture_receipt(self, run_id: str) -> dict[str, object] | None:
        path = self.runs_root / run_id / "capture" / "receipt.json"
        if not os.path.lexists(path):
            return None
        return self._private_receipt(
            path,
            schema_version=CAPTURE_RECEIPT_SCHEMA_VERSION,
            label="微信 capture receipt",
        )

    def publish_capture_pending(
        self,
        plan: Mapping[str, object],
        capture: WechatCapture,
        *,
        capture_ms: int,
    ) -> dict[str, object]:
        run_id = plan.get("run_id")
        created_at = plan.get("created_at")
        if (
            not isinstance(run_id, str)
            or re.fullmatch(r"run_[0-9a-f]{32}", run_id) is None
            or not isinstance(created_at, str)
            or plan.get("schema_version") != RUN_PLAN_SCHEMA_VERSION
            or plan.get("capture_fingerprint") != _capture_fingerprint(capture)
        ):
            raise WechatDigestError("微信 capture pending binding 无效。")
        payload: dict[str, object] = {
            "schema_version": CAPTURE_PENDING_SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": created_at,
            "all_history_upper_bound": plan.get("all_history_upper_bound"),
            "semantic_batch_size": plan.get("semantic_batch_size"),
            "preliminary_plan_fingerprint": _plan_fingerprint(plan),
            "capture_fingerprint": _capture_fingerprint(capture),
            "snapshot_fingerprint": _sha256_bytes(
                (_canonical_json(_capture_snapshot_payload(capture)) + "\n").encode(
                    "utf-8"
                )
            ),
            "capture_ms": capture_ms,
        }
        path = self.runs_root / run_id / "capture-pending.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return self._publish_private_no_replace(path, payload)

    def pending_capture(
        self,
    ) -> tuple[WechatCapture, dict[str, object]] | None:
        pending: list[tuple[WechatCapture, dict[str, object]]] = []
        if not self.runs_root.exists():
            return None
        for run_dir in sorted(self.runs_root.glob("run_*")):
            pending_path = run_dir / "capture-pending.json"
            if not os.path.lexists(pending_path) or os.path.lexists(
                run_dir / "plan.json"
            ):
                continue
            loaded = self.pending_capture_for_run(run_dir.name)
            if loaded is not None:
                pending.append(loaded)
        if len(pending) > 1:
            raise WechatDigestError("存在多个未提交的微信 capture snapshot。")
        return pending[0] if pending else None

    def pending_capture_for_run(
        self, run_id: str
    ) -> tuple[WechatCapture, dict[str, object]] | None:
        run_dir = self.runs_root / run_id
        pending_path = run_dir / "capture-pending.json"
        if not os.path.lexists(pending_path):
            return None
        try:
            value = self._private_receipt(
                pending_path,
                schema_version=CAPTURE_PENDING_SCHEMA_VERSION,
                label="微信 capture pending",
            )
            expected_fields = {
                "schema_version",
                "run_id",
                "created_at",
                "all_history_upper_bound",
                "semantic_batch_size",
                "preliminary_plan_fingerprint",
                "capture_fingerprint",
                "snapshot_fingerprint",
                "capture_ms",
            }
            snapshot_path = run_dir / "capture" / "snapshot.json"
            if set(value) != expected_fields or not os.path.lexists(snapshot_path):
                raise WechatDigestError(
                    "微信 capture pending 尚无可恢复 snapshot。"
                )
            snapshot = self._private_receipt(
                snapshot_path,
                schema_version=CAPTURE_SNAPSHOT_SCHEMA_VERSION,
                label="微信 capture snapshot",
            )
            capture = _capture_from_snapshot(snapshot)
            all_history = value.get("all_history_upper_bound")
            all_history_upper = (
                None
                if all_history is None
                else WechatCursor.from_dict(
                    all_history, "capture.pending.all_history_upper_bound"
                )
            )
            batch_size = value.get("semantic_batch_size")
            if (
                isinstance(batch_size, bool)
                or not isinstance(batch_size, int)
                or batch_size < 1
            ):
                raise WechatDigestError("微信 capture pending batch size 损坏。")
            pending_created_at = str(value["created_at"])
            preliminary, _ = _build_plan(
                capture,
                clock=lambda created_at=pending_created_at: created_at,
                run_id=run_dir.name,
                created_at=pending_created_at,
                semantic_batch_size=batch_size,
                all_history_upper_bound=all_history_upper,
            )
            if (
                value.get("run_id") != run_dir.name
                or value.get("capture_fingerprint")
                != _capture_fingerprint(capture)
                or value.get("snapshot_fingerprint")
                != _sha256_bytes(snapshot_path.read_bytes())
                or value.get("preliminary_plan_fingerprint")
                != _plan_fingerprint(preliminary)
                or isinstance(value.get("capture_ms"), bool)
                or not isinstance(value.get("capture_ms"), int)
                or int(value["capture_ms"]) < 0
            ):
                raise WechatDigestError("微信 capture pending 漂移。")
            return capture, value
        except (TypeError, ValueError) as exc:
            raise WechatDigestError("微信 capture pending 损坏。") from exc

    def publish_capture_artifacts(
        self,
        run_id: str,
        capture: WechatCapture,
        *,
        plan_binding_fingerprint: str,
        capture_ms: int,
    ) -> dict[str, object]:
        if (
            re.fullmatch(r"run_[0-9a-f]{32}", run_id) is None
            or not _sha256_value(plan_binding_fingerprint)
            or isinstance(capture_ms, bool)
            or not isinstance(capture_ms, int)
            or capture_ms < 0
        ):
            raise WechatDigestError("微信 capture artifact binding 无效。")
        directory = self.runs_root / run_id / "capture"
        if os.path.lexists(directory):
            metadata = directory.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.getuid()
            ):
                raise WechatDigestError("微信 capture artifact 目录不是私有目录。")
        else:
            directory.mkdir(parents=True, mode=0o700)
            os.chmod(directory, 0o700)
            directory_fd = os.open(directory.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        snapshot = _capture_snapshot_payload(capture)
        index = _capture_index_payload(capture)
        summary = _capture_summary_payload(capture, capture_ms=capture_ms)
        paths = {
            "snapshot": directory / "snapshot.json",
            "index": directory / "index.json",
            "summary": directory / "summary.json",
        }
        self._publish_private_no_replace(paths["snapshot"], snapshot)
        if self.after_capture_snapshot_write is not None:
            self.after_capture_snapshot_write()
        self._publish_private_no_replace(paths["index"], index)
        if self.after_capture_index_write is not None:
            self.after_capture_index_write()
        self._publish_private_no_replace(paths["summary"], summary)
        if self.after_capture_summary_write is not None:
            self.after_capture_summary_write()
        without_fingerprint: dict[str, object] = {
            "schema_version": CAPTURE_RECEIPT_SCHEMA_VERSION,
            "run_id": run_id,
            "provider_version": capture.provider_version,
            "after_cursor": capture.after_cursor.to_dict(),
            "upper_bound": capture.upper_bound.to_dict(),
            "capture_fingerprint": _capture_fingerprint(capture),
            "snapshot_raw_fingerprint": _sha256_bytes(paths["snapshot"].read_bytes()),
            "index_raw_fingerprint": _sha256_bytes(paths["index"].read_bytes()),
            "summary_raw_fingerprint": _sha256_bytes(paths["summary"].read_bytes()),
            "plan_binding_fingerprint": plan_binding_fingerprint,
            "message_count": len(capture.messages),
            "conversation_count": len({item.conversation_key for item in capture.messages}),
            "attachment_count": sum(len(item.attachments) for item in capture.messages),
        }
        receipt = {
            **without_fingerprint,
            "receipt_fingerprint": _sha256_bytes(
                _canonical_json(without_fingerprint).encode("utf-8")
            ),
        }
        self._publish_private_no_replace(directory / "receipt.json", receipt)
        if self.after_capture_receipt_write is not None:
            self.after_capture_receipt_write()
        observed, _ = self.load_capture_artifacts(
            run_id,
            expected_plan_binding=plan_binding_fingerprint,
        )
        if observed != capture:
            raise WechatDigestError("微信 capture snapshot 读回不一致。")
        return receipt

    def load_capture_artifacts(
        self,
        run_id: str,
        *,
        plan: Mapping[str, object] | None = None,
        expected_plan_binding: str | None = None,
    ) -> tuple[WechatCapture, dict[str, object]]:
        directory = self.runs_root / run_id / "capture"
        try:
            metadata = directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise WechatDigestError("微信 capture artifact 不可读。") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.getuid()
        ):
            raise WechatDigestError("微信 capture artifact 目录不是私有目录。")
        receipt = self.capture_receipt(run_id)
        if receipt is None:
            raise WechatDigestError("微信 capture artifact 尚未提交。")
        expected_receipt_fields = {
            "schema_version",
            "run_id",
            "provider_version",
            "after_cursor",
            "upper_bound",
            "capture_fingerprint",
            "snapshot_raw_fingerprint",
            "index_raw_fingerprint",
            "summary_raw_fingerprint",
            "plan_binding_fingerprint",
            "message_count",
            "conversation_count",
            "attachment_count",
            "receipt_fingerprint",
        }
        if set(receipt) != expected_receipt_fields or receipt.get("run_id") != run_id:
            raise WechatDigestError("微信 capture receipt 损坏。")
        without_fingerprint = dict(receipt)
        observed_receipt_fingerprint = without_fingerprint.pop("receipt_fingerprint", None)
        if observed_receipt_fingerprint != _sha256_bytes(
            _canonical_json(without_fingerprint).encode("utf-8")
        ):
            raise WechatDigestError("微信 capture receipt fingerprint 损坏。")
        if expected_plan_binding is not None and receipt.get(
            "plan_binding_fingerprint"
        ) != expected_plan_binding:
            raise WechatDigestError("微信 capture receipt plan binding 漂移。")
        if plan is not None:
            if (
                plan.get("run_id") != run_id
                or plan.get("schema_version") != SNAPSHOT_RUN_PLAN_SCHEMA_VERSION
                or plan.get("capture_receipt_fingerprint")
                != observed_receipt_fingerprint
                or receipt.get("plan_binding_fingerprint")
                != _plan_fingerprint(_capture_plan_projection(plan))
            ):
                raise WechatDigestError("微信 plan 与 capture receipt 不一致。")
        files = {
            "snapshot": directory / "snapshot.json",
            "index": directory / "index.json",
            "summary": directory / "summary.json",
        }
        raw: dict[str, bytes] = {}
        for label, path in files.items():
            value = self._private_receipt(
                path,
                schema_version={
                    "snapshot": CAPTURE_SNAPSHOT_SCHEMA_VERSION,
                    "index": CAPTURE_INDEX_SCHEMA_VERSION,
                    "summary": CAPTURE_SUMMARY_SCHEMA_VERSION,
                }[label],
                label=f"微信 capture {label}",
            )
            del value
            raw[label] = path.read_bytes()
            if _sha256_bytes(raw[label]) != receipt.get(f"{label}_raw_fingerprint"):
                raise WechatDigestError(f"微信 capture {label} fingerprint 漂移。")
        try:
            snapshot_value = json.loads(raw["snapshot"].decode("utf-8"))
            index_value = json.loads(raw["index"].decode("utf-8"))
            summary_value = json.loads(raw["summary"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatDigestError("微信 capture artifact 损坏。") from exc
        capture_ms_value = (
            summary_value.get("capture_ms")
            if isinstance(summary_value, dict)
            else None
        )
        if (
            isinstance(capture_ms_value, bool)
            or not isinstance(capture_ms_value, int)
            or capture_ms_value < 0
        ):
            raise WechatDigestError("微信 capture summary 性能统计损坏。")
        capture = _capture_from_snapshot(snapshot_value)
        expected_index = _capture_index_payload(capture)
        expected_summary = _capture_summary_payload(
            capture,
            capture_ms=capture_ms_value,
        )
        if index_value != expected_index or summary_value != expected_summary:
            raise WechatDigestError("微信 capture index/summary 与 snapshot 不一致。")
        if (
            receipt.get("provider_version") != capture.provider_version
            or receipt.get("after_cursor") != capture.after_cursor.to_dict()
            or receipt.get("upper_bound") != capture.upper_bound.to_dict()
            or receipt.get("capture_fingerprint") != _capture_fingerprint(capture)
            or receipt.get("message_count") != len(capture.messages)
            or receipt.get("conversation_count")
            != len({item.conversation_key for item in capture.messages})
            or receipt.get("attachment_count")
            != sum(len(item.attachments) for item in capture.messages)
        ):
            raise WechatDigestError("微信 capture receipt 与 snapshot 不一致。")
        return capture, receipt

    def load_capture_summary_receipt(
        self,
        run_id: str,
        *,
        plan: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Validate completed-window metadata without reading snapshot/index."""

        receipt = self.capture_receipt(run_id)
        if receipt is None:
            raise WechatDigestError("微信 capture receipt 缺失。")
        projected = dict(receipt)
        receipt_fingerprint = projected.pop("receipt_fingerprint", None)
        if receipt_fingerprint != _sha256_bytes(
            _canonical_json(projected).encode("utf-8")
        ):
            raise WechatDigestError("微信 capture receipt fingerprint 损坏。")
        if plan is not None and (
            plan.get("run_id") != run_id
            or plan.get("schema_version") != SNAPSHOT_RUN_PLAN_SCHEMA_VERSION
            or plan.get("capture_receipt_fingerprint") != receipt_fingerprint
            or receipt.get("plan_binding_fingerprint")
            != _plan_fingerprint(_capture_plan_projection(plan))
        ):
            raise WechatDigestError("微信 completed plan/capture binding 损坏。")
        path = self.runs_root / run_id / "capture" / "summary.json"
        raw = path.read_bytes()
        summary = self._private_receipt(
            path,
            schema_version=CAPTURE_SUMMARY_SCHEMA_VERSION,
            label="微信 capture summary",
        )
        if (
            _sha256_bytes(raw) != receipt.get("summary_raw_fingerprint")
            or summary.get("provider_version")
            != receipt.get("provider_version")
            or summary.get("after_cursor") != receipt.get("after_cursor")
            or summary.get("upper_bound") != receipt.get("upper_bound")
            or summary.get("message_count") != receipt.get("message_count")
            or summary.get("conversation_count")
            != receipt.get("conversation_count")
            or summary.get("attachment_count")
            != receipt.get("attachment_count")
        ):
            raise WechatDigestError("微信 capture summary/receipt 漂移。")
        return summary, receipt

    def load_capture_index(
        self,
        run_id: str,
        *,
        capture: WechatCapture,
    ) -> dict[str, object]:
        receipt = self.capture_receipt(run_id)
        if receipt is None:
            raise WechatDigestError("微信 capture receipt 缺失。")
        path = self.runs_root / run_id / "capture" / "index.json"
        raw = path.read_bytes()
        index = self._private_receipt(
            path,
            schema_version=CAPTURE_INDEX_SCHEMA_VERSION,
            label="微信 capture index",
        )
        if _sha256_bytes(raw) != receipt.get("index_raw_fingerprint"):
            raise WechatDigestError("微信 capture index fingerprint 漂移。")
        conversations = _plan_sequence(
            index.get("conversations"), "capture.index.conversations"
        )
        attachments = _plan_sequence(
            index.get("attachments"), "capture.index.attachments"
        )
        ordered_indexes: list[int] = []
        conversation_keys: set[str] = set()
        for conversation in conversations:
            key = conversation.get("conversation_key")
            message_indexes = conversation.get("message_indexes")
            if (
                not isinstance(key, str)
                or key in conversation_keys
                or not isinstance(message_indexes, list)
                or any(
                    isinstance(index, bool) or not isinstance(index, int)
                    for index in message_indexes
                )
            ):
                raise WechatDigestError("微信 capture index 会话范围损坏。")
            conversation_keys.add(key)
            ordered_indexes.extend(message_indexes)
        if (
            len(ordered_indexes) != len(capture.messages)
            or sorted(ordered_indexes) != list(range(len(capture.messages)))
            or any(
                not isinstance(entry.get("message_index"), int)
                or isinstance(entry.get("message_index"), bool)
                or not 0 <= int(entry["message_index"]) < len(capture.messages)
                for entry in attachments
            )
        ):
            raise WechatDigestError("微信 capture index 范围损坏。")
        return index

    def create(self, plan: dict[str, object], status: dict[str, object]) -> None:
        run_id = str(plan["run_id"])
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.json"
        status_path = run_dir / "status.json"
        receipt_path = run_dir / "run-plan-receipt.json"
        if plan.get("schema_version") == SNAPSHOT_RUN_PLAN_SCHEMA_VERSION:
            self.load_capture_artifacts(run_id, plan=plan)
        if receipt_path.exists() and not status_path.exists():
            raise WechatDigestError("微信运行创建记录顺序损坏。")
        if plan_path.exists():
            if self.plan(run_id) != plan:
                raise WechatDigestError("微信运行 identity collision。")
        else:
            if status_path.exists() or receipt_path.exists():
                raise WechatDigestError("微信运行创建记录损坏。")
            _atomic_write_json(plan_path, plan)
        if self.before_create_status_write is not None:
            self.before_create_status_write()
        if status_path.exists():
            if self.status(run_id) != status:
                raise WechatDigestError("微信运行创建状态不一致。")
        else:
            _atomic_write_json(status_path, status)
        if self.before_create_receipt_write is not None:
            self.before_create_receipt_write()
        committed_receipt = _committed_plan_receipt(run_id, plan)
        if receipt_path.exists():
            if self.plan_receipt(run_id) != committed_receipt:
                raise WechatDigestError("微信运行创建 receipt 不一致。")
        else:
            _atomic_write_json(receipt_path, committed_receipt)
        if (
            self.plan(run_id) != plan
            or self.status(run_id) != status
            or self.plan_receipt(run_id) != committed_receipt
        ):
            raise WechatDigestError("微信运行创建读回不一致。")
        if self.before_create_active_write is not None:
            self.before_create_active_write()
        _atomic_write_json(self.active_path, {"active_run_id": run_id})

    def update_status(self, run_id: str, status: dict[str, object]) -> None:
        _atomic_write_json(self.runs_root / run_id / "status.json", status)
        if self.status(run_id) != status:
            raise WechatDigestError("微信运行状态写入读回失败。")

    def governance_startup_recovery(
        self, run_id: str
    ) -> dict[str, object] | None:
        path = self.runs_root / run_id / "governance-startup-recovery.json"
        if not os.path.lexists(path):
            return None
        path_stat = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_IMODE(path_stat.st_mode) != 0o600
        ):
            raise WechatDigestError("Governance 启动恢复 receipt 不是私有文件。")
        value = self._read_json(path)
        if (
            not isinstance(value, dict)
            or value.get("schema_version")
            != GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION
        ):
            raise WechatDigestError("Governance 启动恢复 receipt 损坏。")
        return value

    def governance_startup_retry(
        self, run_id: str
    ) -> dict[str, object] | None:
        path = self.runs_root / run_id / "governance-startup-retry.json"
        if not os.path.lexists(path):
            return None
        path_stat = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_IMODE(path_stat.st_mode) != 0o600
        ):
            raise WechatDigestError(
                "Governance 启动恢复 retry receipt 不是私有文件。"
            )
        value = self._read_json(path)
        if (
            not isinstance(value, dict)
            or value.get("schema_version")
            != GOVERNANCE_STARTUP_RETRY_SCHEMA_VERSION
        ):
            raise WechatDigestError("Governance 启动恢复 retry receipt 损坏。")
        return value

    @staticmethod
    def _private_receipt(path: Path, *, schema_version: str, label: str) -> dict[str, object]:
        try:
            path_stat = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise WechatDigestError(f"{label} 不可读。") from exc
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_IMODE(path_stat.st_mode) != 0o600
            or path_stat.st_uid != os.getuid()
            or path_stat.st_nlink != 1
        ):
            raise WechatDigestError(f"{label} 不是私有文件。")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatDigestError(f"{label} 损坏。") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != schema_version
        ):
            raise WechatDigestError(f"{label} 损坏。")
        return value

    def _item_scoped_receipts(
        self,
        run_id: str,
        *,
        legacy_filename: str,
        directory_name: str,
        schema_version: str,
        label: str,
        scoped_filename_fingerprint_field: str,
    ) -> tuple[dict[str, object], ...]:
        run_dir = self.runs_root / run_id
        paths: list[Path] = []
        legacy = run_dir / legacy_filename
        if os.path.lexists(legacy):
            paths.append(legacy)
        directory = run_dir / directory_name
        if os.path.lexists(directory):
            try:
                directory_stat = directory.stat(follow_symlinks=False)
            except OSError as exc:
                raise WechatDigestError(f"{label}目录不可读。") from exc
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or stat.S_IMODE(directory_stat.st_mode) != 0o700
                or directory_stat.st_uid != os.getuid()
            ):
                raise WechatDigestError(f"{label}目录不是私有目录。")
            try:
                children = sorted(directory.iterdir())
            except OSError as exc:
                raise WechatDigestError(f"{label}目录不可读。") from exc
            for child in children:
                if re.fullmatch(r"[0-9a-f]{64}\.json", child.name) is None:
                    raise WechatDigestError(f"{label}目录 inventory 损坏。")
                paths.append(child)
        values: list[dict[str, object]] = []
        for path in paths:
            value = self._private_receipt(
                path,
                schema_version=schema_version,
                label=label,
            )
            if path.parent.name == directory_name:
                fingerprint = value.get(scoped_filename_fingerprint_field)
                if (
                    not _sha256_value(fingerprint)
                    or path.name
                    != f"{str(fingerprint).removeprefix('sha256:')}.json"
                ):
                    raise WechatDigestError(f"{label} filename binding 损坏。")
            values.append(value)
        return tuple(values)

    def governance_startup_recoveries(
        self, run_id: str
    ) -> tuple[dict[str, object], ...]:
        """Return every legacy and item-scoped startup recovery receipt."""

        values = self._item_scoped_receipts(
            run_id,
            legacy_filename="governance-startup-recovery.json",
            directory_name=GOVERNANCE_STARTUP_RECOVERY_DIRECTORY,
            schema_version=GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION,
            label="Governance 启动恢复 receipt",
            scoped_filename_fingerprint_field="receipt_fingerprint",
        )
        return tuple(_validated_governance_startup_recovery_receipt(value) for value in values)

    def governance_startup_retries(
        self, run_id: str
    ) -> tuple[dict[str, object], ...]:
        """Return every legacy and item-scoped startup retry consumption."""

        values = self._item_scoped_receipts(
            run_id,
            legacy_filename="governance-startup-retry.json",
            directory_name=GOVERNANCE_STARTUP_RETRY_DIRECTORY,
            schema_version=GOVERNANCE_STARTUP_RETRY_SCHEMA_VERSION,
            label="Governance 启动恢复 retry receipt",
            scoped_filename_fingerprint_field=(
                "recovery_receipt_fingerprint"
            ),
        )
        return tuple(_validated_governance_startup_retry_receipt(value) for value in values)

    def publish_item_scoped_governance_startup_receipt(
        self,
        run_id: str,
        *,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        """Publish one immutable receipt under its deterministic fingerprint."""

        expected = json.loads(_canonical_json(receipt))
        artifact_kind = expected.get("artifact_kind")
        if artifact_kind == "governance_startup_recovery":
            validated = _validated_governance_startup_recovery_receipt(expected)
            directory_name = GOVERNANCE_STARTUP_RECOVERY_DIRECTORY
            path_fingerprint = validated["receipt_fingerprint"]
        elif artifact_kind == "governance_startup_retry_consumption":
            validated = _validated_governance_startup_retry_receipt(expected)
            directory_name = GOVERNANCE_STARTUP_RETRY_DIRECTORY
            path_fingerprint = validated["recovery_receipt_fingerprint"]
        else:
            raise WechatDigestError("Governance 启动恢复 receipt 类型无效。")
        assert isinstance(path_fingerprint, str)
        directory = self.runs_root / run_id / directory_name
        if os.path.lexists(directory):
            directory_stat = directory.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or stat.S_IMODE(directory_stat.st_mode) != 0o700
                or directory_stat.st_uid != os.getuid()
            ):
                raise WechatDigestError("Governance 启动恢复 receipt 目录损坏。")
        else:
            directory.mkdir(mode=0o700)
            directory_fd = os.open(directory.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        path = directory / f"{path_fingerprint.removeprefix('sha256:')}.json"
        observed = self._publish_private_no_replace(path, expected)
        if observed != validated:
            raise WechatDigestError("Governance 启动恢复 receipt 读回失败。")
        return observed

    def _publish_private_no_replace(
        self, path: Path, expected: Mapping[str, object]
    ) -> dict[str, object]:
        if os.path.lexists(path):
            path_stat = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or stat.S_IMODE(path_stat.st_mode) != 0o600
                or path_stat.st_uid != os.getuid()
                or path_stat.st_nlink != 1
            ):
                raise WechatDigestError(
                    "Governance 启动恢复 receipt 已存在但不是私有文件。"
                )
            observed = self._read_json(path)
            if observed != expected:
                raise WechatDigestError(
                    "Governance 启动恢复 receipt 已存在且不一致。"
                )
            return observed
        encoded = (_canonical_json(expected) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".governance-startup-", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError:
                if self._read_json(path) != expected:
                    raise WechatDigestError(
                        "Governance 启动恢复 receipt 已存在且不一致。"
                    )
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        observed = self._read_json(path)
        if observed != expected or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise WechatDigestError("Governance 启动恢复 receipt 读回失败。")
        return observed

    def failed_closed_recovery(
        self, run_id: str
    ) -> dict[str, object] | None:
        path = self.runs_root / run_id / "failed-closed-recovery.json"
        if not os.path.lexists(path):
            return None
        path_stat = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_IMODE(path_stat.st_mode) != 0o600
        ):
            raise WechatDigestError("历史失败恢复 receipt 不是私有文件。")
        value = self._read_json(path)
        if (
            not isinstance(value, dict)
            or value.get("schema_version")
            != FAILED_CLOSED_RECOVERY_SCHEMA_VERSION
        ):
            raise WechatDigestError("历史失败恢复 receipt 损坏。")
        return value

    def publish_governance_startup_receipt(
        self,
        run_id: str,
        *,
        filename: str,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        if filename not in {
            "governance-startup-recovery.json",
            "governance-startup-retry.json",
            "failed-closed-recovery.json",
        }:
            raise WechatDigestError("Governance 启动恢复 receipt 名称无效。")
        path = self.runs_root / run_id / filename
        expected = json.loads(_canonical_json(receipt))
        if path.exists():
            observed = self._read_json(path)
            if observed != expected:
                raise WechatDigestError(
                    "Governance 启动恢复 receipt 已存在且不一致。"
                )
            return observed
        encoded = (_canonical_json(expected) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".governance-startup-", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError:
                if self._read_json(path) != expected:
                    raise WechatDigestError(
                        "Governance 启动恢复 receipt 已存在且不一致。"
                    )
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        observed = self._read_json(path)
        if observed != expected or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise WechatDigestError("Governance 启动恢复 receipt 读回失败。")
        return observed

    def publish_segment_receipt(
        self,
        run_id: str,
        *,
        status: Mapping[str, object],
        completed_items: int,
        remaining_items: int,
        stop_reason: str,
    ) -> dict[str, object]:
        if (
            isinstance(completed_items, bool)
            or not isinstance(completed_items, int)
            or completed_items < 1
            or isinstance(remaining_items, bool)
            or not isinstance(remaining_items, int)
            or remaining_items < 0
            or stop_reason != "item_limit"
        ):
            raise WechatDigestError("微信短执行段摘要无效。")
        current = self.status(run_id)
        if current != status or current.get("state") not in {
            "processing",
            "completed",
        }:
            raise WechatDigestError("微信短执行段状态无法安全读回。")
        status_fingerprint = _sha256_bytes(
            _canonical_json(current).encode("utf-8")
        )
        without_fingerprint: dict[str, object] = {
            "schema_version": RUN_SEGMENT_RECEIPT_SCHEMA_VERSION,
            "run_id": run_id,
            "stop_reason": stop_reason,
            "completed_items": completed_items,
            "remaining_items": remaining_items,
            "checkpoint_published": bool(current.get("checkpoint_published")),
            "status_fingerprint": status_fingerprint,
            "stopped_at": current.get("updated_at"),
        }
        receipt = {
            **without_fingerprint,
            "receipt_fingerprint": _sha256_bytes(
                _canonical_json(without_fingerprint).encode("utf-8")
            ),
        }
        segment_root = self.runs_root / run_id / "segments"
        segment_root.mkdir(parents=True, exist_ok=True)
        path = segment_root / f"segment-{status_fingerprint.removeprefix('sha256:')}.json"
        if self.before_segment_receipt_write is not None:
            self.before_segment_receipt_write()
        if path.exists():
            observed = self._read_json(path)
            if observed != receipt:
                raise WechatDigestError("微信短执行段 receipt 已存在且不一致。")
        else:
            encoded = (_canonical_json(receipt) + "\n").encode("utf-8")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".segment-", dir=segment_root
            )
            temporary_path = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary_path, path, follow_symlinks=False)
                except FileExistsError:
                    if self._read_json(path) != receipt:
                        raise WechatDigestError(
                            "微信短执行段 receipt 已存在且不一致。"
                        )
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
            directory_fd = os.open(segment_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if self._read_json(path) != receipt:
            raise WechatDigestError("微信短执行段 receipt 读回失败。")
        return receipt

    def complete_upgrade(
        self, run_id: str, plan: dict[str, object], status: dict[str, object]
    ) -> None:
        """Converge an already-validated v1→v2 upgrade; receipt commits last."""
        if self.has_plan_receipt(run_id):
            raise WechatDigestError("active 微信运行已经完成升级。")
        plan_path = self.runs_root / run_id / "plan.json"
        status_path = self.runs_root / run_id / "status.json"
        if self.plan(run_id) != plan:
            _atomic_write_json(plan_path, plan)
        if self.before_upgrade_status_write is not None:
            self.before_upgrade_status_write()
        if self.status(run_id) != status:
            _atomic_write_json(status_path, status)
        _atomic_write_json(
            self.runs_root / run_id / "run-plan-receipt.json",
            _committed_plan_receipt(run_id, plan),
        )
        if self.after_upgrade_receipt_write is not None:
            self.after_upgrade_receipt_write()

    def complete_all_history_upper_upgrade(
        self,
        run_id: str,
        plan: dict[str, object],
        status: dict[str, object],
        pending_receipt: dict[str, object],
    ) -> None:
        """Converge a verified v2→v3 scope upgrade; receipt commits last."""
        plan_path = self.runs_root / run_id / "plan.json"
        status_path = self.runs_root / run_id / "status.json"
        receipt_path = self.runs_root / run_id / "run-plan-receipt.json"
        current_receipt = self.plan_receipt(run_id)
        committed_receipt = _committed_plan_receipt(run_id, plan)
        if current_receipt == committed_receipt:
            return
        if current_receipt != pending_receipt:
            previous_fingerprint = pending_receipt.get(
                "previous_plan_fingerprint"
            )
            if (
                _committed_receipt_fingerprint(current_receipt)
                != previous_fingerprint
                or _plan_fingerprint(self.plan(run_id))
                != previous_fingerprint
                or self.status(run_id).get("plan_fingerprint")
                != previous_fingerprint
            ):
                raise WechatDigestError(
                    "微信全历史边界升级初始 receipt 不一致。"
                )
            _atomic_write_json(receipt_path, pending_receipt)
            if self.plan_receipt(run_id) != pending_receipt:
                raise WechatDigestError(
                    "微信全历史边界 pending receipt 读回不一致。"
                )
        if self.after_upgrade_pending_receipt_write is not None:
            self.after_upgrade_pending_receipt_write()
        if self.plan(run_id) != plan:
            _atomic_write_json(plan_path, plan)
        if self.before_upgrade_status_write is not None:
            self.before_upgrade_status_write()
        if self.status(run_id) != status:
            _atomic_write_json(status_path, status)
        if self.before_upgrade_commit_receipt_write is not None:
            self.before_upgrade_commit_receipt_write()
        _atomic_write_json(receipt_path, committed_receipt)
        if self.after_upgrade_receipt_write is not None:
            self.after_upgrade_receipt_write()

    def complete_capture_upgrade(
        self,
        run_id: str,
        plan: dict[str, object],
        status: dict[str, object],
    ) -> None:
        """Converge a validated v3→v4 capture binding; receipt commits last."""

        if plan.get("schema_version") != SNAPSHOT_RUN_PLAN_SCHEMA_VERSION:
            raise WechatDigestError("微信 capture upgrade target plan 无效。")
        self.load_capture_artifacts(run_id, plan=plan)
        plan_path = self.runs_root / run_id / "plan.json"
        status_path = self.runs_root / run_id / "status.json"
        receipt_path = self.runs_root / run_id / "run-plan-receipt.json"
        target_fingerprint = _plan_fingerprint(plan)
        previous_plan = _capture_plan_projection(plan)
        previous_fingerprint = _plan_fingerprint(previous_plan)
        current_plan = self.plan(run_id)
        current_status = self.status(run_id)
        current_receipt = self.plan_receipt(run_id)
        current_receipt_fingerprint = _committed_receipt_fingerprint(
            current_receipt
        )
        if current_plan not in (previous_plan, plan):
            raise WechatDigestError("微信 capture upgrade plan 漂移。")
        if current_status.get("plan_fingerprint") not in {
            previous_fingerprint,
            target_fingerprint,
        }:
            raise WechatDigestError("微信 capture upgrade status 漂移。")
        if current_receipt_fingerprint not in {
            previous_fingerprint,
            target_fingerprint,
        }:
            raise WechatDigestError("微信 capture upgrade receipt 漂移。")
        if current_receipt_fingerprint == target_fingerprint and (
            current_plan != plan or current_status != status
        ):
            raise WechatDigestError("微信 capture upgrade commit 顺序损坏。")
        if current_plan != plan:
            _atomic_write_json(plan_path, plan)
        if self.before_upgrade_status_write is not None:
            self.before_upgrade_status_write()
        if self.status(run_id) != status:
            _atomic_write_json(status_path, status)
        if self.before_upgrade_commit_receipt_write is not None:
            self.before_upgrade_commit_receipt_write()
        committed = _committed_plan_receipt(run_id, plan)
        if self.plan_receipt(run_id) != committed:
            _atomic_write_json(receipt_path, committed)
        if self.after_upgrade_receipt_write is not None:
            self.after_upgrade_receipt_write()
        if (
            self.plan(run_id) != plan
            or self.status(run_id) != status
            or self.plan_receipt(run_id) != committed
        ):
            raise WechatDigestError("微信 capture upgrade 读回失败。")

    def publish_checkpoint(self, run_id: str, cursor: WechatCursor) -> None:
        if self.before_checkpoint_publish is not None:
            self.before_checkpoint_publish()
        _atomic_write_json(
            self.checkpoint_path,
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "cursor": cursor.to_dict(),
                "published_at": self.clock(),
                "run_id": run_id,
            },
        )
        readback = self.checkpoint()
        if readback != cursor:
            raise WechatDigestError("微信 checkpoint 写入读回失败。")

    def clear_active(self) -> None:
        _atomic_write_json(self.active_path, {"active_run_id": None})
        if self.active_run_id() is not None:
            raise WechatDigestError("微信 active run 清理读回失败。")

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WechatDigestError("微信 durable run 记录不可读。") from exc


def parse_since(value: str) -> WechatCursor:
    raw = value.strip()
    if not raw:
        raise ValueError("--since must not be empty")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("--since must be an ISO date or timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    timestamp = int(parsed.timestamp())
    if timestamp < 0:
        raise ValueError("--since must not precede the Unix epoch")
    return WechatCursor(timestamp, "", "")


def detect_codex_provider_version(codex_binary: str = "codex") -> str:
    try:
        return resolve_codex_executable_identity(codex_binary).provider_version
    except RepresentationInformationError as exc:
        raise WechatDigestError("无法验证 Semantic Provider 版本。") from exc


def detect_clean_git_head(repository_root: Path | None = None) -> str:
    """Read the reviewed build head and reject a dirty implementation tree."""

    root = repository_root or Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WechatDigestError("无法验证当前 ArcheOS build。") from exc
    value = head.stdout.strip()
    if (
        head.returncode != 0
        or status.returncode != 0
        or status.stdout
        or re.fullmatch(r"[0-9a-f]{40}", value) is None
    ):
        raise WechatDigestError("当前 ArcheOS build 未达到 reviewed clean head。")
    return value


class ExistingSemanticHandoff:
    def __init__(
        self,
        *,
        source_repository: LocalManagedSourceRepository,
        representation_repository: LocalRepresentationRepository,
        information_root: Path,
        information_store: JsonlAtomicInformationStore,
        audit_root: Path,
        codex_binary: str,
        provider_version: str,
        timeout_seconds: float,
        model: str = DEFAULT_SEMANTIC_MODEL,
        reasoning_effort: str = DEFAULT_SEMANTIC_REASONING_EFFORT,
        batch_size: int = DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
        reviewed_git_head: str | None = None,
    ) -> None:
        self.service = ExternalAgentSemanticHandoffService(
            RepresentationInformationService(
                source_repository,
                representation_repository,
                information_root,
                batch_size=batch_size,
            ),
            information_store,
            audit_root,
        )
        self._provider_config = {
            "codex_binary": codex_binary,
            "provider_version": provider_version,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "timeout_seconds": timeout_seconds,
        }
        self._diagnostic_root = Path(audit_root) / "semantic-provider-diagnostics"
        self.provider = self._new_provider("serial")
        self.reviewed_git_head = reviewed_git_head or detect_clean_git_head()

    def _new_provider(self, lane: str) -> CodexCliRepresentationAnalysisProvider:
        lane_fingerprint = hashlib.sha256(lane.encode()).hexdigest()[:16]
        return CodexCliRepresentationAnalysisProvider(
            **self._provider_config,
            diagnostic_root=self._diagnostic_root / lane_fingerprint,
        )

    def execute(
        self,
        representation_id: str,
        *,
        privacy_binding: SemanticPrivacyBinding,
        authority_binding: SemanticWindowAuthorityBinding | None,
    ):
        return self.service.execute(
            representation_id,
            self.provider,
            privacy_binding=privacy_binding,
            authority_binding=authority_binding,
        )

    def prepare_results(
        self,
        requests: Sequence[SemanticResultOnlyRequest],
        *,
        parallelism: int,
    ) -> dict[str, int]:
        providers = tuple(
            self._new_provider(request.representation_id) for request in requests
        )
        elapsed = self.service.prepare_results(
            requests,
            providers,
            concurrency=parallelism,
        )
        self.last_prepare_metrics = dict(
            getattr(self.service, "last_result_only_metrics", {})
        )
        return elapsed

    def inspect_recovery_wave(
        self,
        requests: Sequence[SemanticResultOnlyRequest],
    ) -> tuple[dict[str, object], ...]:
        providers = tuple(
            self._new_provider(request.representation_id) for request in requests
        )
        return self.service.inspect_recovery_wave(requests, providers)

    def validate_pre_attempt_inventory(
        self,
        representation_id: str,
        *,
        privacy_binding: SemanticPrivacyBinding,
    ) -> dict[str, object]:
        return self.service.validate_pre_attempt_inventory(
            representation_id,
            self.provider,
            privacy_binding,
        )

    def install_global_authority(
        self,
        *,
        inventory_authority_file: Path,
        window_binding: SemanticWindowAuthorityBinding,
    ) -> dict[str, object]:
        return self.service.install_global_authority(
            self.provider,
            inventory_authority_file=inventory_authority_file,
            window_binding=window_binding,
        )

    def install_global_authority_extension(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
    ) -> dict[str, object]:
        return self.service.install_global_authority_extension(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
        )

    def install_maintenance_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]:
        return self.service.install_maintenance_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
        )

    def install_reviewed_head_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        active_run_binding: Mapping[str, object],
    ) -> dict[str, object]:
        return self.service.install_reviewed_head_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
            active_run_binding=active_run_binding,
        )

    def install_batch_governance_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]:
        return self.service.install_batch_governance_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
        )

    def install_gate_c_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]:
        return self.service.install_gate_c_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
        )

    def install_segmented_gate_c_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
    ) -> dict[str, object]:
        return self.service.install_segmented_gate_c_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
        )

    def install_governance_startup_recovery_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object]:
        return self.service.install_governance_startup_recovery_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
            authority_manifest_fingerprint=authority_manifest_fingerprint,
            authority_manifest_raw_fingerprint=(
                authority_manifest_raw_fingerprint
            ),
        )

    def governance_startup_recovery_continuation(
        self,
        *,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object] | None:
        return self.service.governance_startup_recovery_continuation(
            self.provider,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
            authority_manifest_fingerprint=authority_manifest_fingerprint,
            authority_manifest_raw_fingerprint=(
                authority_manifest_raw_fingerprint
            ),
        )

    def install_multi_governance_startup_recovery_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object]:
        return self.service.install_multi_governance_startup_recovery_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
            authority_manifest_fingerprint=authority_manifest_fingerprint,
            authority_manifest_raw_fingerprint=(
                authority_manifest_raw_fingerprint
            ),
        )

    def multi_governance_startup_recovery_continuation(
        self,
        *,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object] | None:
        return self.service.multi_governance_startup_recovery_continuation(
            self.provider,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
            authority_manifest_fingerprint=authority_manifest_fingerprint,
            authority_manifest_raw_fingerprint=(
                authority_manifest_raw_fingerprint
            ),
        )

    def install_failed_closed_recovery_continuation(
        self,
        *,
        window_binding: SemanticWindowAuthorityBinding,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object]:
        return self.service.install_failed_closed_recovery_continuation(
            self.provider,
            window_binding=window_binding,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
            authority_manifest_fingerprint=authority_manifest_fingerprint,
            authority_manifest_raw_fingerprint=(
                authority_manifest_raw_fingerprint
            ),
        )

    def failed_closed_recovery_continuation(
        self,
        *,
        authority_ref: str,
        authority_manifest_fingerprint: str,
        authority_manifest_raw_fingerprint: str,
    ) -> dict[str, object] | None:
        return self.service.failed_closed_recovery_continuation(
            self.provider,
            reviewed_git_head=self.reviewed_git_head,
            authority_ref=authority_ref,
            authority_manifest_fingerprint=authority_manifest_fingerprint,
            authority_manifest_raw_fingerprint=(
                authority_manifest_raw_fingerprint
            ),
        )

    def resolve_unknown(
        self,
        *,
        authority_manifest_file: Path,
        digest_binding: Mapping[str, object],
        commit_failed_closed_status: Callable[
            [str], tuple[str, Mapping[str, object]]
        ],
    ) -> dict[str, object]:
        return self.service.resolve_unknown(
            self.provider,
            authority_manifest_file=authority_manifest_file,
            reviewed_git_head=self.reviewed_git_head,
            digest_binding=digest_binding,
            commit_failed_closed_status=commit_failed_closed_status,
        )

    def validate_unknown_resolution_digest(
        self,
        *,
        digest_binding: Mapping[str, object],
        failed_closed_status_fingerprint: str,
        resolution_id: str,
    ) -> dict[str, object]:
        return self.service.validate_unknown_resolution_digest(
            digest_binding=digest_binding,
            failed_closed_status_fingerprint=failed_closed_status_fingerprint,
            resolution_id=resolution_id,
        )

    def resolve_timeout_212(
        self,
        *,
        authority_manifest_file: Path,
        digest_binding: Mapping[str, object],
        commit_failed_closed_status: Callable[
            [str], tuple[str, Mapping[str, object]]
        ],
    ) -> dict[str, object]:
        return self.service.resolve_timeout_212(
            self.provider,
            authority_manifest_file=authority_manifest_file,
            reviewed_git_head=self.reviewed_git_head,
            digest_binding=digest_binding,
            commit_failed_closed_status=commit_failed_closed_status,
        )

    def resolve_attempt(
        self,
        *,
        authority_manifest_file: Path,
        digest_binding: Mapping[str, object],
        commit_failed_closed_status: Callable[
            [str, int], tuple[str, Mapping[str, object]]
        ],
    ) -> dict[str, object]:
        return self.service.resolve_attempt(
            self.provider,
            authority_manifest_file=authority_manifest_file,
            reviewed_git_head=self.reviewed_git_head,
            digest_binding=digest_binding,
            commit_failed_closed_status=commit_failed_closed_status,
        )

    def build_attempt_resolution_manifest(
        self,
        *,
        candidate_file: Path,
        authority_ref: str,
        observed_at: str,
        digest_binding: Mapping[str, object],
    ) -> dict[str, object]:
        return self.service.build_attempt_resolution_manifest(
            self.provider,
            candidate_file=candidate_file,
            authority_ref=authority_ref,
            observed_at=observed_at,
            digest_binding=digest_binding,
        )

    def validate_attempt_resolution_digest(
        self,
        *,
        digest_binding: Mapping[str, object],
        failed_closed_status_fingerprint: str,
        resolution_id: str,
    ) -> dict[str, object]:
        return self.service.validate_attempt_resolution_digest(
            digest_binding=digest_binding,
            failed_closed_status_fingerprint=failed_closed_status_fingerprint,
            resolution_id=resolution_id,
        )

    def validate_timeout_212_resolution_digest(
        self,
        *,
        digest_binding: Mapping[str, object],
        failed_closed_status_fingerprint: str,
        resolution_id: str,
    ) -> dict[str, object]:
        return self.service.validate_timeout_212_resolution_digest(
            digest_binding=digest_binding,
            failed_closed_status_fingerprint=failed_closed_status_fingerprint,
            resolution_id=resolution_id,
        )

    def global_campaign_binding(
        self,
    ) -> SemanticCampaignAuthorityBinding | None:
        return self.service.global_campaign_binding()

    def global_attempt_summary(self, representation_id: str) -> dict[str, int]:
        return self.service.global_attempt_summary(representation_id)

    def governance_startup_recovery_snapshot(
        self, representation_id: str
    ) -> dict[str, object]:
        return self.service.governance_startup_recovery_snapshot(representation_id)


@dataclass(frozen=True)
class WechatDigestResult:
    run_id: str
    new_messages: int
    new_attachments: int
    durable_information: int
    local_only: int
    unsupported: int
    pending_human: int
    context_objects: int
    checkpoint_published: bool
    replayed: bool
    context_object_ids: tuple[str, ...] = ()
    governance_app_server_starts: int = 0
    governance_threads: int = 0
    governance_turns: int = 0
    governance_startup_wall_ms: int = 0
    governance_turn_wall_ms_sum: int = 0
    governance_turn_wall_ms_max: int = 0
    governance_wall_ms: int = 0
    governance_timeouts: int = 0
    governance_failures: int = 0
    failed_closed: int = 0
    semantic_preserved_but_unabsorbed: int = 0
    governance_preserved_but_incomplete: int = 0
    segment_safe_stopped: bool = False
    segment_items_completed: int = 0
    segment_remaining_items: int = 0
    segment_stop_reason: str | None = None
    segment_receipt_fingerprint: str | None = None
    upper_bound_probe_calls: int = 0
    capture_attempts: int = 0
    capture_successes: int = 0
    capture_reasons: tuple[str, ...] = ()
    capture_provider_calls: int = 0
    completed_window_connector_replays: int = 0
    materialized_cursor_rows: int = 0
    cursor_discovery_ms: int = 0
    snapshot_bytes: int = 0
    capture_ms: int = 0
    snapshot_publish_ms: int = 0
    snapshot_readback_ms: int = 0
    slice_build_ms: int = 0
    semantic_parallelism: int = 1
    semantic_peak_concurrency: int = 0
    semantic_wall_ms: int = 0
    semantic_serial_estimate_ms: int = 0
    commit_wall_ms: int = 0
    checkpoint_wall_ms: int = 0
    governance_peak_concurrency: int = 0
    resume_provider_calls: int = 0
    total_wall_ms: int = 0


def _empty_governance_metrics() -> dict[str, object]:
    return {
        **{field: 0 for field in GOVERNANCE_METRIC_COUNTERS},
        "failure_categories": {},
    }


def _validated_governance_metrics(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        *GOVERNANCE_METRIC_COUNTERS,
        "failure_categories",
    }:
        raise WechatDigestError("微信 Governance metrics 损坏。")
    for field in GOVERNANCE_METRIC_COUNTERS:
        candidate = value[field]
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate < 0
        ):
            raise WechatDigestError("微信 Governance metrics 损坏。")
    categories = value["failure_categories"]
    if not isinstance(categories, dict) or any(
        not isinstance(category, str)
        or not category
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        for category, count in categories.items()
    ):
        raise WechatDigestError("微信 Governance metrics 损坏。")
    if sum(categories.values()) != value["failure_count"]:
        raise WechatDigestError("微信 Governance metrics 损坏。")
    if value["timeout_count"] != categories.get("timeout", 0):
        raise WechatDigestError("微信 Governance metrics 损坏。")
    return dict(value)


def _merge_governance_metrics(
    existing: object, current: Mapping[str, object]
) -> dict[str, object]:
    before = (
        _empty_governance_metrics()
        if existing is None
        else _validated_governance_metrics(existing)
    )
    after = _validated_governance_metrics(dict(current))
    merged = {
        field: (
            max(int(before[field]), int(after[field]))
            if field == "turn_wall_ms_max"
            else int(before[field]) + int(after[field])
        )
        for field in GOVERNANCE_METRIC_COUNTERS
    }
    categories = dict(before["failure_categories"])
    for category, count in dict(after["failure_categories"]).items():
        categories[category] = int(categories.get(category, 0)) + int(count)
    merged["failure_categories"] = categories
    return merged


def _governance_atomic_fingerprint(atomic_ids: Sequence[str]) -> str:
    if (
        not atomic_ids
        or any(not isinstance(value, str) for value in atomic_ids)
        or len(atomic_ids) != len(set(atomic_ids))
    ):
        raise WechatDigestError("微信 Governance receipt binding 损坏。")
    return _sha256_bytes(
        _canonical_json(sorted(set(atomic_ids))).encode("utf-8")
    )


def _governance_batch_fingerprint(
    atomic_ids: Sequence[str], interpretations: Sequence[InterpretationResult]
) -> str:
    if (
        not atomic_ids
        or len(atomic_ids) != len(set(atomic_ids))
        or len(atomic_ids) != len(interpretations)
    ):
        raise WechatDigestError("微信 Governance batch binding 损坏。")
    payload = [
        {
            "atomic_information_id": atomic_id,
            "interpretation": interpretation_to_dict(interpretation),
        }
        for atomic_id, interpretation in zip(
            atomic_ids, interpretations, strict=True
        )
    ]
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def _validated_governance_receipt(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("微信 Governance receipt 损坏。")
    schema_version = value.get("schema_version")
    phase = value.get("phase")
    if schema_version == LEGACY_GOVERNANCE_RECEIPT_SCHEMA_VERSION:
        expected = (
            {
                "schema_version",
                "phase",
                "atomic_information_fingerprint",
            }
            if phase == "started"
            else {
                "schema_version",
                "phase",
                "atomic_information_fingerprint",
                "pending_human",
                "context_object_ids",
            }
        )
        fingerprint = value.get("atomic_information_fingerprint")
        if (
            phase not in {"started", "completed"}
            or set(value) != expected
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
        ):
            raise WechatDigestError("微信 Governance receipt 损坏。")
        if phase == "completed":
            object_ids = value.get("context_object_ids")
            if (
                not isinstance(value.get("pending_human"), bool)
                or not isinstance(object_ids, list)
                or any(not isinstance(object_id, str) for object_id in object_ids)
                or object_ids != sorted(set(object_ids))
            ):
                raise WechatDigestError("微信 Governance receipt 损坏。")
        return dict(value)

    expected = (
        {
            "schema_version",
            "phase",
            "atomic_information_fingerprint",
        }
        if phase == "started"
        else {
            "schema_version",
            "phase",
            "atomic_information_fingerprint",
            "batch_atomic_information_ids",
            "batch_fingerprint",
            "interpretations",
            "next_index",
            "baseline_effect_fingerprints",
            "cursor_effect_fingerprints",
            "cursor_effect_snapshots",
            "applied_effect_fingerprints",
            "in_flight_index",
            "pending_human",
            "context_object_ids",
        }
    )
    fingerprint = value.get("atomic_information_fingerprint")
    if (
        schema_version != GOVERNANCE_RECEIPT_SCHEMA_VERSION
        or phase not in {
            "started",
            "interpreted",
            "applying",
            "applied",
            "completed",
        }
        or set(value) != expected
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
    ):
        raise WechatDigestError("微信 Governance receipt 损坏。")
    if phase == "started":
        return dict(value)
    atomic_ids = value.get("batch_atomic_information_ids")
    raw_interpretations = value.get("interpretations")
    object_ids = value.get("context_object_ids")
    next_index = value.get("next_index")
    baseline_effect_fingerprints = value.get("baseline_effect_fingerprints")
    cursor_effect_fingerprints = value.get("cursor_effect_fingerprints")
    cursor_effect_snapshots = value.get("cursor_effect_snapshots")
    applied_effect_fingerprints = value.get("applied_effect_fingerprints")
    in_flight_index = value.get("in_flight_index")
    batch_fingerprint = value.get("batch_fingerprint")
    if (
        not isinstance(atomic_ids, list)
        or not atomic_ids
        or any(not isinstance(atomic_id, str) for atomic_id in atomic_ids)
        or len(atomic_ids) != len(set(atomic_ids))
        or not isinstance(raw_interpretations, list)
        or len(raw_interpretations) != len(atomic_ids)
        or isinstance(next_index, bool)
        or not isinstance(next_index, int)
        or not 0 <= next_index <= len(atomic_ids)
        or not isinstance(baseline_effect_fingerprints, list)
        or len(baseline_effect_fingerprints) != len(atomic_ids)
        or any(
            not _sha256_value(item) for item in baseline_effect_fingerprints
        )
        or not isinstance(cursor_effect_fingerprints, list)
        or len(cursor_effect_fingerprints) != len(atomic_ids)
        or any(
            not _sha256_value(item) for item in cursor_effect_fingerprints
        )
        or not isinstance(cursor_effect_snapshots, list)
        or len(cursor_effect_snapshots) != len(atomic_ids)
        or any(not isinstance(item, dict) for item in cursor_effect_snapshots)
        or [
            _sha256_bytes(_canonical_json(item).encode("utf-8"))
            for item in cursor_effect_snapshots
        ]
        != cursor_effect_fingerprints
        or not isinstance(applied_effect_fingerprints, list)
        or len(applied_effect_fingerprints) != next_index
        or any(
            not _sha256_value(item) for item in applied_effect_fingerprints
        )
        or (
            in_flight_index is not None
            and (
                isinstance(in_flight_index, bool)
                or not isinstance(in_flight_index, int)
                or in_flight_index != next_index
                or not 0 <= in_flight_index < len(atomic_ids)
            )
        )
        or not isinstance(value.get("pending_human"), bool)
        or not isinstance(object_ids, list)
        or any(not isinstance(object_id, str) for object_id in object_ids)
        or object_ids != sorted(set(object_ids))
        or not isinstance(batch_fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", batch_fingerprint) is None
    ):
        raise WechatDigestError("微信 Governance receipt 损坏。")
    try:
        interpretations = tuple(
            parse_interpretation(item) for item in raw_interpretations
        )
    except (TypeError, ValueError) as exc:
        raise WechatDigestError("微信 Governance receipt 损坏。") from exc
    if batch_fingerprint != _governance_batch_fingerprint(
        atomic_ids, interpretations
    ):
        raise WechatDigestError("微信 Governance receipt batch binding 不一致。")
    if phase == "interpreted" and (
        next_index != 0 or in_flight_index is not None
    ):
        raise WechatDigestError("微信 Governance receipt 进度损坏。")
    if (
        next_index == 0
        and cursor_effect_fingerprints != baseline_effect_fingerprints
    ):
        raise WechatDigestError("微信 Governance receipt effect cursor 损坏。")
    if applied_effect_fingerprints != cursor_effect_fingerprints[:next_index]:
        raise WechatDigestError("微信 Governance receipt effect cursor 损坏。")
    if phase == "applying" and not (
        0 <= next_index < len(atomic_ids)
        and (next_index > 0 or in_flight_index == 0)
    ):
        raise WechatDigestError("微信 Governance receipt 进度损坏。")
    if phase in {"applied", "completed"} and (
        next_index != len(atomic_ids) or in_flight_index is not None
    ):
        raise WechatDigestError("微信 Governance receipt 进度损坏。")
    return dict(value)


def _validated_governance_migration(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("微信 Governance migration receipt 损坏。")
    expected = {
        "schema_version",
        "phase",
        "authority_ref",
        "implementation_plan_ref",
        "authority_manifest_fingerprint",
        "authority_manifest_raw_fingerprint",
        "legacy_governance_receipt",
        "atomic_information_fingerprint",
        "ordered_atomic_information_ids",
        "completed_atomic_information_ids",
        "remaining_atomic_information_ids",
        "legacy_effect_fingerprint",
        "pristine_remaining_fingerprint",
        "activation_business_tree_fingerprint",
        "pending_human",
        "context_object_ids",
        "semantic_continuation_fingerprint",
        "migration_fingerprint",
    }
    projected = dict(value)
    fingerprint = projected.pop("migration_fingerprint", None)
    ordered = value.get("ordered_atomic_information_ids")
    completed = value.get("completed_atomic_information_ids")
    remaining = value.get("remaining_atomic_information_ids")
    object_ids = value.get("context_object_ids")
    legacy = value.get("legacy_governance_receipt")
    try:
        legacy_receipt = _validated_governance_receipt(legacy)
    except WechatDigestError as exc:
        raise WechatDigestError(
            "微信 Governance migration receipt 损坏。"
        ) from exc
    if (
        set(value) != expected
        or value.get("schema_version") != GOVERNANCE_MIGRATION_SCHEMA_VERSION
        or value.get("phase") != "activated"
        or re.fullmatch(
            r"https://github\.com/leevi2010-cursor/ArcheOS/issues/135"
            r"#issuecomment-[0-9]+",
            str(value.get("authority_ref")),
        )
        is None
        or value.get("authority_ref")
        == (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
            "#issuecomment-5353218136"
        )
        or value.get("implementation_plan_ref")
        != (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
            "#issuecomment-5353218136"
        )
        or legacy_receipt.get("schema_version")
        != LEGACY_GOVERNANCE_RECEIPT_SCHEMA_VERSION
        or legacy_receipt.get("phase") != "started"
        or not isinstance(ordered, list)
        or len(ordered) != 18
        or any(not isinstance(item, str) for item in ordered)
        or len(ordered) != len(set(ordered))
        or not isinstance(completed, list)
        or completed != ordered[:15]
        or not isinstance(remaining, list)
        or remaining != ordered[15:]
        or len(remaining) != 3
        or value.get("atomic_information_fingerprint")
        != _governance_atomic_fingerprint(ordered)
        or legacy_receipt.get("atomic_information_fingerprint")
        != value.get("atomic_information_fingerprint")
        or not _sha256_value(value.get("legacy_effect_fingerprint"))
        or not _sha256_value(value.get("pristine_remaining_fingerprint"))
        or not _sha256_value(value.get("authority_manifest_fingerprint"))
        or not _sha256_value(
            value.get("authority_manifest_raw_fingerprint")
        )
        or not _sha256_value(
            value.get("activation_business_tree_fingerprint")
        )
        or not isinstance(value.get("pending_human"), bool)
        or not isinstance(object_ids, list)
        or any(not isinstance(object_id, str) for object_id in object_ids)
        or object_ids != sorted(set(object_ids))
        or not _sha256_value(
            value.get("semantic_continuation_fingerprint")
        )
        or not _sha256_value(fingerprint)
        or fingerprint != _sha256_bytes(
            _canonical_json(projected).encode("utf-8")
        )
    ):
        raise WechatDigestError("微信 Governance migration receipt 损坏。")
    return dict(value)


def _validated_batch_governance_authority_manifest(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("Batch Governance authority manifest 损坏。")
    expected = {
        "schema_version",
        "authority_ref",
        "implementation_plan_ref",
        "activation_binding",
        "completed_atomic_information_ids",
        "remaining_atomic_information_ids",
        "completed_effect_bindings",
        "remaining_pristine_bindings",
        "business_tree_fingerprint",
        "previous_effective_head",
        "reviewed_git_head",
        "semantic_summary",
        "manifest_fingerprint",
    }
    projected = dict(value)
    fingerprint = projected.pop("manifest_fingerprint", None)
    completed = value.get("completed_atomic_information_ids")
    remaining = value.get("remaining_atomic_information_ids")
    completed_bindings = value.get("completed_effect_bindings")
    pristine_bindings = value.get("remaining_pristine_bindings")
    summary = value.get("semantic_summary")
    if (
        set(value) != expected
        or value.get("schema_version")
        != BATCH_GOVERNANCE_AUTHORITY_SCHEMA_VERSION
        or re.fullmatch(
            r"https://github\.com/leevi2010-cursor/ArcheOS/issues/135"
            r"#issuecomment-[0-9]+",
            str(value.get("authority_ref")),
        )
        is None
        or value.get("authority_ref")
        == (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
            "#issuecomment-5353218136"
        )
        or value.get("implementation_plan_ref")
        != (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
            "#issuecomment-5353218136"
        )
        or not isinstance(value.get("activation_binding"), dict)
        or not isinstance(completed, list)
        or len(completed) != 15
        or any(not isinstance(item, str) for item in completed)
        or len(completed) != len(set(completed))
        or not isinstance(remaining, list)
        or len(remaining) != 3
        or any(not isinstance(item, str) for item in remaining)
        or len(remaining) != len(set(remaining))
        or set(completed).intersection(remaining)
        or not isinstance(completed_bindings, list)
        or [
            item.get("atomic_information_id")
            for item in completed_bindings
            if isinstance(item, dict)
        ]
        != completed
        or len(completed_bindings) != len(completed)
        or not isinstance(pristine_bindings, list)
        or [
            item.get("atomic_information_id")
            for item in pristine_bindings
            if isinstance(item, dict)
        ]
        != remaining
        or len(pristine_bindings) != len(remaining)
        or any(
            not isinstance(item, dict)
            or item.get("revision_count") != 1
            or item.get("proposal_history_count") != 0
            or item.get("journal_count") != 0
            or item.get("apply_receipt_count") != 0
            for item in pristine_bindings
        )
        or not _sha256_value(value.get("business_tree_fingerprint"))
        or value.get("previous_effective_head")
        != "deaee94fe8c87ec84505a7de10d6f8d35eec87a5"
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("reviewed_git_head")))
        is None
        or summary
        != {
            "global_attempt_total": 220,
            "global_unknown": 0,
            "last_global_ordinal": 220,
            "next_global_ordinal": 221,
            "absolute_cap": 1000,
        }
        or not _sha256_value(fingerprint)
        or fingerprint
        != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
    ):
        raise WechatDigestError("Batch Governance authority manifest 损坏。")
    for binding in (*completed_bindings, *pristine_bindings):
        if not isinstance(binding, dict) or any(
            not _sha256_value(binding.get(field))
            for field in (
                "current_revision_fingerprint",
                "revision_history_fingerprint",
                "proposal_history_fingerprint",
                "journal_fingerprint",
                "apply_receipts_fingerprint",
                "world_projection_fingerprint",
                "effect_fingerprint",
            )
        ):
            raise WechatDigestError("Batch Governance authority effect 损坏。")
    return dict(value)


def _sha256_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    )


def _validated_governance_startup_recovery_receipt(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("Governance 启动恢复 receipt 损坏。")
    projected = dict(value)
    fingerprint = projected.pop("receipt_fingerprint", None)
    binding = value.get("recovery_binding")
    effect_bindings = value.get("atomic_effect_bindings")
    ordered_ids = (
        binding.get("ordered_atomic_information_ids")
        if isinstance(binding, dict)
        else None
    )
    if (
        set(value)
        != {
            "schema_version",
            "artifact_kind",
            "authority_ref",
            "authority_manifest_fingerprint",
            "authority_manifest_raw_fingerprint",
            "recovery_binding",
            "atomic_effect_bindings",
            "business_tree_fingerprint",
            "semantic_continuation_fingerprint",
            "provider_retry_permitted",
            "max_retry_attempts",
            "retry_consumed",
            "receipt_fingerprint",
        }
        or value.get("schema_version")
        != GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION
        or value.get("artifact_kind") != "governance_startup_recovery"
        or re.fullmatch(
            r"https://github\.com/leevi2010-cursor/ArcheOS/issues/[0-9]+"
            r"#issuecomment-[0-9]+",
            str(value.get("authority_ref")),
        )
        is None
        or not _sha256_value(value.get("authority_manifest_fingerprint"))
        or not _sha256_value(value.get("authority_manifest_raw_fingerprint"))
        or not isinstance(binding, dict)
        or re.fullmatch(r"run_[0-9a-f]{32}", str(binding.get("run_id")))
        is None
        or not isinstance(binding.get("item_id"), str)
        or not binding["item_id"]
        or not isinstance(ordered_ids, list)
        or not ordered_ids
        or len(set(ordered_ids)) != len(ordered_ids)
        or any(not isinstance(item, str) for item in ordered_ids)
        or not _sha256_value(
            binding.get("governance_started_receipt_fingerprint")
        )
        or any(
            not _sha256_value(field_value)
            for field_name, field_value in binding.items()
            if field_name.endswith("_fingerprint")
        )
        or not isinstance(effect_bindings, list)
        or len(effect_bindings) != len(ordered_ids)
        or not _sha256_value(value.get("business_tree_fingerprint"))
        or not _sha256_value(value.get("semantic_continuation_fingerprint"))
        or value.get("provider_retry_permitted") is not True
        or value.get("max_retry_attempts") != 1
        or value.get("retry_consumed") is not False
        or not _sha256_value(fingerprint)
        or fingerprint
        != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
    ):
        raise WechatDigestError("Governance 启动恢复 receipt 损坏。")
    for effect_binding in effect_bindings:
        if not isinstance(effect_binding, dict) or any(
            not _sha256_value(effect_binding.get(field))
            for field in (
                "current_revision_fingerprint",
                "revision_history_fingerprint",
                "proposal_history_fingerprint",
                "journal_fingerprint",
                "apply_receipts_fingerprint",
                "world_projection_fingerprint",
                "effect_fingerprint",
            )
        ):
            raise WechatDigestError("Governance 启动恢复 effect binding 损坏。")
    return dict(value)


def _validated_governance_startup_retry_receipt(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("Governance 启动恢复 retry receipt 损坏。")
    projected = dict(value)
    fingerprint = projected.pop("receipt_fingerprint", None)
    if (
        set(value)
        != {
            "schema_version",
            "artifact_kind",
            "recovery_receipt_fingerprint",
            "run_id",
            "item_id",
            "atomic_information_fingerprint",
            "retry_attempt",
            "consumed_at",
            "receipt_fingerprint",
        }
        or value.get("schema_version")
        != GOVERNANCE_STARTUP_RETRY_SCHEMA_VERSION
        or value.get("artifact_kind")
        != "governance_startup_retry_consumption"
        or not _sha256_value(value.get("recovery_receipt_fingerprint"))
        or re.fullmatch(r"run_[0-9a-f]{32}", str(value.get("run_id")))
        is None
        or not isinstance(value.get("item_id"), str)
        or not value["item_id"]
        or not _sha256_value(value.get("atomic_information_fingerprint"))
        or value.get("retry_attempt") != 1
        or not isinstance(value.get("consumed_at"), str)
        or not value["consumed_at"]
        or not _sha256_value(fingerprint)
        or fingerprint
        != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
    ):
        raise WechatDigestError("Governance 启动恢复 retry receipt 损坏。")
    return dict(value)


def _validated_governance_startup_recovery_manifest(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("Governance 启动恢复 authority manifest 损坏。")
    projected = dict(value)
    fingerprint = projected.pop("manifest_fingerprint", None)
    binding = value.get("recovery_binding")
    semantic_summary = value.get("semantic_summary")
    if (
        set(value)
        != {
            "schema_version",
            "authority_ref",
            "recovery_binding",
            "atomic_effect_bindings",
            "business_tree_fingerprint",
            "previous_reviewed_git_head",
            "reviewed_git_head",
            "execution_contract_unchanged",
            "semantic_summary",
            "manifest_fingerprint",
        }
        or value.get("schema_version")
        != GOVERNANCE_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION
        or re.fullmatch(
            r"https://github\.com/leevi2010-cursor/ArcheOS/issues/150"
            r"#issuecomment-[0-9]+",
            str(value.get("authority_ref")),
        )
        is None
        or not isinstance(binding, dict)
        or not isinstance(value.get("atomic_effect_bindings"), list)
        or not _sha256_value(value.get("business_tree_fingerprint"))
        or value.get("previous_reviewed_git_head")
        != "67d159411e968c6b0c2f787f9063a22682c10fb9"
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("reviewed_git_head")))
        is None
        or value.get("reviewed_git_head")
        == value.get("previous_reviewed_git_head")
        or value.get("execution_contract_unchanged") is not True
        or semantic_summary
        != {
            "global_attempt_total": 298,
            "global_unknown": 0,
            "last_global_ordinal": 298,
            "next_global_ordinal": 299,
            "absolute_cap": 1000,
        }
        or not _sha256_value(fingerprint)
        or fingerprint
        != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
    ):
        raise WechatDigestError("Governance 启动恢复 authority manifest 损坏。")
    expected_binding = {
        "run_id",
        "plan_fingerprint",
        "plan_receipt_fingerprint",
        "status_fingerprint",
        "capture_fingerprint",
        "checkpoint_fingerprint",
        "item_id",
        "source_id",
        "source_manifest_fingerprint",
        "representation_id",
        "representation_manifest_fingerprint",
        "representation_artifact_inventory_fingerprint",
        "semantic_package_fingerprint",
        "candidate_count",
        "residue_count",
        "ordered_atomic_information_ids",
        "ordered_atomic_revision_fingerprints",
        "governance_started_receipt_fingerprint",
        "governance_metrics_fingerprint",
        "semantic_window_binding_fingerprint",
    }
    if (
        set(binding) != expected_binding
        or binding.get("candidate_count") != 4
        or binding.get("residue_count") != 1
        or not isinstance(binding.get("ordered_atomic_information_ids"), list)
        or len(binding["ordered_atomic_information_ids"]) != 4
        or len(set(binding["ordered_atomic_information_ids"])) != 4
        or any(
            not isinstance(item, str)
            for item in binding["ordered_atomic_information_ids"]
        )
        or not isinstance(
            binding.get("ordered_atomic_revision_fingerprints"), list
        )
        or len(binding["ordered_atomic_revision_fingerprints"]) != 4
        or any(
            not _sha256_value(item)
            for item in binding["ordered_atomic_revision_fingerprints"]
        )
        or any(
            not _sha256_value(binding.get(field))
            for field in expected_binding
            if field.endswith("_fingerprint")
        )
    ):
        raise WechatDigestError("Governance 启动恢复 authority manifest 损坏。")
    return dict(value)


def _validated_item_governance_startup_recovery_manifest(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("Governance 单项启动恢复 authority manifest 损坏。")
    projected = dict(value)
    fingerprint = projected.pop("manifest_fingerprint", None)
    binding = value.get("recovery_binding")
    semantic = value.get("semantic_snapshot")
    expected_binding = {
        "run_id", "plan_fingerprint", "plan_receipt_fingerprint",
        "status_fingerprint", "capture_fingerprint", "checkpoint_fingerprint",
        "item_id", "source_id", "source_manifest_fingerprint",
        "representation_id", "representation_manifest_fingerprint",
        "representation_artifact_inventory_fingerprint",
        "semantic_package_fingerprint", "candidate_count", "residue_count",
        "ordered_atomic_information_ids",
        "ordered_atomic_revision_fingerprints",
        "governance_started_receipt_fingerprint",
        "governance_metrics_fingerprint", "semantic_window_binding_fingerprint",
        "semantic_snapshot_fingerprint",
    }
    semantic_fields = {
        "global_attempt_total", "global_unknown", "last_global_ordinal",
        "next_global_ordinal", "absolute_cap", "commit_cursor_fingerprint",
        "target_global_ordinal_range", "commit_cursor_ordinal",
        "latest_attempt_receipt_fingerprint", "semantic_run_id", "batch_ordinal",
        "result_binding_fingerprint", "processing_audit_fingerprint",
        "global_authority_fingerprint", "reviewed_git_head",
        "execution_contract_fingerprint", "reviewed_head_sequence",
        "reviewed_head_chain_fingerprint",
        "ledger_tail_attempt_receipt_fingerprint",
    }
    ordered_ids = binding.get("ordered_atomic_information_ids") if isinstance(binding, dict) else None
    revisions = binding.get("ordered_atomic_revision_fingerprints") if isinstance(binding, dict) else None
    numeric_semantic = (
        "global_attempt_total", "global_unknown", "last_global_ordinal",
        "next_global_ordinal", "absolute_cap", "batch_ordinal",
        "reviewed_head_sequence", "commit_cursor_ordinal",
    )
    if (
        set(value) != {
            "schema_version", "authority_ref", "recovery_binding",
            "atomic_effect_bindings", "business_tree_fingerprint",
            "reviewed_git_head", "execution_contract_unchanged",
            "semantic_snapshot", "manifest_fingerprint",
        }
        or value.get("schema_version") != GOVERNANCE_ITEM_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION
        or re.fullmatch(
            r"https://github\.com/leevi2010-cursor/ArcheOS/issues/[0-9]+#issuecomment-[0-9]+",
            str(value.get("authority_ref")),
        ) is None
        or not isinstance(binding, dict) or set(binding) != expected_binding
        or not isinstance(ordered_ids, list) or not ordered_ids
        or len(set(ordered_ids)) != len(ordered_ids)
        or any(not isinstance(item, str) for item in ordered_ids)
        or not isinstance(revisions, list) or len(revisions) != len(ordered_ids)
        or any(not _sha256_value(item) for item in revisions)
        or binding.get("candidate_count") != len(ordered_ids)
        or isinstance(binding.get("residue_count"), bool)
        or not isinstance(binding.get("residue_count"), int)
        or int(binding["residue_count"]) < 0
        or any(not _sha256_value(binding.get(field)) for field in expected_binding if field.endswith("_fingerprint"))
        or not isinstance(value.get("atomic_effect_bindings"), list)
        or len(value["atomic_effect_bindings"]) != len(ordered_ids)
        or not _sha256_value(value.get("business_tree_fingerprint"))
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("reviewed_git_head"))) is None
        or value.get("execution_contract_unchanged") is not True
        or not isinstance(semantic, dict) or set(semantic) != semantic_fields
        or any(isinstance(semantic.get(field), bool) or not isinstance(semantic.get(field), int) for field in numeric_semantic)
        or semantic.get("reviewed_git_head") != value.get("reviewed_git_head")
        or semantic.get("global_unknown") != 0
        or semantic.get("last_global_ordinal") != semantic.get("global_attempt_total")
        or semantic.get("next_global_ordinal") != semantic.get("global_attempt_total") + 1
        or not isinstance(semantic.get("target_global_ordinal_range"), list)
        or len(semantic["target_global_ordinal_range"]) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in semantic["target_global_ordinal_range"])
        or semantic["target_global_ordinal_range"][1] != semantic.get("commit_cursor_ordinal")
        or any(not _sha256_value(semantic.get(field)) for field in semantic_fields if field.endswith("_fingerprint"))
        or not _sha256_value(fingerprint)
        or fingerprint != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
    ):
        raise WechatDigestError("Governance 单项启动恢复 authority manifest 损坏。")
    return dict(value)


def _validated_multi_governance_startup_recovery_manifest(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("多项目 Governance 启动恢复 authority manifest 损坏。")
    projected = dict(value)
    fingerprint = projected.pop("manifest_fingerprint", None)
    binding = value.get("recovery_binding")
    semantic_summary = value.get("semantic_summary")
    if (
        set(value)
        != {
            "schema_version",
            "authority_ref",
            "recovery_binding",
            "atomic_effect_bindings",
            "business_tree_fingerprint",
            "previous_reviewed_git_head",
            "reviewed_git_head",
            "execution_contract_unchanged",
            "semantic_summary",
            "manifest_fingerprint",
        }
        or value.get("schema_version")
        != GOVERNANCE_MULTI_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION
        or re.fullmatch(
            r"https://github\.com/leevi2010-cursor/ArcheOS/issues/168"
            r"#issuecomment-[0-9]+",
            str(value.get("authority_ref")),
        )
        is None
        or not isinstance(binding, dict)
        or not isinstance(value.get("atomic_effect_bindings"), list)
        or not _sha256_value(value.get("business_tree_fingerprint"))
        or value.get("previous_reviewed_git_head")
        != "ce49d89355caab38da08b4522f416d248c60646b"
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("reviewed_git_head")))
        is None
        or value.get("reviewed_git_head")
        == value.get("previous_reviewed_git_head")
        or value.get("execution_contract_unchanged") is not True
        or semantic_summary
        != {
            "global_attempt_total": 302,
            "global_unknown": 0,
            "last_global_ordinal": 302,
            "next_global_ordinal": 303,
            "absolute_cap": 1000,
        }
        or not _sha256_value(fingerprint)
        or fingerprint
        != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
    ):
        raise WechatDigestError("多项目 Governance 启动恢复 authority manifest 损坏。")
    expected_binding = {
        "run_id",
        "plan_fingerprint",
        "plan_receipt_fingerprint",
        "status_fingerprint",
        "capture_fingerprint",
        "checkpoint_fingerprint",
        "item_id",
        "source_id",
        "source_manifest_fingerprint",
        "representation_id",
        "representation_manifest_fingerprint",
        "representation_artifact_inventory_fingerprint",
        "semantic_package_fingerprint",
        "candidate_count",
        "residue_count",
        "ordered_atomic_information_ids",
        "ordered_atomic_revision_fingerprints",
        "governance_started_receipt_fingerprint",
        "governance_metrics_fingerprint",
        "semantic_window_binding_fingerprint",
        "startup_recovery_inventory_fingerprint",
        "startup_retry_inventory_fingerprint",
    }
    ordered_ids = binding.get("ordered_atomic_information_ids")
    revision_fingerprints = binding.get(
        "ordered_atomic_revision_fingerprints"
    )
    if (
        set(binding) != expected_binding
        or binding.get("candidate_count") != 3
        or isinstance(binding.get("residue_count"), bool)
        or not isinstance(binding.get("residue_count"), int)
        or binding["residue_count"] < 0
        or not isinstance(ordered_ids, list)
        or len(ordered_ids) != 3
        or len(set(ordered_ids)) != 3
        or any(not isinstance(item, str) for item in ordered_ids)
        or not isinstance(revision_fingerprints, list)
        or len(revision_fingerprints) != 3
        or any(not _sha256_value(item) for item in revision_fingerprints)
        or any(
            not _sha256_value(binding.get(field))
            for field in expected_binding
            if field.endswith("_fingerprint")
        )
    ):
        raise WechatDigestError("多项目 Governance 启动恢复 authority manifest 损坏。")
    effect_bindings = value["atomic_effect_bindings"]
    for effect_binding in effect_bindings:
        if not isinstance(effect_binding, dict) or any(
            not _sha256_value(effect_binding.get(field))
            for field in (
                "current_revision_fingerprint",
                "revision_history_fingerprint",
                "proposal_history_fingerprint",
                "journal_fingerprint",
                "apply_receipts_fingerprint",
                "world_projection_fingerprint",
                "effect_fingerprint",
            )
        ):
            raise WechatDigestError(
                "多项目 Governance authority effect binding 损坏。"
            )
    return dict(value)


def _validated_failed_closed_recovery_manifest(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WechatDigestError("历史失败恢复 authority manifest 损坏。")
    projected = dict(value)
    fingerprint = projected.pop("manifest_fingerprint", None)
    binding = value.get("recovery_binding")
    summary = value.get("semantic_summary")
    historical_summary = value.get("historical_failed_closed_summary")
    if (
        set(value)
        != {
            "schema_version",
            "authority_ref",
            "recovery_binding",
            "business_tree_fingerprint",
            "previous_reviewed_git_head",
            "reviewed_git_head",
            "execution_contract_unchanged",
            "semantic_summary",
            "historical_failed_closed_summary",
            "manifest_fingerprint",
        }
        or value.get("schema_version")
        != FAILED_CLOSED_RECOVERY_MANIFEST_SCHEMA_VERSION
        or re.fullmatch(
            r"https://github\.com/leevi2010-cursor/ArcheOS/issues/154"
            r"#issuecomment-[0-9]+",
            str(value.get("authority_ref")),
        )
        is None
        or not isinstance(binding, dict)
        or not _sha256_value(value.get("business_tree_fingerprint"))
        or value.get("previous_reviewed_git_head")
        != "c8ece3782ae3ba289d06c36d1e352ce23e0f627b"
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("reviewed_git_head")))
        is None
        or value.get("reviewed_git_head")
        == value.get("previous_reviewed_git_head")
        or value.get("execution_contract_unchanged") is not True
        or summary
        != {
            "global_attempt_total": 298,
            "global_unknown": 0,
            "last_global_ordinal": 298,
            "next_global_ordinal": 299,
            "absolute_cap": 1000,
        }
        or not isinstance(historical_summary, dict)
        or set(historical_summary)
        != {
            "total",
            "semantic",
            "governance",
            "inventory_fingerprint",
        }
        or historical_summary.get("total") != 5
        or historical_summary.get("semantic") != 2
        or historical_summary.get("governance") != 3
        or not _sha256_value(
            historical_summary.get("inventory_fingerprint")
        )
        or not _sha256_value(fingerprint)
        or fingerprint
        != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
    ):
        raise WechatDigestError("历史失败恢复 authority manifest 损坏。")
    expected_binding = {
        "run_id",
        "plan_fingerprint",
        "plan_receipt_fingerprint",
        "current_status_fingerprint",
        "current_failed_status",
        "capture_fingerprint",
        "checkpoint_fingerprint",
        "current_item_id",
        "source_id",
        "source_manifest_fingerprint",
        "representation_id",
        "representation_manifest_fingerprint",
        "representation_artifact_inventory_fingerprint",
        "previous_item_id",
        "previous_atomic_information_ids",
        "previous_governance_receipt_fingerprint",
        "startup_recovery_receipt_fingerprint",
        "startup_retry_receipt_fingerprint",
        "semantic_window_binding_fingerprint",
    }
    if (
        set(binding) != expected_binding
        or not isinstance(binding.get("current_failed_status"), dict)
        or binding.get("current_status_fingerprint")
        != _sha256_bytes(
            _canonical_json(binding.get("current_failed_status")).encode(
                "utf-8"
            )
        )
        or not isinstance(binding.get("previous_atomic_information_ids"), list)
        or len(binding["previous_atomic_information_ids"]) != 4
        or len(set(binding["previous_atomic_information_ids"])) != 4
        or any(
            not isinstance(item, str)
            for item in binding["previous_atomic_information_ids"]
        )
        or any(
            not _sha256_value(binding.get(field))
            for field in expected_binding
            if field.endswith("_fingerprint")
        )
    ):
        raise WechatDigestError("历史失败恢复 authority manifest 损坏。")
    return dict(value)


def _validated_global_attempt_summary(value: object) -> dict[str, int]:
    expected = {
        "global_attempt_total",
        "global_unknown",
        "next_global_ordinal",
        "absolute_cap",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise WechatDigestError("Semantic global attempt summary 损坏。")
    if any(
        isinstance(value[field], bool)
        or not isinstance(value[field], int)
        or value[field] < 0
        for field in expected
    ):
        raise WechatDigestError("Semantic global attempt summary 损坏。")
    summary = {field: int(value[field]) for field in expected}
    if (
        summary["global_attempt_total"] < 1
        or summary["global_unknown"] != 0
        or summary["next_global_ordinal"]
        != summary["global_attempt_total"] + 1
        or summary["global_attempt_total"] > summary["absolute_cap"]
    ):
        raise WechatDigestError(
            "Semantic global attempt ledger 尚未满足治理超时封存条件。"
        )
    return summary


def _capture_fingerprint(capture: WechatCapture) -> str:
    payload = {
        "provider_version": capture.provider_version,
        "after_cursor": capture.after_cursor.to_dict(),
        "upper_bound": capture.upper_bound.to_dict(),
        "messages": [
            {
                "conversation_key": message.conversation_key,
                "provider_conversation_id": message.provider_conversation_id,
                "conversation_label": message.conversation_label,
                "is_group": message.is_group,
                "message_key": message.message_key,
                "cursor": message.cursor.to_dict(),
                "sender_label": message.sender_label,
                "message_type": message.message_type,
                "timestamp": message.timestamp,
                "sent_at": message.sent_at,
                "visible_content": message.visible_content,
                "structured_payload": message.structured_payload,
                "attachments": [
                    {
                        "attachment_key": attachment.attachment_key,
                        "status": attachment.status,
                        "filename_hint": attachment.filename_hint,
                        "media_type": attachment.media_type,
                        "content_hash": attachment.content_hash,
                        "size_bytes": attachment.size_bytes,
                    }
                    for attachment in message.attachments
                ],
            }
            for message in capture.messages
        ],
    }
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def _capture_snapshot_payload(capture: WechatCapture) -> dict[str, object]:
    """Return the private, complete Processing snapshot for one fixed window."""

    return {
        "schema_version": CAPTURE_SNAPSHOT_SCHEMA_VERSION,
        "provider_version": capture.provider_version,
        "after_cursor": capture.after_cursor.to_dict(),
        "upper_bound": capture.upper_bound.to_dict(),
        "messages": [
            {
                "conversation_key": message.conversation_key,
                "provider_conversation_id": message.provider_conversation_id,
                "conversation_label": message.conversation_label,
                "is_group": message.is_group,
                "message_key": message.message_key,
                "cursor": message.cursor.to_dict(),
                "sender_label": message.sender_label,
                "message_type": message.message_type,
                "timestamp": message.timestamp,
                "sent_at": message.sent_at,
                "visible_content": message.visible_content,
                "structured_payload": message.structured_payload,
                "attachments": [
                    {
                        "attachment_key": attachment.attachment_key,
                        "status": attachment.status,
                        "filename_hint": attachment.filename_hint,
                        "media_type": attachment.media_type,
                        "path": (
                            None
                            if attachment.path is None
                            else str(attachment.path)
                        ),
                        "content_hash": attachment.content_hash,
                        "size_bytes": attachment.size_bytes,
                    }
                    for attachment in message.attachments
                ],
            }
            for message in capture.messages
        ],
    }


def _capture_from_snapshot(value: object) -> WechatCapture:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "provider_version",
        "after_cursor",
        "upper_bound",
        "messages",
    }:
        raise WechatDigestError("微信 capture snapshot 损坏。")
    if value.get("schema_version") != CAPTURE_SNAPSHOT_SCHEMA_VERSION:
        raise WechatDigestError("微信 capture snapshot schema 不受支持。")
    provider_version = value.get("provider_version")
    messages_value = value.get("messages")
    if not isinstance(provider_version, str) or not isinstance(messages_value, list):
        raise WechatDigestError("微信 capture snapshot 损坏。")
    try:
        after = WechatCursor.from_dict(value.get("after_cursor"), "snapshot.after_cursor")
        upper = WechatCursor.from_dict(value.get("upper_bound"), "snapshot.upper_bound")
        messages: list[CapturedMessage] = []
        expected_message_fields = {
            "conversation_key",
            "provider_conversation_id",
            "conversation_label",
            "is_group",
            "message_key",
            "cursor",
            "sender_label",
            "message_type",
            "timestamp",
            "sent_at",
            "visible_content",
            "structured_payload",
            "attachments",
        }
        expected_attachment_fields = {
            "attachment_key",
            "status",
            "filename_hint",
            "media_type",
            "path",
            "content_hash",
            "size_bytes",
        }
        for raw_message in messages_value:
            if not isinstance(raw_message, dict) or set(raw_message) != expected_message_fields:
                raise ValueError("message")
            raw_attachments = raw_message["attachments"]
            if not isinstance(raw_attachments, list):
                raise ValueError("attachments")
            attachments: list[CapturedAttachment] = []
            for raw_attachment in raw_attachments:
                if (
                    not isinstance(raw_attachment, dict)
                    or set(raw_attachment) != expected_attachment_fields
                    or raw_attachment.get("status")
                    not in {"available", "missing", "ambiguous"}
                ):
                    raise ValueError("attachment")
                raw_path = raw_attachment.get("path")
                path = None if raw_path is None else Path(str(raw_path))
                if (
                    (raw_attachment.get("status") == "available" and (path is None or not path.is_absolute()))
                    or (raw_attachment.get("status") != "available" and path is not None)
                ):
                    raise ValueError("attachment path")
                content_hash = raw_attachment.get("content_hash")
                size_bytes = raw_attachment.get("size_bytes")
                if raw_attachment.get("status") == "available":
                    if not _sha256_value(content_hash) or (
                        isinstance(size_bytes, bool)
                        or not isinstance(size_bytes, int)
                        or size_bytes < 0
                    ):
                        raise ValueError("attachment identity")
                elif content_hash is not None or size_bytes is not None:
                    raise ValueError("attachment identity")
                attachments.append(
                    CapturedAttachment(
                        str(raw_attachment["attachment_key"]),
                        str(raw_attachment["status"]),
                        str(raw_attachment["filename_hint"]),
                        str(raw_attachment["media_type"]),
                        path,
                        content_hash if isinstance(content_hash, str) else None,
                        size_bytes if isinstance(size_bytes, int) else None,
                    )
                )
            strings = tuple(
                raw_message[field]
                for field in (
                    "conversation_key",
                    "provider_conversation_id",
                    "conversation_label",
                    "message_key",
                    "sender_label",
                    "message_type",
                    "sent_at",
                    "visible_content",
                    "structured_payload",
                )
            )
            if any(not isinstance(item, str) for item in strings):
                raise ValueError("message strings")
            if not isinstance(raw_message["is_group"], bool) or (
                isinstance(raw_message["timestamp"], bool)
                or not isinstance(raw_message["timestamp"], int)
            ):
                raise ValueError("message types")
            messages.append(
                CapturedMessage(
                    conversation_key=str(raw_message["conversation_key"]),
                    provider_conversation_id=str(raw_message["provider_conversation_id"]),
                    conversation_label=str(raw_message["conversation_label"]),
                    is_group=bool(raw_message["is_group"]),
                    message_key=str(raw_message["message_key"]),
                    cursor=WechatCursor.from_dict(raw_message["cursor"], "snapshot.message.cursor"),
                    sender_label=str(raw_message["sender_label"]),
                    message_type=str(raw_message["message_type"]),
                    timestamp=int(raw_message["timestamp"]),
                    sent_at=str(raw_message["sent_at"]),
                    visible_content=str(raw_message["visible_content"]),
                    structured_payload=str(raw_message["structured_payload"]),
                    attachments=tuple(attachments),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise WechatDigestError("微信 capture snapshot 损坏。") from exc
    capture = WechatCapture(provider_version, after, upper, tuple(messages))
    if (
        upper < after
        or tuple(message.cursor for message in capture.messages)
        != tuple(sorted(message.cursor for message in capture.messages))
        or len({message.message_key for message in capture.messages})
        != len(capture.messages)
        or any(not after < message.cursor <= upper for message in capture.messages)
    ):
        raise WechatDigestError("微信 capture snapshot 顺序损坏。")
    return capture


def _capture_index_payload(capture: WechatCapture) -> dict[str, object]:
    indexes_by_conversation: dict[str, list[int]] = {}
    for index, message in enumerate(capture.messages):
        indexes_by_conversation.setdefault(message.conversation_key, []).append(index)
    conversations: list[dict[str, object]] = []
    for conversation_key in sorted(indexes_by_conversation):
        indexes = indexes_by_conversation[conversation_key]
        conversations.append(
            {
                "conversation_key": conversation_key,
                "message_indexes": indexes,
                "message_keys": [capture.messages[index].message_key for index in indexes],
            }
        )
    attachments = [
        {
            "attachment_key": attachment.attachment_key,
            "message_index": message_index,
            "message_key": message.message_key,
        }
        for message_index, message in enumerate(capture.messages)
        for attachment in message.attachments
    ]
    return {
        "schema_version": CAPTURE_INDEX_SCHEMA_VERSION,
        "conversations": conversations,
        "attachments": attachments,
    }


def _capture_summary_payload(
    capture: WechatCapture,
    *,
    capture_ms: int,
) -> dict[str, object]:
    statuses = Counter(
        attachment.status
        for message in capture.messages
        for attachment in message.attachments
    )
    return {
        "schema_version": CAPTURE_SUMMARY_SCHEMA_VERSION,
        "provider_version": capture.provider_version,
        "after_cursor": capture.after_cursor.to_dict(),
        "upper_bound": capture.upper_bound.to_dict(),
        "message_count": len(capture.messages),
        "conversation_count": len({item.conversation_key for item in capture.messages}),
        "attachment_count": sum(len(item.attachments) for item in capture.messages),
        "attachment_status_counts": dict(sorted(statuses.items())),
        "capture_ms": capture_ms,
    }


def _capture_plan_projection(plan: Mapping[str, object]) -> dict[str, object]:
    projected = dict(plan)
    projected["schema_version"] = RUN_PLAN_SCHEMA_VERSION
    projected.pop("capture_receipt_fingerprint", None)
    return projected


def _conversation_source_payload(
    capture: WechatCapture,
    conversation_key: str,
    attachment_sources: Mapping[str, str | None],
    *,
    message_indexes: Sequence[int] | None = None,
) -> bytes:
    selected = (
        tuple(capture.messages[index] for index in message_indexes)
        if message_indexes is not None
        else tuple(
            message
            for message in capture.messages
            if message.conversation_key == conversation_key
        )
    )
    if not selected:
        raise WechatDigestError("微信运行计划缺少 Conversation 消息。")
    if any(message.conversation_key != conversation_key for message in selected):
        raise WechatDigestError("微信 Conversation index 越界。")
    first = selected[0]
    payload = {
        "schema_version": "wechat-capture-source/1.0",
        "provider": "wechat",
        "provider_version": capture.provider_version,
        "range": {
            "after_cursor": capture.after_cursor.to_dict(),
            "upper_bound": capture.upper_bound.to_dict(),
        },
        "conversation": {
            "conversation_key": first.conversation_key,
            "provider_conversation_id": first.provider_conversation_id,
            "conversation_label": first.conversation_label,
            "is_group": first.is_group,
        },
        "messages": [
            {
                "message_key": message.message_key,
                "cursor": message.cursor.to_dict(),
                "sender_label": message.sender_label,
                "sent_at": message.sent_at,
                "timestamp": message.timestamp,
                "message_type": message.message_type,
                "visible_content": message.visible_content,
                "structured_payload": message.structured_payload,
                "attachments": [
                    {
                        "attachment_key": attachment.attachment_key,
                        "status": attachment.status,
                        "source_id": attachment_sources.get(
                            attachment.attachment_key
                        ),
                        "filename_hint": attachment.filename_hint,
                        "media_type": attachment.media_type,
                        "content_hash": attachment.content_hash,
                        "size_bytes": attachment.size_bytes,
                    }
                    for attachment in message.attachments
                ],
            }
            for message in selected
        ],
    }
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _build_plan(
    capture: WechatCapture, *, clock: Callable[[], str],
    semantic_batch_size: int = DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
    all_history_upper_bound: WechatCursor | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
    capture_receipt_fingerprint: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    if (
        all_history_upper_bound is not None
        and capture.upper_bound > all_history_upper_bound
    ):
        raise WechatDigestError("微信窗口超出冻结的全历史边界。")
    fingerprint = _capture_fingerprint(capture)
    created_at = created_at or clock()
    run_id = run_id or _stable_id(
        "run",
        fingerprint,
        _canonical_json(capture.after_cursor.to_dict()),
        _canonical_json(capture.upper_bound.to_dict()),
        created_at,
    )
    attachment_sources: dict[str, str | None] = {}
    attachment_plans: list[dict[str, object]] = []
    for message in capture.messages:
        for attachment in message.attachments:
            source_id = (
                _stable_id(
                    "src", run_id, message.message_key, attachment.attachment_key
                )
                if attachment.status == "available"
                else None
            )
            attachment_sources[attachment.attachment_key] = source_id
            attachment_plans.append(
                {
                    "attachment_key": attachment.attachment_key,
                    "message_key": message.message_key,
                    "status": attachment.status,
                    "source_id": source_id,
                    "content_hash": attachment.content_hash,
                    "size_bytes": attachment.size_bytes,
                    "media_type": attachment.media_type,
                    "filename_hint": attachment.filename_hint,
                }
            )
    conversation_plans: list[dict[str, object]] = []
    indexes_by_conversation: dict[str, list[int]] = {}
    for index, message in enumerate(capture.messages):
        indexes_by_conversation.setdefault(message.conversation_key, []).append(index)
    for conversation_key in sorted(indexes_by_conversation):
        source_id = _stable_id("src", run_id, conversation_key)
        payload = _conversation_source_payload(
            capture,
            conversation_key,
            attachment_sources,
            message_indexes=indexes_by_conversation[conversation_key],
        )
        conversation_plans.append(
            {
                "conversation_key": conversation_key,
                "source_id": source_id,
                "content_hash": _sha256_bytes(payload),
                "size_bytes": len(payload),
                "filename_hint": f"wechat-{conversation_key[-12:]}.json",
                "message_keys": [
                    capture.messages[index].message_key
                    for index in indexes_by_conversation[conversation_key]
                ],
            }
        )
    plan: dict[str, object] = {
        "schema_version": (
            RUN_PLAN_SCHEMA_VERSION
            if capture_receipt_fingerprint is None
            else SNAPSHOT_RUN_PLAN_SCHEMA_VERSION
        ),
        "run_id": run_id,
        "created_at": created_at,
        "provider_version": capture.provider_version,
        "after_cursor": capture.after_cursor.to_dict(),
        "upper_bound": capture.upper_bound.to_dict(),
        "all_history_upper_bound": (
            None
            if all_history_upper_bound is None
            else all_history_upper_bound.to_dict()
        ),
        "capture_fingerprint": fingerprint,
        "semantic_batch_size": semantic_batch_size,
        "message_keys": [message.message_key for message in capture.messages],
        "conversations": conversation_plans,
        "attachments": attachment_plans,
    }
    if capture_receipt_fingerprint is not None:
        if not _sha256_value(capture_receipt_fingerprint):
            raise WechatDigestError("微信 capture receipt fingerprint 无效。")
        plan["capture_receipt_fingerprint"] = capture_receipt_fingerprint
    items = {
        f"conversation:{item['conversation_key']}": {
            "kind": "conversation",
            "state": "planned",
            "source_id": item["source_id"],
            "representation_id": None,
            "privacy_route": None,
            "privacy_categories": [],
            "atomic_information_ids": [],
            "pending_human": False,
            "context_object_ids": [],
            "message_count": len(item["message_keys"]),
        }
        for item in conversation_plans
    }
    for attachment in attachment_plans:
        items[f"attachment:{attachment['attachment_key']}"] = {
            "kind": "attachment",
            "state": (
                "planned"
                if attachment["status"] == "available"
                else "unsupported"
            ),
            "source_id": attachment["source_id"],
            "representation_id": None,
            "privacy_route": None,
            "privacy_categories": [],
            "atomic_information_ids": [],
            "pending_human": False,
            "context_object_ids": [],
            "message_count": 0,
            "attachment_status": attachment["status"],
        }
    plan_fingerprint = _plan_fingerprint(plan)
    status: dict[str, object] = {
        "schema_version": RUN_STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "state": "planned",
        "failure_category": None,
        "checkpoint_published": False,
        "plan_fingerprint": plan_fingerprint,
        "items": items,
        "updated_at": clock(),
    }
    return plan, status


def _plan_fingerprint(plan: Mapping[str, object]) -> str:
    keys = [
        "schema_version",
        "run_id",
        "after_cursor",
        "upper_bound",
        "capture_fingerprint",
        "semantic_batch_size",
        "conversations",
    ]
    if plan.get("schema_version") in {
        RUN_PLAN_SCHEMA_VERSION,
        SNAPSHOT_RUN_PLAN_SCHEMA_VERSION,
    }:
        keys.insert(4, "all_history_upper_bound")
    if plan.get("schema_version") == SNAPSHOT_RUN_PLAN_SCHEMA_VERSION:
        keys.insert(7, "capture_receipt_fingerprint")
    return _sha256_bytes(_canonical_json({key: plan.get(key) for key in keys}).encode("utf-8"))


def _committed_plan_receipt(
    run_id: str, plan: Mapping[str, object]
) -> dict[str, object]:
    return {
        "schema_version": RUN_PLAN_RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "phase": "committed",
        "plan_fingerprint": _plan_fingerprint(plan),
    }


def _committed_receipt_fingerprint(receipt: Mapping[str, object]) -> str:
    if receipt.get("schema_version") == LEGACY_RUN_PLAN_RECEIPT_SCHEMA_VERSION or (
        receipt.get("schema_version") == RUN_PLAN_RECEIPT_SCHEMA_VERSION
        and receipt.get("phase") == "committed"
    ):
        value = receipt.get("plan_fingerprint")
    else:
        raise WechatDigestError("微信运行计划 receipt 尚未提交。")
    if not isinstance(value, str):
        raise WechatDigestError("微信运行计划 receipt 损坏。")
    return value


def _previous_plan_projection(plan: Mapping[str, object]) -> dict[str, object]:
    projected = dict(plan)
    projected["schema_version"] = PREVIOUS_RUN_PLAN_SCHEMA_VERSION
    projected.pop("all_history_upper_bound", None)
    return projected


def _plan_sequence(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise WechatDigestError(f"微信运行计划 {field} 损坏。")
    return value  # type: ignore[return-value]


class _GovernanceProviderCallBoundary:
    def __init__(
        self,
        delegate: AtomicInformationInterpretationProvider,
        before_call: Callable[[], None],
    ) -> None:
        self.delegate = delegate
        self.before_call = before_call
        self.name = delegate.name

    def interpret(self, atomic_information, current_world_state):
        self.before_call()
        return self.delegate.interpret(atomic_information, current_world_state)

    def interpret_batch(self, items):
        self.before_call()
        method = getattr(self.delegate, "interpret_batch", None)
        if not callable(method):
            if len(items) == 1:
                atomic_information, current_world_state = items[0]
                return (
                    self.delegate.interpret(
                        atomic_information, current_world_state
                    ),
                )
            raise RuntimeError(
                "batch interpretation provider is required for multi-item digestion"
            )
        return method(items)


class WechatDigestService:
    def __init__(
        self,
        *,
        workspace: Path,
        capture_provider: WechatCaptureProvider,
        semantic_handoff_factory: Callable[[], SemanticHandoffPort],
        interpretation_provider: AtomicInformationInterpretationProvider,
        privacy_gate: DeterministicPrivacyGate | None = None,
        run_store: WechatDigestRunStore | None = None,
        semantic_batch_size: int = DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
        semantic_parallelism: int = 2,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.workspace = Path(workspace)
        self.capture_provider = capture_provider
        self.semantic_handoff_factory = semantic_handoff_factory
        self.interpretation_provider = interpretation_provider
        self.privacy_gate = privacy_gate or DeterministicPrivacyGate()
        self.clock = clock
        self.source_repository = LocalManagedSourceRepository(
            self.workspace / "01_inbox"
        )
        self.source_service = ManagedSourceService(self.source_repository)
        self.representation_repository = LocalRepresentationRepository(
            self.workspace / "02_processing" / "representations"
        )
        self.representation_service = RepresentationService(
            self.source_repository, self.representation_repository
        )
        self.information_store = JsonlAtomicInformationStore(
            self.workspace / "03_information" / "atomic_information.jsonl"
        )
        self.proposal_store = JsonlChangeProposalStore(
            self.workspace / "03_information" / "change_proposals.jsonl"
        )
        self.journal = JsonlChangeJournal(
            self.workspace / "03_information" / "change_journal.jsonl"
        )
        self.database = self.workspace / "04_core" / "archeos.sqlite3"
        self.run_store = run_store or WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest", clock=clock
        )
        self._semantic_handoff: SemanticHandoffPort | None = None
        self._before_governance_provider_call: Callable[[], None] | None = None
        self._governance_resume_state: dict[str, object] | None = None
        self._governance_migration_state: dict[str, object] | None = None
        self._governance_execution_lock = threading.Lock()
        self._governance_observation_lock = threading.Lock()
        self._governance_active = 0
        self._after_governance_batch_interpretation: Callable[
            [
                tuple[str, ...],
                tuple[InterpretationResult, ...],
                bool,
                tuple[str, ...],
                tuple[dict[str, object], ...],
            ],
            None,
        ] | None = None
        self._before_governance_application: Callable[[int], None] | None = None
        self._after_governance_application: Callable[
            [int, bool, tuple[str, ...], tuple[dict[str, object], ...]], None
        ] | None = None
        if isinstance(semantic_batch_size, bool) or not isinstance(semantic_batch_size, int) or semantic_batch_size < 1:
            raise ValueError("semantic batch size must be positive")
        if semantic_parallelism not in {1, 2, 3, 4}:
            raise ValueError("semantic parallelism must be between 1 and 4")
        self.semantic_batch_size = semantic_batch_size
        self.semantic_parallelism = semantic_parallelism
        self._segment_performance: dict[str, int] = {}
        self._capture_reasons: list[str] = []
        self._committed_result_wave: dict[str, dict[str, object]] = {}
        self._completed_window_chain_cache: tuple[
            tuple[object, ...], tuple[SemanticCompletedWindowBinding, ...]
        ] | None = None

    @staticmethod
    def _cursor_tuple(cursor: WechatCursor) -> tuple[int, str, str]:
        return (cursor.timestamp, cursor.conversation_key, cursor.message_key)

    def _observed_capture(
        self,
        after_cursor: WechatCursor,
        *,
        upper_bound: WechatCursor | None = None,
        all_history_upper_bound: WechatCursor | None = None,
        observe_only: bool = False,
        upper_probe: bool = False,
        completed_window_replay: bool = False,
        reason: str,
    ) -> tuple[WechatCapture, int]:
        """Record the real connector boundary before and after each attempt."""

        started = time.monotonic()
        self._capture_reasons.append(reason)
        if self._segment_performance:
            self._segment_performance["capture_attempts"] += 1
            if upper_probe:
                self._segment_performance["upper_bound_probe_calls"] += 1
            elif completed_window_replay:
                self._segment_performance[
                    "completed_window_connector_replays"
                ] += 1
        try:
            capture = self.capture_provider.capture(
                after_cursor,
                upper_bound=upper_bound,
                all_history_upper_bound=all_history_upper_bound,
                observe_only=observe_only,
            )
        except Exception:
            if self._segment_performance:
                self._segment_performance["capture_ms"] += round(
                    (time.monotonic() - started) * 1000
                )
            raise
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if self._segment_performance:
            self._segment_performance["capture_successes"] += 1
            if not upper_probe:
                self._segment_performance["capture_provider_calls"] += 1
            self._segment_performance["capture_ms"] += elapsed_ms
            metrics = getattr(
                self.capture_provider, "last_capture_metrics", None
            )
            if isinstance(metrics, dict):
                for field in (
                    "materialized_cursor_rows",
                    "cursor_discovery_ms",
                ):
                    value = metrics.get(field)
                    if (
                        not isinstance(value, bool)
                        and isinstance(value, int)
                        and value >= 0
                    ):
                        self._segment_performance[field] += value
        return capture, elapsed_ms

    def _load_completed_window_lightweight(
        self, run_id: str
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        """Validate one completed window without reading business artifacts."""

        plan = self.run_store.plan(run_id)
        receipt = self.run_store.plan_receipt(run_id)
        status = self.run_store.status(run_id)
        plan_fingerprint = _plan_fingerprint(plan)
        if (
            _committed_receipt_fingerprint(receipt) != plan_fingerprint
            or status.get("run_id") != run_id
            or status.get("state") != "completed"
            or status.get("failure_category") is not None
            or status.get("checkpoint_published") is not True
        ):
            raise WechatDigestError(
                "微信 Semantic completed window authority 损坏。"
            )
        expected_status_keys = {
            "schema_version",
            "run_id",
            "state",
            "failure_category",
            "checkpoint_published",
            "items",
            "updated_at",
        }
        if plan.get("schema_version") != LEGACY_RUN_PLAN_SCHEMA_VERSION:
            expected_status_keys.add("plan_fingerprint")
            if status.get("plan_fingerprint") != plan_fingerprint:
                raise WechatDigestError(
                    "微信 Semantic completed window plan binding 损坏。"
                )
        elif status.get("plan_fingerprint") is not None:
            raise WechatDigestError(
                "微信 Semantic completed window legacy status 损坏。"
            )
        if (
            set(status) != expected_status_keys
            or status.get("schema_version") != RUN_STATUS_SCHEMA_VERSION
        ):
            raise WechatDigestError(
                "微信 Semantic completed window status 形态损坏。"
            )
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError(
                "微信 Semantic completed window items 损坏。"
            )
        expected_items = {
            **{
                f"conversation:{item['conversation_key']}": (
                    "conversation",
                    item["source_id"],
                )
                for item in _plan_sequence(
                    plan.get("conversations"), "conversations"
                )
            },
            **{
                f"attachment:{item['attachment_key']}": (
                    "attachment",
                    item["source_id"],
                )
                for item in _plan_sequence(
                    plan.get("attachments"), "attachments"
                )
            },
        }
        if set(items) != set(expected_items):
            raise WechatDigestError(
                "微信 Semantic completed window plan/status 不收敛。"
            )
        for item_id, (kind, source_id) in expected_items.items():
            item = items.get(item_id)
            if (
                not isinstance(item, dict)
                or item.get("kind") != kind
                or item.get("source_id") != source_id
                or item.get("state") not in TERMINAL_ITEM_STATES
            ):
                raise WechatDigestError(
                    "微信 Semantic completed window item binding 损坏。"
                )
        if plan.get("schema_version") == SNAPSHOT_RUN_PLAN_SCHEMA_VERSION:
            self.run_store.load_capture_summary_receipt(run_id, plan=plan)
        return plan, receipt, status

    def _semantic_authority_binding(
        self,
        run_id: str,
        *,
        allow_reviewed_head_extension: bool = False,
    ) -> SemanticWindowAuthorityBinding:
        plan = self.run_store.plan(run_id)
        receipt = self.run_store.plan_receipt(run_id)
        after = WechatCursor.from_dict(plan.get("after_cursor"), "plan.after_cursor")
        upper = WechatCursor.from_dict(plan.get("upper_bound"), "plan.upper_bound")
        global_upper = self._plan_all_history_upper(plan) or upper
        port = self._semantic_port()
        campaign = port.global_campaign_binding()
        if campaign is None:
            campaign_created_at = plan.get("created_at")
            campaign_lower = self._cursor_tuple(after)
            campaign_upper = self._cursor_tuple(global_upper)
            campaign_provider_version = plan.get("provider_version")
            campaign_batch_size = self._plan_batch_size(plan)
            campaign_reviewed_head = port.reviewed_git_head
        else:
            campaign_created_at = campaign.created_at
            campaign_lower = campaign.lower_cursor
            campaign_upper = campaign.frozen_global_upper_cursor
            campaign_provider_version = campaign.capture_provider_version
            campaign_batch_size = campaign.semantic_batch_size
            campaign_reviewed_head = campaign.reviewed_git_head
            if (
                self._cursor_tuple(global_upper) != campaign_upper
                or plan.get("provider_version") != campaign_provider_version
                or self._plan_batch_size(plan) != campaign_batch_size
                or (
                    not allow_reviewed_head_extension
                    and port.reviewed_git_head != campaign_reviewed_head
                )
            ):
                raise WechatDigestError(
                    "微信运行计划与 frozen Semantic campaign 漂移。"
                )
        checkpoint_payload = None
        checkpoint_fingerprint = None
        checkpoint = self.run_store.checkpoint()
        if checkpoint is not None:
            checkpoint_payload = self.run_store._read_json(
                self.run_store.checkpoint_path
            )
            checkpoint_fingerprint = _sha256_bytes(
                _canonical_json(checkpoint_payload).encode("utf-8")
            )
        cache_key = (
            campaign_created_at,
            campaign_lower,
            campaign_upper,
            campaign_provider_version,
            campaign_batch_size,
            self._cursor_tuple(after),
            checkpoint_fingerprint,
        )
        completed_windows: list[SemanticCompletedWindowBinding] = []
        if (
            self._completed_window_chain_cache is not None
            and self._completed_window_chain_cache[0] == cache_key
        ):
            completed_windows.extend(
                self._completed_window_chain_cache[1]
            )
            if campaign is None and completed_windows:
                campaign_lower = completed_windows[0].window_after_cursor
        candidates: list[
            tuple[
                WechatCursor,
                WechatCursor,
                str,
                dict[str, object],
                dict[str, object],
                dict[str, object],
            ]
        ] = []
        if not completed_windows and self.run_store.runs_root.exists():
            for path in sorted(self.run_store.runs_root.iterdir()):
                if path.name == run_id:
                    continue
                if not path.is_dir() or re.fullmatch(
                    r"run_[0-9a-f]{32}", path.name
                ) is None:
                    raise WechatDigestError(
                        "微信 Semantic completed window inventory 损坏。"
                    )
                candidate_plan = self.run_store.plan(path.name)
                if candidate_plan.get("created_at") != campaign_created_at:
                    continue
                candidate_global_upper = self._plan_all_history_upper(
                    candidate_plan
                )
                if (
                    candidate_global_upper is None
                    or self._cursor_tuple(candidate_global_upper)
                    != campaign_upper
                    or candidate_plan.get("provider_version")
                    != campaign_provider_version
                    or self._plan_batch_size(candidate_plan)
                    != campaign_batch_size
                ):
                    if campaign is None:
                        continue
                    raise WechatDigestError(
                        "微信 Semantic completed window campaign 漂移。"
                    )
                candidate_after = WechatCursor.from_dict(
                    candidate_plan.get("after_cursor"),
                    "plan.after_cursor",
                )
                candidate_upper = WechatCursor.from_dict(
                    candidate_plan.get("upper_bound"),
                    "plan.upper_bound",
                )
                if candidate_upper > after:
                    continue
                (
                    verified_plan,
                    candidate_receipt,
                    candidate_status,
                ) = self._load_completed_window_lightweight(
                    path.name
                )
                if verified_plan != candidate_plan:
                    raise WechatDigestError(
                        "微信 Semantic completed window plan 读回漂移。"
                    )
                candidates.append(
                    (
                        candidate_after,
                        candidate_upper,
                        path.name,
                        candidate_plan,
                        candidate_receipt,
                        candidate_status,
                    )
                )
        if not completed_windows:
            candidates.sort(key=lambda item: item[0])
            if campaign is None and candidates:
                campaign_lower = self._cursor_tuple(candidates[0][0])
            chain_cursor = WechatCursor(*campaign_lower)
            for (
                candidate_after,
                candidate_upper,
                candidate_run_id,
                candidate_plan,
                candidate_receipt,
                candidate_status,
            ) in candidates:
                if candidate_after != chain_cursor:
                    raise WechatDigestError(
                        "微信 Semantic completed window chain 不连续。"
                    )
                completed_windows.append(
                    SemanticCompletedWindowBinding(
                        window_run_id=candidate_run_id,
                        window_plan_fingerprint=_plan_fingerprint(candidate_plan),
                        window_plan_receipt_fingerprint=_sha256_bytes(
                            _canonical_json(candidate_receipt).encode("utf-8")
                        ),
                        window_status_fingerprint=_sha256_bytes(
                            _canonical_json(candidate_status).encode("utf-8")
                        ),
                        window_after_cursor=self._cursor_tuple(candidate_after),
                        window_upper_cursor=self._cursor_tuple(candidate_upper),
                    )
                )
                chain_cursor = candidate_upper
            self._completed_window_chain_cache = (
                cache_key,
                tuple(completed_windows),
            )
        else:
            chain_cursor = (
                WechatCursor(*completed_windows[-1].window_upper_cursor)
                if completed_windows
                else WechatCursor(*campaign_lower)
            )
        if chain_cursor != after:
            raise WechatDigestError(
                "微信 Semantic completed window chain 不完整。"
            )
        if checkpoint is not None:
            if checkpoint != after:
                raise WechatDigestError(
                    "微信 checkpoint 与当前 Semantic window 不连续。"
                )
            if (
                completed_windows
                and isinstance(checkpoint_payload, dict)
                and checkpoint_payload.get("run_id")
                != completed_windows[-1].window_run_id
            ):
                raise WechatDigestError(
                    "微信 checkpoint 与 completed window chain 不一致。"
                )
            checkpoint_fingerprint = _sha256_bytes(
                _canonical_json(checkpoint_payload).encode("utf-8")
            )
        elif self._cursor_tuple(after) != campaign_lower:
            raise WechatDigestError(
                "微信 Semantic window 缺少 previous checkpoint receipt。"
            )
        if not isinstance(campaign_created_at, str) or not isinstance(
            campaign_provider_version, str
        ):
            raise WechatDigestError("微信运行计划缺少 Semantic authority binding。")
        return SemanticWindowAuthorityBinding(
            campaign_created_at=campaign_created_at,
            campaign_lower_cursor=campaign_lower,
            frozen_global_upper_cursor=campaign_upper,
            capture_provider_version=campaign_provider_version,
            semantic_batch_size=campaign_batch_size,
            window_run_id=run_id,
            window_plan_fingerprint=_plan_fingerprint(plan),
            window_plan_receipt_fingerprint=_sha256_bytes(
                _canonical_json(receipt).encode("utf-8")
            ),
            window_after_cursor=self._cursor_tuple(after),
            window_upper_cursor=self._cursor_tuple(upper),
            previous_checkpoint_fingerprint=checkpoint_fingerprint,
            completed_window_chain=tuple(completed_windows),
            reviewed_git_head=campaign_reviewed_head,
        )

    def _load_active_capture_artifacts(
        self,
        run_id: str,
        plan: dict[str, object],
        status: dict[str, object],
        *,
        allow_pending_unknown_resolution: bool = False,
    ) -> tuple[WechatCapture, dict[str, object]]:
        """Read and validate one active window without invoking its connector."""

        if plan.get("schema_version") != SNAPSHOT_RUN_PLAN_SCHEMA_VERSION:
            raise WechatDigestError(
                "active 微信运行必须先完成 durable capture 升级。"
            )
        started = time.monotonic()
        capture, _ = self.run_store.load_capture_artifacts(run_id, plan=plan)
        index = self.run_store.load_capture_index(run_id, capture=capture)
        if self._segment_performance:
            self._segment_performance["snapshot_readback_ms"] = (
                self._segment_performance.get("snapshot_readback_ms", 0)
                + round((time.monotonic() - started) * 1000)
            )
        self._verify_capture_against_plan(capture, plan)
        self._verify_plan_and_status(
            run_id,
            capture,
            plan,
            status,
            allow_pending_unknown_resolution=allow_pending_unknown_resolution,
        )
        return capture, index

    def install_semantic_authority(
        self,
        *,
        inventory_authority_file: Path,
    ) -> dict[str, object]:
        """Install the one frozen campaign grant without starting a Provider."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError("不存在可绑定 Semantic authority 的 active run。")
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic authority 只能绑定已冻结全局上界的 campaign。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(run_id, plan, status)
            binding = self._semantic_authority_binding(run_id)
            grant = self._semantic_port().install_global_authority(
                inventory_authority_file=inventory_authority_file,
                window_binding=binding,
            )
            if grant.get("global_authority_fingerprint") is None:
                raise WechatDigestError("Semantic authority 安装读回失败。")
            return grant

    def install_semantic_authority_extension(self) -> dict[str, object]:
        """Install the approved append-only cap-1000 extension, zero Provider."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可绑定 Semantic authority extension 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic authority extension 只能绑定已冻结全局上界的 campaign。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(run_id, plan, status)
            items = status.get("items")
            if not isinstance(items, dict) or any(
                not isinstance(item, dict)
                or item.get("state") not in TERMINAL_ITEM_STATES
                for item in items.values()
            ):
                raise WechatDigestError(
                    "Semantic authority extension 要求当前窗口全部 terminal。"
                )
            binding = self._semantic_authority_binding(
                run_id, allow_reviewed_head_extension=True
            )
            extension = self._semantic_port().install_global_authority_extension(
                window_binding=binding
            )
            if extension.get("extension_fingerprint") is None:
                raise WechatDigestError(
                    "Semantic authority extension 安装读回失败。"
                )
            return extension

    def install_semantic_maintenance_continuation(
        self, *, authority_ref: str
    ) -> dict[str, object]:
        """Install the single reviewed-head continuation with zero Providers."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可绑定 Semantic maintenance continuation 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic maintenance continuation 只能绑定 frozen campaign。"
                )
            after = WechatCursor.from_dict(
                plan.get("after_cursor"), "plan.after_cursor"
            )
            checkpoint = self.run_store.checkpoint()
            if checkpoint is not None and checkpoint != after:
                raise WechatDigestError(
                    "Semantic maintenance continuation checkpoint binding 不一致。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(run_id, plan, status)
            processing_pre_state = (
                status.get("state") == "processing"
                and status.get("failure_category") is None
            )
            failed_pre_state = (
                status.get("state") == "failed"
                and status.get("failure_category") == "WechatDigestError"
            )
            if (
                not (processing_pre_state or failed_pre_state)
                or status.get("checkpoint_published") is not False
            ):
                raise WechatDigestError(
                    "Semantic maintenance continuation active run 状态不匹配。"
                )
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            state_counts: dict[str, int] = {}
            semantic_failed_closed = 0
            governance_failed_closed = 0
            for value in items.values():
                if not isinstance(value, dict) or not isinstance(
                    value.get("state"), str
                ):
                    raise WechatDigestError("微信运行状态 items 损坏。")
                state = str(value["state"])
                state_counts[state] = state_counts.get(state, 0) + 1
                if state not in TERMINAL_ITEM_STATES | {"represented", "planned"}:
                    raise WechatDigestError(
                        "Semantic maintenance continuation 存在未收敛 item。"
                    )
                if state == "failed_closed":
                    if value.get("governance_failure") is not None:
                        self._verify_governance_failed_closed_item(value)
                        governance_failed_closed += 1
                    if value.get("semantic_failure") is not None:
                        semantic_failed_closed += 1
            if (
                state_counts.get("represented") != 1
                or state_counts.get("planned") != 3
                or (
                    failed_pre_state
                    and (
                        semantic_failed_closed != 1
                        or governance_failed_closed != 1
                    )
                )
            ):
                raise WechatDigestError(
                    "Semantic maintenance continuation active item 边界不匹配。"
                )
            continuation = self._semantic_port().install_maintenance_continuation(
                window_binding=self._semantic_authority_binding(
                    run_id, allow_reviewed_head_extension=True
                ),
                authority_ref=authority_ref,
            )
            if continuation.get("continuation_fingerprint") is None:
                raise WechatDigestError(
                    "Semantic maintenance continuation 安装读回失败。"
                )
            return continuation

    def install_semantic_reviewed_head_continuation(
        self, *, authority_ref: str
    ) -> dict[str, object]:
        """Install one general reviewed-head continuation without Providers."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可绑定 Semantic reviewed-head continuation 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic reviewed-head continuation 只能绑定 frozen campaign。"
                )
            after = WechatCursor.from_dict(
                plan.get("after_cursor"), "plan.after_cursor"
            )
            checkpoint = self.run_store.checkpoint()
            if checkpoint is not None and checkpoint != after:
                raise WechatDigestError(
                    "Semantic reviewed-head continuation checkpoint binding 不一致。"
                )
            status = self.run_store.status(run_id)
            plan_before = _canonical_json(plan)
            status_before = _canonical_json(status)
            self._load_active_capture_artifacts(run_id, plan, status)
            processing_pre_state = (
                status.get("state") == "processing"
                and status.get("failure_category") is None
            )
            committed_failure_state = (
                status.get("state") == "failed"
                and status.get("failure_category")
                == "SemanticHandoffError"
            )
            governance_startup_failure_state = (
                status.get("state") == "failed"
                and status.get("failure_category") == "RuntimeError"
            )
            if (
                not (
                    processing_pre_state
                    or committed_failure_state
                    or governance_startup_failure_state
                )
                or status.get("checkpoint_published") is not False
            ):
                raise WechatDigestError(
                    "Semantic reviewed-head continuation active run 状态不匹配。"
                )
            if processing_pre_state:
                self._verify_pre_provider_items(
                    plan,
                    status,
                    allow_run_receipt_only=True,
                )
            elif committed_failure_state:
                committed_result_wave = self._inspect_committed_result_wave(
                    run_id,
                    plan,
                    status,
                    allow_reviewed_head_extension=True,
                )
                if committed_result_wave is None:
                    raise WechatDigestError(
                        "Semantic reviewed-head continuation committed-result wave 不完整。"
                    )
            else:
                startup_evidence = (
                    self._build_item_governance_startup_recovery_manifest_unlocked(
                        authority_ref=authority_ref,
                        for_reviewed_head_install=True,
                    )
                )
                if startup_evidence.get("reviewed_git_head") != (
                    self._semantic_authority_binding(
                        run_id,
                        allow_reviewed_head_extension=True,
                    ).reviewed_git_head
                ):
                    raise WechatDigestError(
                        "Semantic reviewed-head continuation Governance authority 漂移。"
                    )
            plan_fingerprint = _plan_fingerprint(plan)
            capture_receipt_fingerprint = plan.get(
                "capture_receipt_fingerprint"
            )
            if (
                status.get("plan_fingerprint") != plan_fingerprint
                or not _sha256_value(capture_receipt_fingerprint)
            ):
                raise WechatDigestError(
                    "Semantic reviewed-head continuation active binding 损坏。"
                )
            active_run_binding = {
                "run_id": run_id,
                "plan_fingerprint": plan_fingerprint,
                "capture_receipt_fingerprint": capture_receipt_fingerprint,
                "status_fingerprint": _sha256_bytes(
                    status_before.encode("utf-8")
                ),
            }
            continuation = (
                self._semantic_port().install_reviewed_head_continuation(
                    window_binding=self._semantic_authority_binding(
                        run_id, allow_reviewed_head_extension=True
                    ),
                    authority_ref=authority_ref,
                    active_run_binding=active_run_binding,
                )
            )
            if (
                continuation.get("activation_unknown_count") != 0
                or continuation.get("activation_last_global_ordinal")
                != continuation.get("activation_total")
                or isinstance(continuation.get("activation_total"), bool)
                or not isinstance(continuation.get("activation_total"), int)
                or continuation.get("next_global_ordinal")
                != continuation["activation_total"] + 1
                or not _sha256_value(
                    continuation.get("continuation_fingerprint")
                )
                or _canonical_json(self.run_store.plan(run_id)) != plan_before
                or _canonical_json(self.run_store.status(run_id))
                != status_before
                or self.run_store.checkpoint() != checkpoint
            ):
                raise WechatDigestError(
                    "Semantic reviewed-head continuation 安装读回失败。"
                )
            return continuation

    def install_semantic_gate_c_continuation(
        self, *, authority_ref: str
    ) -> dict[str, object]:
        """Install the fixed Gate C reviewed-head continuation, zero Provider."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可绑定 Semantic Gate C continuation 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic Gate C continuation 只能绑定 frozen campaign。"
                )
            after = WechatCursor.from_dict(
                plan.get("after_cursor"), "plan.after_cursor"
            )
            checkpoint = self.run_store.checkpoint()
            if checkpoint is not None and checkpoint != after:
                raise WechatDigestError(
                    "Semantic Gate C continuation checkpoint binding 不一致。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(run_id, plan, status)
            if (
                status.get("state") != "processing"
                or status.get("failure_category") is not None
                or status.get("checkpoint_published") is not False
            ):
                raise WechatDigestError(
                    "Semantic Gate C continuation active run 状态不匹配。"
                )
            items = status.get("items")
            if not isinstance(items, dict) or len(items) != 134:
                raise WechatDigestError(
                    "Semantic Gate C continuation active items 不匹配。"
                )
            state_counts: dict[str, int] = {}
            semantic_failed_closed = 0
            governance_failed_closed = 0
            for item in items.values():
                if not isinstance(item, dict) or not isinstance(
                    item.get("state"), str
                ):
                    raise WechatDigestError("微信运行状态 items 损坏。")
                state = str(item["state"])
                state_counts[state] = state_counts.get(state, 0) + 1
                if state == "failed_closed":
                    if item.get("semantic_failure") is not None:
                        semantic_failed_closed += 1
                    if item.get("governance_failure") is not None:
                        self._verify_governance_failed_closed_item(item)
                        governance_failed_closed += 1
            if state_counts != {
                "failed_closed": 2,
                "local_only": 3,
                "pending_human": 17,
                "planned": 4,
                "processed": 7,
                "unsupported": 101,
            } or (semantic_failed_closed, governance_failed_closed) != (1, 1):
                raise WechatDigestError(
                    "Semantic Gate C continuation active item 边界不匹配。"
                )
            continuation = self._semantic_port().install_gate_c_continuation(
                window_binding=self._semantic_authority_binding(
                    run_id, allow_reviewed_head_extension=True
                ),
                authority_ref=authority_ref,
            )
            if (
                continuation.get("activation_total") != 220
                or continuation.get("activation_unknown_count") != 0
                or continuation.get("activation_last_global_ordinal") != 220
                or continuation.get("next_global_ordinal") != 221
                or continuation.get("absolute_cap") != 1000
                or not _sha256_value(
                    continuation.get("continuation_fingerprint")
                )
            ):
                raise WechatDigestError(
                    "Semantic Gate C continuation 安装读回失败。"
                )
            return continuation

    def install_semantic_segmented_gate_c_continuation(
        self, *, authority_ref: str
    ) -> dict[str, object]:
        """Install the fixed Issue #148 short-run continuation, zero Provider."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可绑定 Semantic segmented Gate C continuation 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic segmented Gate C continuation 只能绑定 frozen campaign。"
                )
            after = WechatCursor.from_dict(
                plan.get("after_cursor"), "plan.after_cursor"
            )
            checkpoint = self.run_store.checkpoint()
            if checkpoint is not None and checkpoint != after:
                raise WechatDigestError(
                    "Semantic segmented Gate C continuation checkpoint binding 不一致。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(run_id, plan, status)
            if (
                status.get("state") != "processing"
                or status.get("failure_category") is not None
                or status.get("checkpoint_published") is not False
            ):
                raise WechatDigestError(
                    "Semantic segmented Gate C continuation active run 状态不匹配。"
                )
            items = status.get("items")
            if not isinstance(items, dict) or len(items) != 189:
                raise WechatDigestError(
                    "Semantic segmented Gate C continuation active items 不匹配。"
                )
            state_counts: dict[str, int] = {}
            represented: list[dict[str, object]] = []
            for item in items.values():
                if not isinstance(item, dict) or not isinstance(
                    item.get("state"), str
                ):
                    raise WechatDigestError("微信运行状态 items 损坏。")
                state = str(item["state"])
                state_counts[state] = state_counts.get(state, 0) + 1
                if state == "represented":
                    represented.append(item)
            if state_counts != {
                "local_only": 3,
                "pending_human": 20,
                "planned": 17,
                "processed": 6,
                "represented": 1,
                "unsupported": 142,
            }:
                raise WechatDigestError(
                    "Semantic segmented Gate C continuation active item 边界不匹配。"
                )
            representation_id = represented[0].get("representation_id")
            if (
                not isinstance(representation_id, str)
                or not self.representation_service.verify(
                    representation_id
                ).verified
                or (
                    self.workspace
                    / "02_processing"
                    / "information"
                    / representation_id
                ).exists()
                or represented[0].get("atomic_information_ids") != []
            ):
                raise WechatDigestError(
                    "Semantic segmented Gate C continuation represented item 不匹配。"
                )
            continuation = (
                self._semantic_port().install_segmented_gate_c_continuation(
                    window_binding=self._semantic_authority_binding(
                        run_id, allow_reviewed_head_extension=True
                    ),
                    authority_ref=authority_ref,
                )
            )
            if (
                continuation.get("activation_total") != 297
                or continuation.get("activation_unknown_count") != 0
                or continuation.get("activation_last_global_ordinal") != 297
                or continuation.get("next_global_ordinal") != 298
                or continuation.get("absolute_cap") != 1000
                or not _sha256_value(
                    continuation.get("continuation_fingerprint")
                )
            ):
                raise WechatDigestError(
                    "Semantic segmented Gate C continuation 安装读回失败。"
                )
            return continuation

    def _build_governance_startup_recovery_manifest_unlocked(
        self,
        *,
        authority_ref: str,
        adopted_continuation: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        run_id = self.run_store.active_run_id()
        if run_id is None:
            raise WechatDigestError("不存在可恢复 Governance 启动失败的 active run。")
        plan = self.run_store.plan(run_id)
        plan_receipt = self.run_store.plan_receipt(run_id)
        after = WechatCursor.from_dict(plan.get("after_cursor"), "plan.after_cursor")
        status = self.run_store.status(run_id)
        capture, _ = self._load_active_capture_artifacts(
            run_id, plan, status
        )
        checkpoint = self.run_store.checkpoint()
        if (
            self._plan_all_history_upper(plan) is None
            or checkpoint not in {None, after}
            or status.get("state") != "failed"
            or status.get("failure_category") != "RuntimeError"
            or status.get("checkpoint_published") is not False
        ):
            raise WechatDigestError("Governance 启动恢复 active stop 边界不匹配。")
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        candidates: list[tuple[str, dict[str, object]]] = []
        for item_id, item in items.items():
            if not isinstance(item_id, str) or not isinstance(item, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            receipt_value = item.get("governance_receipt")
            if receipt_value is None:
                continue
            receipt = _validated_governance_receipt(receipt_value)
            if item.get("state") == "represented" and receipt.get("phase") == "started":
                candidates.append((item_id, item))
        if len(candidates) != 1:
            raise WechatDigestError("Governance 启动恢复必须唯一绑定 started item。")
        item_id, item = candidates[0]
        receipt = _validated_governance_receipt(item.get("governance_receipt"))
        metrics = _validated_governance_metrics(item.get("governance_metrics"))
        representation_id = item.get("representation_id")
        source_id = item.get("source_id")
        if (
            not isinstance(representation_id, str)
            or not isinstance(source_id, str)
            or item.get("atomic_information_ids") != []
            or receipt.get("schema_version") != GOVERNANCE_RECEIPT_SCHEMA_VERSION
            or receipt.get("phase") != "started"
            or metrics.get("app_server_start_count") != 1
            or metrics.get("thread_count") != 0
            or metrics.get("turn_count") != 0
            or metrics.get("timeout_count") != 0
            or metrics.get("failure_count") != 1
            or metrics.get("failure_categories") != {"startup": 1}
        ):
            raise WechatDigestError("Governance 启动失败证据不匹配。")
        source = self.source_repository.get(source_id)
        representation = self.representation_repository.get(representation_id)
        if (
            not self.source_repository.verify(source_id).verified
            or not self.representation_repository.verify(representation_id).verified
            or representation.source_id != source_id
        ):
            raise WechatDigestError("Governance 启动恢复 Source/Representation 校验失败。")
        package = self.workspace / "02_processing" / "information" / representation_id
        manifest, _candidates = validate_representation_information_package(package)
        counts = manifest.get("counts")
        if (
            not isinstance(counts, dict)
            or counts.get("atomic_information_candidates") != 4
            or counts.get("residue_items") != 1
        ):
            raise WechatDigestError("Governance 启动恢复 semantic package 数量不匹配。")
        ordered_ids = self._verify_semantic_receipts(
            representation_id, item, recover_missing_item_receipt=True
        )
        if (
            len(ordered_ids) != 4
            or receipt.get("atomic_information_fingerprint")
            != _governance_atomic_fingerprint(ordered_ids)
        ):
            raise WechatDigestError("Governance 启动恢复 Atomic Information 不匹配。")
        artifact_inventory: list[dict[str, object]] = []
        for artifact in representation.artifacts:
            raw = self.representation_repository.read_artifact(
                representation_id, artifact.artifact_id
            )
            if _sha256_bytes(raw) != artifact.content_hash or len(raw) != artifact.size_bytes:
                raise WechatDigestError("Governance 启动恢复 Representation artifact 漂移。")
            artifact_inventory.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                }
            )
        with SQLiteWorldModelRepository(self.database) as repository:
            atomic_effect_bindings = [
                self._governance_effect_binding(repository, atomic_id)
                for atomic_id in ordered_ids
            ]
            if any(
                binding.get("revision_count") != 1
                or binding.get("proposal_history_count") != 0
                or binding.get("journal_count") != 0
                or binding.get("apply_receipt_count") != 0
                or self.information_store.get_current(atomic_id).related_object_ids
                for atomic_id, binding in zip(
                    ordered_ids, atomic_effect_bindings, strict=True
                )
            ):
                raise WechatDigestError("Governance 启动恢复前业务效果已变化。")
            business_tree_fingerprint = self._governance_business_tree_fingerprint(
                repository
            )
        semantic_summary = _validated_global_attempt_summary(
            self._semantic_port().global_attempt_summary(representation_id)
        )
        if semantic_summary != {
            "global_attempt_total": 298,
            "global_unknown": 0,
            "next_global_ordinal": 299,
            "absolute_cap": 1000,
        }:
            raise WechatDigestError("Governance 启动恢复 Semantic ledger 不匹配。")
        window_binding = self._semantic_authority_binding(
            run_id, allow_reviewed_head_extension=True
        )
        if adopted_continuation is not None:
            previous_head = adopted_continuation.get(
                "previous_reviewed_git_head"
            )
            reviewed_head = adopted_continuation.get("reviewed_git_head")
            if (
                previous_head
                != "67d159411e968c6b0c2f787f9063a22682c10fb9"
                or reviewed_head != self._semantic_port().reviewed_git_head
            ):
                raise WechatDigestError(
                    "Governance 启动恢复 continuation head binding 漂移。"
                )
            window_binding = replace(
                window_binding,
                reviewed_git_head=str(previous_head),
            )
        else:
            previous_head = window_binding.reviewed_git_head
            reviewed_head = self._semantic_port().reviewed_git_head
        recovery_binding = {
            "run_id": run_id,
            "plan_fingerprint": _plan_fingerprint(plan),
            "plan_receipt_fingerprint": _sha256_bytes(
                _canonical_json(plan_receipt).encode("utf-8")
            ),
            "status_fingerprint": _sha256_bytes(
                _canonical_json(status).encode("utf-8")
            ),
            "capture_fingerprint": _capture_fingerprint(capture),
            "checkpoint_fingerprint": _sha256_bytes(
                _canonical_json(
                    None if checkpoint is None else checkpoint.to_dict()
                ).encode("utf-8")
            ),
            "item_id": item_id,
            "source_id": source_id,
            "source_manifest_fingerprint": _sha256_bytes(
                _canonical_json(source.to_manifest_dict()).encode("utf-8")
            ),
            "representation_id": representation_id,
            "representation_manifest_fingerprint": _sha256_bytes(
                _canonical_json(representation.to_manifest_dict()).encode("utf-8")
            ),
            "representation_artifact_inventory_fingerprint": _sha256_bytes(
                _canonical_json(artifact_inventory).encode("utf-8")
            ),
            "semantic_package_fingerprint": _package_fingerprint(package),
            "candidate_count": 4,
            "residue_count": 1,
            "ordered_atomic_information_ids": list(ordered_ids),
            "ordered_atomic_revision_fingerprints": [
                _sha256_bytes(
                    _canonical_json(asdict(self.information_store.get_current(atomic_id))).encode("utf-8")
                )
                for atomic_id in ordered_ids
            ],
            "governance_started_receipt_fingerprint": _sha256_bytes(
                _canonical_json(receipt).encode("utf-8")
            ),
            "governance_metrics_fingerprint": _sha256_bytes(
                _canonical_json(metrics).encode("utf-8")
            ),
            "semantic_window_binding_fingerprint": _sha256_bytes(
                _canonical_json(asdict(window_binding)).encode("utf-8")
            ),
        }
        candidate: dict[str, object] = {
            "schema_version": GOVERNANCE_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION,
            "authority_ref": authority_ref,
            "recovery_binding": recovery_binding,
            "atomic_effect_bindings": atomic_effect_bindings,
            "business_tree_fingerprint": business_tree_fingerprint,
            "previous_reviewed_git_head": previous_head,
            "reviewed_git_head": reviewed_head,
            "execution_contract_unchanged": True,
            "semantic_summary": {
                **semantic_summary,
                "last_global_ordinal": 298,
            },
        }
        candidate["manifest_fingerprint"] = _sha256_bytes(
            _canonical_json(candidate).encode("utf-8")
        )
        return _validated_governance_startup_recovery_manifest(candidate)

    def _build_item_governance_startup_recovery_manifest_unlocked(
        self,
        *,
        authority_ref: str,
        allow_existing: bool = False,
        for_reviewed_head_install: bool = False,
    ) -> dict[str, object]:
        run_id = self.run_store.active_run_id()
        if run_id is None:
            raise WechatDigestError("不存在可恢复 Governance 启动失败的 active run。")
        plan = self.run_store.plan(run_id)
        plan_receipt = self.run_store.plan_receipt(run_id)
        after = WechatCursor.from_dict(plan.get("after_cursor"), "plan.after_cursor")
        status = self.run_store.status(run_id)
        capture, _ = self._load_active_capture_artifacts(run_id, plan, status)
        checkpoint = self.run_store.checkpoint()
        if (
            self._plan_all_history_upper(plan) is None
            or checkpoint not in {None, after}
            or status.get("state") != "failed"
            or not isinstance(status.get("failure_category"), str)
            or not status.get("failure_category")
            or status.get("checkpoint_published") is not False
        ):
            raise WechatDigestError("Governance 单项启动恢复 stop 边界不匹配。")
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        candidates: list[tuple[str, dict[str, object]]] = []
        for item_id, item in items.items():
            if not isinstance(item_id, str) or not isinstance(item, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            receipt_value = item.get("governance_receipt")
            if receipt_value is None:
                continue
            receipt = _validated_governance_receipt(receipt_value)
            if item.get("state") == "represented" and receipt.get("phase") == "started":
                candidates.append((item_id, item))
        if len(candidates) != 1:
            raise WechatDigestError("Governance 单项启动恢复必须唯一绑定 started item。")
        item_id, item = candidates[0]
        if for_reviewed_head_install and (
            sum(
                candidate.get("state") == "represented"
                for candidate in items.values()
                if isinstance(candidate, dict)
            )
            != 1
            or any(
                not isinstance(candidate, dict)
                or candidate.get("state")
                not in TERMINAL_ITEM_STATES | {"planned", "represented"}
                for candidate in items.values()
            )
        ):
            raise WechatDigestError(
                "Semantic reviewed-head continuation Governance 当前 item 边界不匹配。"
            )
        receipt = _validated_governance_receipt(item.get("governance_receipt"))
        metrics = _validated_governance_metrics(item.get("governance_metrics"))
        representation_id = item.get("representation_id")
        source_id = item.get("source_id")
        ordered_ids = item.get("atomic_information_ids")
        if (
            not isinstance(representation_id, str)
            or not isinstance(source_id, str)
            or not isinstance(ordered_ids, list) or not ordered_ids
            or len(set(ordered_ids)) != len(ordered_ids)
            or any(not isinstance(value, str) for value in ordered_ids)
            or receipt.get("schema_version") != GOVERNANCE_RECEIPT_SCHEMA_VERSION
            or receipt.get("phase") != "started"
            or receipt.get("atomic_information_fingerprint") != _governance_atomic_fingerprint(ordered_ids)
            or metrics.get("app_server_start_count") != 1
            or metrics.get("thread_count") != 0 or metrics.get("turn_count") != 0
            or metrics.get("timeout_count") != 0 or metrics.get("failure_count") != 1
            or metrics.get("failure_categories") != {"startup": 1}
            or item.get("pending_human") is not False
            or item.get("context_object_ids") != []
        ):
            raise WechatDigestError("Governance 单项启动失败证据不匹配。")
        recoveries, retries = self._governance_startup_history(run_id)
        matching_recoveries = [
            recovery for recovery in recoveries
            if isinstance(recovery.get("recovery_binding"), dict)
            and recovery["recovery_binding"].get("item_id") == item_id
        ]
        matching_fingerprints = {
            str(recovery.get("receipt_fingerprint"))
            for recovery in matching_recoveries
        }
        matching_retries = [
            retry
            for retry in retries
            if retry.get("recovery_receipt_fingerprint")
            in matching_fingerprints
        ]
        if matching_retries or len(matching_recoveries) > (
            1 if allow_existing else 0
        ):
            raise WechatDigestError("Governance 单项启动恢复许可已存在或已消费。")
        source = self.source_repository.get(source_id)
        representation = self.representation_repository.get(representation_id)
        if (
            not self.source_repository.verify(source_id).verified
            or not self.representation_repository.verify(representation_id).verified
            or representation.source_id != source_id
        ):
            raise WechatDigestError("Governance 单项启动恢复 Source/Representation 校验失败。")
        package = self.workspace / "02_processing" / "information" / representation_id
        package_manifest, package_candidates = validate_representation_information_package(package)
        counts = package_manifest.get("counts")
        if not isinstance(counts, dict):
            raise WechatDigestError("Governance 单项启动恢复 semantic package 数量损坏。")
        verified_ids = self._verify_semantic_receipts(representation_id, item)
        if tuple(ordered_ids) != verified_ids or len(package_candidates) != len(ordered_ids):
            raise WechatDigestError("Governance 单项启动恢复 Atomic Information 不匹配。")
        artifact_inventory: list[dict[str, object]] = []
        for artifact in representation.artifacts:
            raw = self.representation_repository.read_artifact(representation_id, artifact.artifact_id)
            if _sha256_bytes(raw) != artifact.content_hash or len(raw) != artifact.size_bytes:
                raise WechatDigestError("Governance 单项启动恢复 Representation artifact 漂移。")
            artifact_inventory.append({
                "artifact_id": artifact.artifact_id,
                "content_hash": artifact.content_hash,
                "size_bytes": artifact.size_bytes,
            })
        with SQLiteWorldModelRepository(self.database) as repository:
            atomic_effect_bindings = [
                self._governance_effect_binding(repository, atomic_id)
                for atomic_id in ordered_ids
            ]
            if any(
                binding.get("revision_count") != 1
                or binding.get("proposal_history_count") != 0
                or binding.get("journal_count") != 0
                or binding.get("apply_receipt_count") != 0
                or self.information_store.get_current(atomic_id).related_object_ids
                for atomic_id, binding in zip(ordered_ids, atomic_effect_bindings, strict=True)
            ):
                raise WechatDigestError("Governance 单项启动恢复前业务效果已变化。")
            business_tree_fingerprint = self._governance_business_tree_fingerprint(repository)
        semantic_snapshot = self._semantic_port().governance_startup_recovery_snapshot(representation_id)
        window_binding = self._semantic_authority_binding(run_id, allow_reviewed_head_extension=True)
        reviewed_head = semantic_snapshot.get("reviewed_git_head")
        target_reviewed_head = self._semantic_port().reviewed_git_head
        if (
            window_binding.reviewed_git_head != reviewed_head
            or for_reviewed_head_install
            and (
                re.fullmatch(r"[0-9a-f]{40}", target_reviewed_head) is None
                or reviewed_head == target_reviewed_head
            )
            or not for_reviewed_head_install
            and reviewed_head != target_reviewed_head
        ):
            raise WechatDigestError("Governance 单项启动恢复 reviewed head 漂移。")
        recovery_binding = {
            "run_id": run_id,
            "plan_fingerprint": _plan_fingerprint(plan),
            "plan_receipt_fingerprint": _sha256_bytes(_canonical_json(plan_receipt).encode("utf-8")),
            "status_fingerprint": _sha256_bytes(_canonical_json(status).encode("utf-8")),
            "capture_fingerprint": _capture_fingerprint(capture),
            "checkpoint_fingerprint": _sha256_bytes(_canonical_json(None if checkpoint is None else checkpoint.to_dict()).encode("utf-8")),
            "item_id": item_id,
            "source_id": source_id,
            "source_manifest_fingerprint": _sha256_bytes(_canonical_json(source.to_manifest_dict()).encode("utf-8")),
            "representation_id": representation_id,
            "representation_manifest_fingerprint": _sha256_bytes(_canonical_json(representation.to_manifest_dict()).encode("utf-8")),
            "representation_artifact_inventory_fingerprint": _sha256_bytes(_canonical_json(artifact_inventory).encode("utf-8")),
            "semantic_package_fingerprint": _package_fingerprint(package),
            "candidate_count": len(ordered_ids),
            "residue_count": int(counts.get("residue_items", 0)),
            "ordered_atomic_information_ids": list(ordered_ids),
            "ordered_atomic_revision_fingerprints": [
                _sha256_bytes(_canonical_json(asdict(self.information_store.get_current(atomic_id))).encode("utf-8"))
                for atomic_id in ordered_ids
            ],
            "governance_started_receipt_fingerprint": _sha256_bytes(_canonical_json(receipt).encode("utf-8")),
            "governance_metrics_fingerprint": _sha256_bytes(_canonical_json(metrics).encode("utf-8")),
            "semantic_window_binding_fingerprint": _sha256_bytes(_canonical_json(asdict(window_binding)).encode("utf-8")),
            "semantic_snapshot_fingerprint": _sha256_bytes(_canonical_json(semantic_snapshot).encode("utf-8")),
        }
        candidate: dict[str, object] = {
            "schema_version": GOVERNANCE_ITEM_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION,
            "authority_ref": authority_ref,
            "recovery_binding": recovery_binding,
            "atomic_effect_bindings": atomic_effect_bindings,
            "business_tree_fingerprint": business_tree_fingerprint,
            "reviewed_git_head": reviewed_head,
            "execution_contract_unchanged": True,
            "semantic_snapshot": semantic_snapshot,
        }
        candidate["manifest_fingerprint"] = _sha256_bytes(_canonical_json(candidate).encode("utf-8"))
        return _validated_item_governance_startup_recovery_manifest(candidate)

    def build_governance_startup_recovery_manifest(
        self, *, authority_ref: str
    ) -> dict[str, object]:
        """Build a read-only startup recovery candidate for Lead approval."""

        with self.run_store.lock():
            if not authority_ref.startswith("https://github.com/leevi2010-cursor/ArcheOS/issues/150"):
                return self._build_item_governance_startup_recovery_manifest_unlocked(authority_ref=authority_ref)
            return self._build_governance_startup_recovery_manifest_unlocked(
                authority_ref=authority_ref
            )

    def resolve_governance_startup_failure(
        self, *, authority_ref: str, authority_manifest_file: Path
    ) -> dict[str, object]:
        """Install one exact startup recovery permission without Providers."""

        manifest, raw_fingerprint = _read_private_json_manifest(
            authority_manifest_file
        )
        if manifest.get("schema_version") == GOVERNANCE_ITEM_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION:
            generic = _validated_item_governance_startup_recovery_manifest(manifest)
            if generic.get("authority_ref") != authority_ref:
                raise WechatDigestError("Governance 单项启动恢复 authority ref 不匹配。")
            with self.run_store.lock():
                binding = generic["recovery_binding"]
                assert isinstance(binding, dict)
                run_id = str(binding["run_id"])
                if self.run_store.active_run_id() != run_id:
                    raise WechatDigestError("Governance 单项启动恢复 active run 不匹配。")
                expected = self._build_item_governance_startup_recovery_manifest_unlocked(
                    authority_ref=authority_ref,
                    allow_existing=True,
                )
                if expected != generic:
                    raise WechatDigestError("Governance 单项启动恢复 manifest 与现场不匹配。")
                matching = [
                    receipt
                    for receipt in self.run_store.governance_startup_recoveries(run_id)
                    if receipt.get("authority_manifest_fingerprint") == generic.get("manifest_fingerprint")
                    and receipt.get("authority_manifest_raw_fingerprint") == raw_fingerprint
                ]
                if len(matching) > 1:
                    raise WechatDigestError("Governance 单项启动恢复 receipt 重复。")
                if matching:
                    return matching[0]
                semantic = generic["semantic_snapshot"]
                assert isinstance(semantic, dict)
                without_fingerprint: dict[str, object] = {
                    "schema_version": GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION,
                    "artifact_kind": "governance_startup_recovery",
                    "authority_ref": authority_ref,
                    "authority_manifest_fingerprint": generic["manifest_fingerprint"],
                    "authority_manifest_raw_fingerprint": raw_fingerprint,
                    "recovery_binding": binding,
                    "atomic_effect_bindings": generic["atomic_effect_bindings"],
                    "business_tree_fingerprint": generic["business_tree_fingerprint"],
                    "semantic_continuation_fingerprint": semantic["global_authority_fingerprint"],
                    "provider_retry_permitted": True,
                    "max_retry_attempts": 1,
                    "retry_consumed": False,
                }
                receipt = {
                    **without_fingerprint,
                    "receipt_fingerprint": _sha256_bytes(_canonical_json(without_fingerprint).encode("utf-8")),
                }
                observed = self.run_store.publish_item_scoped_governance_startup_receipt(run_id, receipt=receipt)
                if observed != receipt:
                    raise WechatDigestError("Governance 单项启动恢复 receipt 读回失败。")
                return observed
        manifest = _validated_governance_startup_recovery_manifest(manifest)
        if manifest.get("authority_ref") != authority_ref:
            raise WechatDigestError("Governance 启动恢复 authority ref 不匹配。")
        with self.run_store.lock():
            binding = manifest["recovery_binding"]
            assert isinstance(binding, dict)
            run_id = str(binding["run_id"])
            if self.run_store.active_run_id() != run_id:
                raise WechatDigestError("Governance 启动恢复 active run 不匹配。")
            existing = self.run_store.governance_startup_recovery(run_id)
            if existing is None:
                semantic_continuation = (
                    self._semantic_port()
                    .governance_startup_recovery_continuation(
                        authority_ref=authority_ref,
                        authority_manifest_fingerprint=str(
                            manifest["manifest_fingerprint"]
                        ),
                        authority_manifest_raw_fingerprint=raw_fingerprint,
                    )
                )
                expected = self._build_governance_startup_recovery_manifest_unlocked(
                    authority_ref=authority_ref,
                    adopted_continuation=semantic_continuation,
                )
                if expected != manifest:
                    raise WechatDigestError("Governance 启动恢复 manifest 与现场不匹配。")
                if semantic_continuation is None:
                    semantic_continuation = (
                        self._semantic_port()
                        .install_governance_startup_recovery_continuation(
                            window_binding=self._semantic_authority_binding(
                                run_id, allow_reviewed_head_extension=True
                            ),
                            authority_ref=authority_ref,
                            authority_manifest_fingerprint=str(
                                manifest["manifest_fingerprint"]
                            ),
                            authority_manifest_raw_fingerprint=raw_fingerprint,
                        )
                    )
                receipt_without_fingerprint: dict[str, object] = {
                    "schema_version": GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION,
                    "artifact_kind": "governance_startup_recovery",
                    "authority_ref": authority_ref,
                    "authority_manifest_fingerprint": manifest[
                        "manifest_fingerprint"
                    ],
                    "authority_manifest_raw_fingerprint": raw_fingerprint,
                    "recovery_binding": binding,
                    "atomic_effect_bindings": manifest[
                        "atomic_effect_bindings"
                    ],
                    "business_tree_fingerprint": manifest[
                        "business_tree_fingerprint"
                    ],
                    "semantic_continuation_fingerprint": semantic_continuation[
                        "continuation_fingerprint"
                    ],
                    "provider_retry_permitted": True,
                    "max_retry_attempts": 1,
                    "retry_consumed": False,
                }
                receipt = {
                    **receipt_without_fingerprint,
                    "receipt_fingerprint": _sha256_bytes(
                        _canonical_json(receipt_without_fingerprint).encode("utf-8")
                    ),
                }
                existing = self.run_store.publish_governance_startup_receipt(
                    run_id,
                    filename="governance-startup-recovery.json",
                    receipt=receipt,
                )
            existing_projected = dict(existing)
            existing_fingerprint = existing_projected.pop(
                "receipt_fingerprint", None
            )
            if (
                set(existing)
                != {
                    "schema_version",
                    "artifact_kind",
                    "authority_ref",
                    "authority_manifest_fingerprint",
                    "authority_manifest_raw_fingerprint",
                    "recovery_binding",
                    "atomic_effect_bindings",
                    "business_tree_fingerprint",
                    "semantic_continuation_fingerprint",
                    "provider_retry_permitted",
                    "max_retry_attempts",
                    "retry_consumed",
                    "receipt_fingerprint",
                }
                or existing.get("schema_version")
                != GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION
                or existing.get("artifact_kind")
                != "governance_startup_recovery"
                or existing.get("authority_ref") != authority_ref
                or existing.get("authority_manifest_fingerprint")
                != manifest.get("manifest_fingerprint")
                or existing.get("authority_manifest_raw_fingerprint")
                != raw_fingerprint
                or existing.get("recovery_binding") != binding
                or existing.get("atomic_effect_bindings")
                != manifest.get("atomic_effect_bindings")
                or existing.get("business_tree_fingerprint")
                != manifest.get("business_tree_fingerprint")
                or not _sha256_value(
                    existing.get("semantic_continuation_fingerprint")
                )
                or existing.get("provider_retry_permitted") is not True
                or existing.get("max_retry_attempts") != 1
                or existing.get("retry_consumed") is not False
                or not _sha256_value(existing_fingerprint)
                or existing_fingerprint
                != _sha256_bytes(
                    _canonical_json(existing_projected).encode("utf-8")
                )
            ):
                raise WechatDigestError("Governance 启动恢复 receipt 不匹配。")
            status = self.run_store.status(run_id)
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            item_id = str(binding["item_id"])
            item = self._item(items, item_id)
            ordered_ids = list(binding["ordered_atomic_information_ids"])
            plan = self.run_store.plan(run_id)
            capture, _ = self._load_active_capture_artifacts(
                run_id, plan, status
            )
            source_id = str(binding["source_id"])
            representation_id = str(binding["representation_id"])
            source = self.source_repository.get(source_id)
            representation = self.representation_repository.get(
                representation_id
            )
            artifact_inventory = [
                {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in representation.artifacts
            ]
            package = (
                self.workspace
                / "02_processing"
                / "information"
                / representation_id
            )
            item_receipt = _validated_governance_receipt(
                item.get("governance_receipt")
            )
            item_metrics = _validated_governance_metrics(
                item.get("governance_metrics")
            )
            current_checkpoint = self.run_store.checkpoint()
            if (
                item.get("state") != "represented"
                or item.get("source_id") != source_id
                or item.get("representation_id") != representation_id
                or item_receipt.get("phase") != "started"
                or _sha256_bytes(
                    _canonical_json(item_receipt).encode("utf-8")
                )
                != binding.get("governance_started_receipt_fingerprint")
                or _sha256_bytes(
                    _canonical_json(item_metrics).encode("utf-8")
                )
                != binding.get("governance_metrics_fingerprint")
                or _plan_fingerprint(plan) != binding.get("plan_fingerprint")
                or _sha256_bytes(
                    _canonical_json(self.run_store.plan_receipt(run_id)).encode(
                        "utf-8"
                    )
                )
                != binding.get("plan_receipt_fingerprint")
                or _capture_fingerprint(capture)
                != binding.get("capture_fingerprint")
                or _sha256_bytes(
                    _canonical_json(
                        None
                        if current_checkpoint is None
                        else current_checkpoint.to_dict()
                    ).encode("utf-8")
                )
                != binding.get("checkpoint_fingerprint")
                or _sha256_bytes(
                    _canonical_json(source.to_manifest_dict()).encode("utf-8")
                )
                != binding.get("source_manifest_fingerprint")
                or not self.source_repository.verify(source_id).verified
                or representation.source_id != source_id
                or not self.representation_repository.verify(
                    representation_id
                ).verified
                or _sha256_bytes(
                    _canonical_json(representation.to_manifest_dict()).encode(
                        "utf-8"
                    )
                )
                != binding.get("representation_manifest_fingerprint")
                or _sha256_bytes(
                    _canonical_json(artifact_inventory).encode("utf-8")
                )
                != binding.get(
                    "representation_artifact_inventory_fingerprint"
                )
                or _package_fingerprint(package)
                != binding.get("semantic_package_fingerprint")
                or [
                    _sha256_bytes(
                        _canonical_json(
                            asdict(
                                self.information_store.get_current(atomic_id)
                            )
                        ).encode("utf-8")
                    )
                    for atomic_id in ordered_ids
                ]
                != binding.get("ordered_atomic_revision_fingerprints")
                or status.get("state") not in {"failed", "processing"}
                or status.get("state") == "failed"
                and _sha256_bytes(
                    _canonical_json(status).encode("utf-8")
                )
                != binding.get("status_fingerprint")
                or status.get("state") == "processing"
                and (
                    status.get("failure_category") is not None
                    or item.get("atomic_information_ids") != ordered_ids
                )
            ):
                raise WechatDigestError(
                    "Governance 启动恢复 durable binding 漂移。"
                )
            with SQLiteWorldModelRepository(self.database) as repository:
                current_effect_bindings = [
                    self._governance_effect_binding(repository, atomic_id)
                    for atomic_id in ordered_ids
                ]
                if (
                    current_effect_bindings
                    != manifest.get("atomic_effect_bindings")
                    or self._governance_business_tree_fingerprint(repository)
                    != manifest.get("business_tree_fingerprint")
                ):
                    raise WechatDigestError(
                        "Governance 启动恢复业务状态漂移。"
                    )
            if status.get("state") == "failed":
                updated_items = dict(items)
                updated_items[item_id] = {
                    **item,
                    "atomic_information_ids": ordered_ids,
                }
                status["items"] = updated_items
                status["state"] = "processing"
                status["failure_category"] = None
                status["updated_at"] = self.clock()
                self.run_store.update_status(run_id, status)
            observed = self.run_store.status(run_id)
            observed_items = observed.get("items")
            if (
                observed.get("state") != "processing"
                or observed.get("failure_category") is not None
                or observed.get("checkpoint_published") is not False
                or not isinstance(observed_items, dict)
                or self._item(observed_items, item_id).get(
                    "atomic_information_ids"
                )
                != ordered_ids
            ):
                raise WechatDigestError("Governance 启动恢复状态读回失败。")
            return existing

    def _build_multi_governance_startup_recovery_manifest_unlocked(
        self,
        *,
        authority_ref: str,
        adopted_continuation: Mapping[str, object] | None = None,
        replay_capture: bool = True,
    ) -> dict[str, object]:
        run_id = self.run_store.active_run_id()
        if run_id is None:
            raise WechatDigestError("不存在可恢复 Governance 启动失败的 active run。")
        plan = self.run_store.plan(run_id)
        plan_receipt = self.run_store.plan_receipt(run_id)
        after = WechatCursor.from_dict(plan.get("after_cursor"), "plan.after_cursor")
        status = self.run_store.status(run_id)
        capture_fingerprint = plan.get("capture_fingerprint")
        if not _sha256_value(capture_fingerprint):
            raise WechatDigestError("多项目 Governance capture binding 损坏。")
        if replay_capture:
            capture, _ = self._load_active_capture_artifacts(
                run_id, plan, status
            )
            capture_fingerprint = _capture_fingerprint(capture)
        else:
            self._verify_plan_and_status(run_id, None, plan, status)
        checkpoint = self.run_store.checkpoint()
        if (
            self._plan_all_history_upper(plan) is None
            or checkpoint not in {None, after}
            or status.get("state") != "failed"
            or status.get("failure_category") != "WechatDigestError"
            or status.get("checkpoint_published") is not False
        ):
            raise WechatDigestError("多项目 Governance 启动恢复 stop 边界不匹配。")
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        candidates: list[tuple[str, dict[str, object]]] = []
        for item_id, item in items.items():
            if not isinstance(item_id, str) or not isinstance(item, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            receipt_value = item.get("governance_receipt")
            if receipt_value is None:
                continue
            receipt = _validated_governance_receipt(receipt_value)
            if item.get("state") == "represented" and receipt.get("phase") == "started":
                candidates.append((item_id, item))
        if len(candidates) != 1:
            raise WechatDigestError("多项目 Governance 启动恢复必须唯一绑定 started item。")
        item_id, item = candidates[0]
        receipt = _validated_governance_receipt(item.get("governance_receipt"))
        metrics = _validated_governance_metrics(item.get("governance_metrics"))
        source_id = item.get("source_id")
        representation_id = item.get("representation_id")
        ordered_ids = item.get("atomic_information_ids")
        if (
            not isinstance(source_id, str)
            or not isinstance(representation_id, str)
            or not isinstance(ordered_ids, list)
            or len(ordered_ids) != 3
            or len(set(ordered_ids)) != 3
            or any(not isinstance(value, str) for value in ordered_ids)
            or receipt.get("schema_version") != GOVERNANCE_RECEIPT_SCHEMA_VERSION
            or receipt.get("phase") != "started"
            or receipt.get("atomic_information_fingerprint")
            != _governance_atomic_fingerprint(ordered_ids)
            or metrics.get("app_server_start_count") != 1
            or metrics.get("thread_count") != 0
            or metrics.get("turn_count") != 0
            or metrics.get("timeout_count") != 0
            or metrics.get("failure_count") != 1
            or metrics.get("failure_categories") != {"startup": 1}
        ):
            raise WechatDigestError("多项目 Governance 启动失败证据不匹配。")
        source = self.source_repository.get(source_id)
        representation = self.representation_repository.get(representation_id)
        if (
            not self.source_repository.verify(source_id).verified
            or not self.representation_repository.verify(representation_id).verified
            or representation.source_id != source_id
        ):
            raise WechatDigestError("多项目 Governance Source/Representation 校验失败。")
        package = self.workspace / "02_processing" / "information" / representation_id
        package_manifest, _candidates = validate_representation_information_package(
            package
        )
        counts = package_manifest.get("counts")
        if (
            not isinstance(counts, dict)
            or counts.get("atomic_information_candidates") != 3
            or isinstance(counts.get("residue_items"), bool)
            or not isinstance(counts.get("residue_items"), int)
            or counts["residue_items"] < 0
        ):
            raise WechatDigestError("多项目 Governance semantic package 数量不匹配。")
        verified_ids = self._verify_semantic_receipts(representation_id, item)
        if verified_ids != tuple(ordered_ids):
            raise WechatDigestError("多项目 Governance Atomic Information 不匹配。")
        artifact_inventory: list[dict[str, object]] = []
        for artifact in representation.artifacts:
            raw = self.representation_repository.read_artifact(
                representation_id, artifact.artifact_id
            )
            if _sha256_bytes(raw) != artifact.content_hash or len(raw) != artifact.size_bytes:
                raise WechatDigestError("多项目 Governance Representation artifact 漂移。")
            artifact_inventory.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                }
            )
        with SQLiteWorldModelRepository(self.database) as repository:
            atomic_effect_bindings = [
                self._governance_effect_binding(repository, atomic_id)
                for atomic_id in ordered_ids
            ]
            if any(
                binding.get("revision_count") != 1
                or binding.get("proposal_history_count") != 0
                or binding.get("journal_count") != 0
                or binding.get("apply_receipt_count") != 0
                or self.information_store.get_current(atomic_id).related_object_ids
                for atomic_id, binding in zip(
                    ordered_ids, atomic_effect_bindings, strict=True
                )
            ):
                raise WechatDigestError("多项目 Governance 恢复前业务效果已变化。")
            business_tree_fingerprint = self._governance_business_tree_fingerprint(
                repository
            )
        recoveries, retries = self._governance_startup_history(run_id)
        if not any(
            str(value.get("authority_ref", "")).startswith(
                "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            )
            for value in recoveries
        ) or not retries:
            raise WechatDigestError("历史 Governance 启动恢复证据缺失。")
        if self._startup_recovery_for_item(
            run_id=run_id,
            item_id=item_id,
            atomic_ids=ordered_ids,
            started_receipt=receipt,
        ) is not None:
            raise WechatDigestError("当前 Governance 启动恢复 receipt 已存在。")
        semantic_summary = _validated_global_attempt_summary(
            self._semantic_port().global_attempt_summary(representation_id)
        )
        if semantic_summary != {
            "global_attempt_total": 302,
            "global_unknown": 0,
            "next_global_ordinal": 303,
            "absolute_cap": 1000,
        }:
            raise WechatDigestError("多项目 Governance Semantic ledger 不匹配。")
        window_binding = self._semantic_authority_binding(
            run_id,
            allow_reviewed_head_extension=True,
        )
        if adopted_continuation is not None:
            previous_head = adopted_continuation.get("previous_reviewed_git_head")
            reviewed_head = adopted_continuation.get("reviewed_git_head")
            if (
                previous_head != "ce49d89355caab38da08b4522f416d248c60646b"
                or reviewed_head != self._semantic_port().reviewed_git_head
            ):
                raise WechatDigestError("多项目 Governance continuation head binding 漂移。")
            window_binding = replace(
                window_binding,
                reviewed_git_head=str(previous_head),
            )
        else:
            previous_head = window_binding.reviewed_git_head
            reviewed_head = self._semantic_port().reviewed_git_head
        recovery_binding = {
            "run_id": run_id,
            "plan_fingerprint": _plan_fingerprint(plan),
            "plan_receipt_fingerprint": _sha256_bytes(
                _canonical_json(plan_receipt).encode("utf-8")
            ),
            "status_fingerprint": _sha256_bytes(
                _canonical_json(status).encode("utf-8")
            ),
            "capture_fingerprint": capture_fingerprint,
            "checkpoint_fingerprint": _sha256_bytes(
                _canonical_json(
                    None if checkpoint is None else checkpoint.to_dict()
                ).encode("utf-8")
            ),
            "item_id": item_id,
            "source_id": source_id,
            "source_manifest_fingerprint": _sha256_bytes(
                _canonical_json(source.to_manifest_dict()).encode("utf-8")
            ),
            "representation_id": representation_id,
            "representation_manifest_fingerprint": _sha256_bytes(
                _canonical_json(representation.to_manifest_dict()).encode("utf-8")
            ),
            "representation_artifact_inventory_fingerprint": _sha256_bytes(
                _canonical_json(artifact_inventory).encode("utf-8")
            ),
            "semantic_package_fingerprint": _package_fingerprint(package),
            "candidate_count": 3,
            "residue_count": counts["residue_items"],
            "ordered_atomic_information_ids": list(ordered_ids),
            "ordered_atomic_revision_fingerprints": [
                _sha256_bytes(
                    _canonical_json(
                        asdict(self.information_store.get_current(atomic_id))
                    ).encode("utf-8")
                )
                for atomic_id in ordered_ids
            ],
            "governance_started_receipt_fingerprint": _sha256_bytes(
                _canonical_json(receipt).encode("utf-8")
            ),
            "governance_metrics_fingerprint": _sha256_bytes(
                _canonical_json(metrics).encode("utf-8")
            ),
            "semantic_window_binding_fingerprint": _sha256_bytes(
                _canonical_json(asdict(window_binding)).encode("utf-8")
            ),
            "startup_recovery_inventory_fingerprint": _sha256_bytes(
                _canonical_json(recoveries).encode("utf-8")
            ),
            "startup_retry_inventory_fingerprint": _sha256_bytes(
                _canonical_json(retries).encode("utf-8")
            ),
        }
        candidate: dict[str, object] = {
            "schema_version": (
                GOVERNANCE_MULTI_STARTUP_RECOVERY_MANIFEST_SCHEMA_VERSION
            ),
            "authority_ref": authority_ref,
            "recovery_binding": recovery_binding,
            "atomic_effect_bindings": atomic_effect_bindings,
            "business_tree_fingerprint": business_tree_fingerprint,
            "previous_reviewed_git_head": previous_head,
            "reviewed_git_head": reviewed_head,
            "execution_contract_unchanged": True,
            "semantic_summary": {
                **semantic_summary,
                "last_global_ordinal": 302,
            },
        }
        candidate["manifest_fingerprint"] = _sha256_bytes(
            _canonical_json(candidate).encode("utf-8")
        )
        return _validated_multi_governance_startup_recovery_manifest(candidate)

    def build_multi_governance_startup_recovery_manifest(
        self, *, authority_ref: str
    ) -> dict[str, object]:
        """Build the read-only Issue #168 recovery authority candidate."""

        with self.run_store.lock():
            return self._build_multi_governance_startup_recovery_manifest_unlocked(
                authority_ref=authority_ref
            )

    def resolve_multi_governance_startup_failure(
        self,
        *,
        authority_ref: str,
        authority_manifest_file: Path,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        """Install one item-scoped Issue #168 recovery without Providers."""

        manifest, raw_fingerprint = _read_private_json_manifest(
            authority_manifest_file
        )
        manifest = _validated_multi_governance_startup_recovery_manifest(
            manifest
        )
        if manifest.get("authority_ref") != authority_ref:
            raise WechatDigestError("多项目 Governance authority ref 不匹配。")
        if progress is not None:
            progress("capture_skipped")
            progress("verify")
        with self.run_store.lock():
            binding = manifest["recovery_binding"]
            assert isinstance(binding, dict)
            run_id = str(binding["run_id"])
            item_id = str(binding["item_id"])
            ordered_ids = list(binding["ordered_atomic_information_ids"])
            if self.run_store.active_run_id() != run_id:
                raise WechatDigestError("多项目 Governance active run 不匹配。")
            status = self.run_store.status(run_id)
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            item = self._item(items, item_id)
            started_receipt = _validated_governance_receipt(
                item.get("governance_receipt")
            )
            recoveries, retries = self._governance_startup_history(run_id)
            existing_matches = [
                value
                for value in recoveries
                if isinstance(value.get("recovery_binding"), dict)
                and value["recovery_binding"].get("run_id") == run_id
                and value["recovery_binding"].get("item_id") == item_id
                and value["recovery_binding"].get(
                    "ordered_atomic_information_ids"
                )
                == ordered_ids
                and value["recovery_binding"].get(
                    "governance_started_receipt_fingerprint"
                )
                == _sha256_bytes(
                    _canonical_json(started_receipt).encode("utf-8")
                )
            ]
            if len(existing_matches) > 1:
                raise WechatDigestError("多项目 Governance recovery 重复匹配。")
            existing = existing_matches[0] if existing_matches else None
            if existing is None:
                semantic_continuation = (
                    self._semantic_port()
                    .multi_governance_startup_recovery_continuation(
                        authority_ref=authority_ref,
                        authority_manifest_fingerprint=str(
                            manifest["manifest_fingerprint"]
                        ),
                        authority_manifest_raw_fingerprint=raw_fingerprint,
                    )
                )
                expected = (
                    self._build_multi_governance_startup_recovery_manifest_unlocked(
                        authority_ref=authority_ref,
                        adopted_continuation=semantic_continuation,
                        replay_capture=False,
                    )
                )
                if expected != manifest:
                    raise WechatDigestError("多项目 Governance manifest 与现场不匹配。")
                if progress is not None:
                    progress("write")
                if semantic_continuation is None:
                    semantic_continuation = (
                        self._semantic_port()
                        .install_multi_governance_startup_recovery_continuation(
                            window_binding=self._semantic_authority_binding(
                                run_id,
                                allow_reviewed_head_extension=True,
                            ),
                            authority_ref=authority_ref,
                            authority_manifest_fingerprint=str(
                                manifest["manifest_fingerprint"]
                            ),
                            authority_manifest_raw_fingerprint=raw_fingerprint,
                        )
                    )
                without_fingerprint: dict[str, object] = {
                    "schema_version": GOVERNANCE_STARTUP_RECOVERY_SCHEMA_VERSION,
                    "artifact_kind": "governance_startup_recovery",
                    "authority_ref": authority_ref,
                    "authority_manifest_fingerprint": manifest[
                        "manifest_fingerprint"
                    ],
                    "authority_manifest_raw_fingerprint": raw_fingerprint,
                    "recovery_binding": binding,
                    "atomic_effect_bindings": manifest["atomic_effect_bindings"],
                    "business_tree_fingerprint": manifest[
                        "business_tree_fingerprint"
                    ],
                    "semantic_continuation_fingerprint": semantic_continuation[
                        "continuation_fingerprint"
                    ],
                    "provider_retry_permitted": True,
                    "max_retry_attempts": 1,
                    "retry_consumed": False,
                }
                receipt = {
                    **without_fingerprint,
                    "receipt_fingerprint": _sha256_bytes(
                        _canonical_json(without_fingerprint).encode("utf-8")
                    ),
                }
                existing = (
                    self.run_store.publish_item_scoped_governance_startup_receipt(
                        run_id,
                        receipt=receipt,
                    )
                )
            elif progress is not None:
                progress("write_skipped")
            if progress is not None:
                progress("readback")
            existing = _validated_governance_startup_recovery_receipt(existing)
            if (
                existing.get("authority_ref") != authority_ref
                or existing.get("authority_manifest_fingerprint")
                != manifest.get("manifest_fingerprint")
                or existing.get("authority_manifest_raw_fingerprint")
                != raw_fingerprint
                or existing.get("recovery_binding") != binding
                or existing.get("atomic_effect_bindings")
                != manifest.get("atomic_effect_bindings")
                or existing.get("business_tree_fingerprint")
                != manifest.get("business_tree_fingerprint")
            ):
                raise WechatDigestError("多项目 Governance recovery receipt 不匹配。")
            observed_continuation = (
                self._semantic_port()
                .multi_governance_startup_recovery_continuation(
                    authority_ref=authority_ref,
                    authority_manifest_fingerprint=str(
                        manifest["manifest_fingerprint"]
                    ),
                    authority_manifest_raw_fingerprint=raw_fingerprint,
                )
            )
            if (
                observed_continuation is None
                or observed_continuation.get("continuation_fingerprint")
                != existing.get("semantic_continuation_fingerprint")
            ):
                raise WechatDigestError("多项目 Governance continuation 读回失败。")
            historical_recoveries, observed_retries = (
                self._governance_startup_history(run_id)
            )
            historical_recoveries = tuple(
                value
                for value in historical_recoveries
                if value.get("receipt_fingerprint")
                != existing.get("receipt_fingerprint")
            )
            if (
                _sha256_bytes(
                    _canonical_json(historical_recoveries).encode("utf-8")
                )
                != binding.get("startup_recovery_inventory_fingerprint")
                or _sha256_bytes(
                    _canonical_json(observed_retries).encode("utf-8")
                )
                != binding.get("startup_retry_inventory_fingerprint")
                or any(
                    retry.get("recovery_receipt_fingerprint")
                    == existing.get("receipt_fingerprint")
                    for retry in observed_retries
                )
            ):
                raise WechatDigestError("多项目 Governance 历史 receipt 漂移。")
            plan = self.run_store.plan(run_id)
            self._verify_plan_and_status(run_id, None, plan, status)
            source_id = str(binding["source_id"])
            representation_id = str(binding["representation_id"])
            source = self.source_repository.get(source_id)
            representation = self.representation_repository.get(
                representation_id
            )
            artifact_inventory = [
                {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in representation.artifacts
            ]
            checkpoint = self.run_store.checkpoint()
            metrics = _validated_governance_metrics(
                item.get("governance_metrics")
            )
            if (
                item.get("state") != "represented"
                or item.get("atomic_information_ids") != ordered_ids
                or item.get("source_id") != source_id
                or item.get("representation_id") != representation_id
                or started_receipt.get("phase") != "started"
                or _sha256_bytes(
                    _canonical_json(started_receipt).encode("utf-8")
                )
                != binding.get("governance_started_receipt_fingerprint")
                or _sha256_bytes(_canonical_json(metrics).encode("utf-8"))
                != binding.get("governance_metrics_fingerprint")
                or _plan_fingerprint(plan) != binding.get("plan_fingerprint")
                or _sha256_bytes(
                    _canonical_json(self.run_store.plan_receipt(run_id)).encode(
                        "utf-8"
                    )
                )
                != binding.get("plan_receipt_fingerprint")
                or plan.get("capture_fingerprint")
                != binding.get("capture_fingerprint")
                or _sha256_bytes(
                    _canonical_json(
                        None if checkpoint is None else checkpoint.to_dict()
                    ).encode("utf-8")
                )
                != binding.get("checkpoint_fingerprint")
                or _sha256_bytes(
                    _canonical_json(source.to_manifest_dict()).encode("utf-8")
                )
                != binding.get("source_manifest_fingerprint")
                or not self.source_repository.verify(source_id).verified
                or representation.source_id != source_id
                or not self.representation_repository.verify(
                    representation_id
                ).verified
                or _sha256_bytes(
                    _canonical_json(representation.to_manifest_dict()).encode(
                        "utf-8"
                    )
                )
                != binding.get("representation_manifest_fingerprint")
                or _sha256_bytes(
                    _canonical_json(artifact_inventory).encode("utf-8")
                )
                != binding.get(
                    "representation_artifact_inventory_fingerprint"
                )
                or _package_fingerprint(
                    self.workspace
                    / "02_processing"
                    / "information"
                    / representation_id
                )
                != binding.get("semantic_package_fingerprint")
                or [
                    _sha256_bytes(
                        _canonical_json(
                            asdict(
                                self.information_store.get_current(atomic_id)
                            )
                        ).encode("utf-8")
                    )
                    for atomic_id in ordered_ids
                ]
                != binding.get("ordered_atomic_revision_fingerprints")
                or status.get("state") not in {"failed", "processing"}
                or status.get("state") == "failed"
                and _sha256_bytes(_canonical_json(status).encode("utf-8"))
                != binding.get("status_fingerprint")
                or status.get("state") == "processing"
                and status.get("failure_category") is not None
            ):
                raise WechatDigestError("多项目 Governance durable binding 漂移。")
            with SQLiteWorldModelRepository(self.database) as repository:
                if (
                    [
                        self._governance_effect_binding(repository, atomic_id)
                        for atomic_id in ordered_ids
                    ]
                    != manifest.get("atomic_effect_bindings")
                    or self._governance_business_tree_fingerprint(repository)
                    != manifest.get("business_tree_fingerprint")
                ):
                    raise WechatDigestError("多项目 Governance 业务状态漂移。")
            if status.get("state") == "failed":
                status["state"] = "processing"
                status["failure_category"] = None
                status["updated_at"] = self.clock()
                self.run_store.update_status(run_id, status)
            observed = self.run_store.status(run_id)
            if (
                observed.get("state") != "processing"
                or observed.get("failure_category") is not None
                or observed.get("checkpoint_published") is not False
            ):
                raise WechatDigestError("多项目 Governance 状态读回失败。")
            return existing

    def _historical_failed_closed_summary(
        self, *, active_run_id: str
    ) -> dict[str, object]:
        """Read, validate, and bind failed-closed items outside the active run."""

        inventory: list[dict[str, object]] = []
        variant_counts: Counter[str] = Counter()
        if not self.run_store.runs_root.is_dir():
            raise WechatDigestError("历史 failed_closed run inventory 缺失。")
        for path in sorted(self.run_store.runs_root.iterdir()):
            if path.name == active_run_id:
                continue
            if not path.is_dir() or re.fullmatch(
                r"run_[0-9a-f]{32}", path.name
            ) is None:
                raise WechatDigestError(
                    "历史 failed_closed run inventory 损坏。"
                )
            run_id = path.name
            plan = self.run_store.plan(run_id)
            receipt = self.run_store.plan_receipt(run_id)
            status = self.run_store.status(run_id)
            plan_fingerprint = _plan_fingerprint(plan)
            if (
                plan.get("run_id") != run_id
                or status.get("run_id") != run_id
                or status.get("plan_fingerprint") != plan_fingerprint
                or _committed_receipt_fingerprint(receipt)
                != plan_fingerprint
            ):
                raise WechatDigestError(
                    "历史 failed_closed run binding 损坏。"
                )
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError(
                    "历史 failed_closed status inventory 损坏。"
                )
            failed_items: list[dict[str, object]] = []
            for item_id in sorted(items):
                item = items[item_id]
                if not isinstance(item, dict):
                    raise WechatDigestError(
                        "历史 failed_closed status item 损坏。"
                    )
                if item.get("state") != "failed_closed":
                    continue
                semantic_failure = item.get("semantic_failure")
                governance_failure = item.get("governance_failure")
                if (semantic_failure is None) == (governance_failure is None):
                    raise WechatDigestError(
                        "历史 failed_closed failure variant 损坏。"
                    )
                variant = (
                    "semantic"
                    if semantic_failure is not None
                    else "governance"
                )
                self._verify_failed_closed_item(
                    run_id=run_id,
                    plan=plan,
                    item_id=item_id,
                    item=item,
                )
                variant_counts[variant] += 1
                failed_items.append(
                    {
                        "item_id": item_id,
                        "failure_variant": variant,
                        "item_fingerprint": _sha256_bytes(
                            _canonical_json(item).encode("utf-8")
                        ),
                    }
                )
            if failed_items:
                inventory.append(
                    {
                        "run_id": run_id,
                        "plan_fingerprint": plan_fingerprint,
                        "plan_receipt_fingerprint": _sha256_bytes(
                            _canonical_json(receipt).encode("utf-8")
                        ),
                        "status_fingerprint": _sha256_bytes(
                            _canonical_json(status).encode("utf-8")
                        ),
                        "failed_closed_items": failed_items,
                    }
                )
        if variant_counts != {"semantic": 2, "governance": 3}:
            raise WechatDigestError(
                "历史 failed_closed summary 不匹配。"
            )
        return {
            "total": 5,
            "semantic": 2,
            "governance": 3,
            "inventory_fingerprint": _sha256_bytes(
                _canonical_json(inventory).encode("utf-8")
            ),
        }

    def _build_failed_closed_recovery_manifest_unlocked(
        self,
        *,
        authority_ref: str,
        adopted_continuation: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        run_id = self.run_store.active_run_id()
        if run_id is None:
            raise WechatDigestError("不存在可恢复历史失败校验的 active run。")
        plan = self.run_store.plan(run_id)
        plan_receipt = self.run_store.plan_receipt(run_id)
        status = self.run_store.status(run_id)
        current_failed_status = json.loads(_canonical_json(status))
        if (
            status.get("state") != "failed"
            or status.get("failure_category") != "WechatDigestError"
            or status.get("checkpoint_published") is not False
            or self._plan_all_history_upper(plan) is None
        ):
            raise WechatDigestError("历史失败恢复 active run 状态不匹配。")
        after = WechatCursor.from_dict(plan.get("after_cursor"), "plan.after_cursor")
        checkpoint = self.run_store.checkpoint()
        if checkpoint is not None and checkpoint != after:
            raise WechatDigestError("历史失败恢复 checkpoint binding 不匹配。")
        capture, _ = self._load_active_capture_artifacts(
            run_id, plan, status
        )
        items = status.get("items")
        if not isinstance(items, dict) or len(items) != 189:
            raise WechatDigestError("历史失败恢复 item inventory 不匹配。")
        state_counts = Counter(
            item.get("state")
            for item in items.values()
            if isinstance(item, dict)
        )
        if state_counts != {
            "local_only": 3,
            "pending_human": 20,
            "planned": 16,
            "processed": 7,
            "represented": 1,
            "unsupported": 142,
        }:
            raise WechatDigestError("历史失败恢复 item 状态边界不匹配。")
        historical_failed_closed_summary = (
            self._historical_failed_closed_summary(active_run_id=run_id)
        )
        represented = [
            (item_id, item)
            for item_id, item in items.items()
            if isinstance(item, dict) and item.get("state") == "represented"
        ]
        if len(represented) != 1:
            raise WechatDigestError("历史失败恢复 current item 不唯一。")
        current_item_id, current_item = represented[0]
        source_id = current_item.get("source_id")
        representation_id = current_item.get("representation_id")
        if not isinstance(source_id, str) or not isinstance(
            representation_id, str
        ):
            raise WechatDigestError("历史失败恢复 current identity 损坏。")
        source = self.source_repository.get(source_id)
        representation = self.representation_repository.get(representation_id)
        artifact_inventory = [
            {
                "artifact_id": artifact.artifact_id,
                "content_hash": artifact.content_hash,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in representation.artifacts
        ]
        package = (
            self.workspace / "02_processing" / "information" / representation_id
        )
        if (
            current_item.get("atomic_information_ids") != []
            or current_item.get("governance_receipt") is not None
            or current_item.get("governance_metrics") is not None
            or current_item.get("semantic_failure") is not None
            or current_item.get("governance_failure") is not None
            or package.exists()
            or representation.source_id != source_id
            or not self.source_repository.verify(source_id).verified
            or not self.representation_repository.verify(
                representation_id
            ).verified
        ):
            raise WechatDigestError("历史失败恢复 current represented item 漂移。")
        startup = self.run_store.governance_startup_recovery(run_id)
        retry = self.run_store.governance_startup_retry(run_id)
        if startup is None or retry is None:
            raise WechatDigestError("历史失败恢复缺少 Issue #150 durable receipt。")
        startup_binding = startup.get("recovery_binding")
        previous_item_id = (
            startup_binding.get("item_id")
            if isinstance(startup_binding, dict)
            else None
        )
        previous_item = items.get(previous_item_id)
        previous_atomic_ids = (
            previous_item.get("atomic_information_ids")
            if isinstance(previous_item, dict)
            else None
        )
        previous_receipt = (
            _validated_governance_receipt(
                previous_item.get("governance_receipt")
            )
            if isinstance(previous_item, dict)
            else None
        )
        previous_representation_id = (
            previous_item.get("representation_id")
            if isinstance(previous_item, dict)
            else None
        )
        if (
            not isinstance(previous_item_id, str)
            or not isinstance(previous_item, dict)
            or previous_item.get("state") != "processed"
            or not isinstance(previous_atomic_ids, list)
            or len(previous_atomic_ids) != 4
            or len(set(previous_atomic_ids)) != 4
            or previous_receipt is None
            or previous_receipt.get("phase") != "completed"
            or not isinstance(previous_representation_id, str)
            or startup.get("retry_consumed") is not False
            or retry.get("retry_attempt") != 1
            or retry.get("run_id") != run_id
            or retry.get("item_id") != previous_item_id
        ):
            raise WechatDigestError("历史失败恢复 previous completed item 漂移。")
        semantic_summary = _validated_global_attempt_summary(
            self._semantic_port().global_attempt_summary(
                previous_representation_id
            )
        )
        if semantic_summary != {
            "global_attempt_total": 298,
            "global_unknown": 0,
            "next_global_ordinal": 299,
            "absolute_cap": 1000,
        }:
            raise WechatDigestError("历史失败恢复 Semantic ledger 不匹配。")
        window_binding = self._semantic_authority_binding(
            run_id, allow_reviewed_head_extension=True
        )
        if adopted_continuation is not None:
            previous_head = adopted_continuation.get(
                "previous_reviewed_git_head"
            )
            reviewed_head = adopted_continuation.get("reviewed_git_head")
            if (
                previous_head
                != "c8ece3782ae3ba289d06c36d1e352ce23e0f627b"
                or reviewed_head != self._semantic_port().reviewed_git_head
            ):
                raise WechatDigestError(
                    "历史失败恢复 continuation head binding 漂移。"
                )
            window_binding = replace(
                window_binding, reviewed_git_head=str(previous_head)
            )
        else:
            previous_head = window_binding.reviewed_git_head
            reviewed_head = self._semantic_port().reviewed_git_head
        with SQLiteWorldModelRepository(self.database) as repository:
            business_tree_fingerprint = (
                self._governance_business_tree_fingerprint(repository)
            )
        recovery_binding: dict[str, object] = {
            "run_id": run_id,
            "plan_fingerprint": _plan_fingerprint(plan),
            "plan_receipt_fingerprint": _sha256_bytes(
                _canonical_json(plan_receipt).encode("utf-8")
            ),
            "current_status_fingerprint": _sha256_bytes(
                _canonical_json(status).encode("utf-8")
            ),
            "current_failed_status": current_failed_status,
            "capture_fingerprint": _capture_fingerprint(capture),
            "checkpoint_fingerprint": _sha256_bytes(
                _canonical_json(
                    None if checkpoint is None else checkpoint.to_dict()
                ).encode("utf-8")
            ),
            "current_item_id": current_item_id,
            "source_id": source_id,
            "source_manifest_fingerprint": _sha256_bytes(
                _canonical_json(source.to_manifest_dict()).encode("utf-8")
            ),
            "representation_id": representation_id,
            "representation_manifest_fingerprint": _sha256_bytes(
                _canonical_json(representation.to_manifest_dict()).encode("utf-8")
            ),
            "representation_artifact_inventory_fingerprint": _sha256_bytes(
                _canonical_json(artifact_inventory).encode("utf-8")
            ),
            "previous_item_id": previous_item_id,
            "previous_atomic_information_ids": list(previous_atomic_ids),
            "previous_governance_receipt_fingerprint": _sha256_bytes(
                _canonical_json(previous_receipt).encode("utf-8")
            ),
            "startup_recovery_receipt_fingerprint": _sha256_bytes(
                _canonical_json(startup).encode("utf-8")
            ),
            "startup_retry_receipt_fingerprint": _sha256_bytes(
                _canonical_json(retry).encode("utf-8")
            ),
            "semantic_window_binding_fingerprint": _sha256_bytes(
                _canonical_json(asdict(window_binding)).encode("utf-8")
            ),
        }
        candidate: dict[str, object] = {
            "schema_version": FAILED_CLOSED_RECOVERY_MANIFEST_SCHEMA_VERSION,
            "authority_ref": authority_ref,
            "recovery_binding": recovery_binding,
            "business_tree_fingerprint": business_tree_fingerprint,
            "previous_reviewed_git_head": previous_head,
            "reviewed_git_head": reviewed_head,
            "execution_contract_unchanged": True,
            "semantic_summary": {**semantic_summary, "last_global_ordinal": 298},
            "historical_failed_closed_summary": (
                historical_failed_closed_summary
            ),
        }
        candidate["manifest_fingerprint"] = _sha256_bytes(
            _canonical_json(candidate).encode("utf-8")
        )
        return _validated_failed_closed_recovery_manifest(candidate)

    def build_failed_closed_recovery_manifest(
        self, *, authority_ref: str
    ) -> dict[str, object]:
        """Build the read-only Issue #154 private authority candidate."""

        with self.run_store.lock():
            return self._build_failed_closed_recovery_manifest_unlocked(
                authority_ref=authority_ref
            )

    def resolve_failed_closed_continuation(
        self, *, authority_ref: str, authority_manifest_file: Path
    ) -> dict[str, object]:
        """Recover the fixed post-Gate-C failed state with zero Providers."""

        manifest, raw_fingerprint = _read_private_json_manifest(
            authority_manifest_file
        )
        manifest = _validated_failed_closed_recovery_manifest(manifest)
        if manifest.get("authority_ref") != authority_ref:
            raise WechatDigestError("历史失败恢复 authority ref 不匹配。")
        binding = manifest["recovery_binding"]
        assert isinstance(binding, dict)
        run_id = str(binding["run_id"])
        with self.run_store.lock():
            if self.run_store.active_run_id() != run_id:
                raise WechatDigestError("历史失败恢复 active run 不匹配。")
            existing = self.run_store.failed_closed_recovery(run_id)
            if existing is None:
                continuation = (
                    self._semantic_port().failed_closed_recovery_continuation(
                        authority_ref=authority_ref,
                        authority_manifest_fingerprint=str(
                            manifest["manifest_fingerprint"]
                        ),
                        authority_manifest_raw_fingerprint=raw_fingerprint,
                    )
                )
                expected = self._build_failed_closed_recovery_manifest_unlocked(
                    authority_ref=authority_ref,
                    adopted_continuation=continuation,
                )
                if expected != manifest:
                    raise WechatDigestError(
                        "历史失败恢复 manifest 与现场不匹配。"
                    )
                if continuation is None:
                    continuation = self._semantic_port().install_failed_closed_recovery_continuation(
                        window_binding=self._semantic_authority_binding(
                            run_id, allow_reviewed_head_extension=True
                        ),
                        authority_ref=authority_ref,
                        authority_manifest_fingerprint=str(
                            manifest["manifest_fingerprint"]
                        ),
                        authority_manifest_raw_fingerprint=raw_fingerprint,
                    )
                receipt_without_fingerprint: dict[str, object] = {
                    "schema_version": FAILED_CLOSED_RECOVERY_SCHEMA_VERSION,
                    "artifact_kind": "failed_closed_recovery",
                    "authority_ref": authority_ref,
                    "authority_manifest_fingerprint": manifest[
                        "manifest_fingerprint"
                    ],
                    "authority_manifest_raw_fingerprint": raw_fingerprint,
                    "recovery_binding": binding,
                    "business_tree_fingerprint": manifest[
                        "business_tree_fingerprint"
                    ],
                    "historical_failed_closed_summary": manifest[
                        "historical_failed_closed_summary"
                    ],
                    "semantic_continuation_fingerprint": continuation[
                        "continuation_fingerprint"
                    ],
                    "provider_calls": 0,
                }
                receipt = {
                    **receipt_without_fingerprint,
                    "receipt_fingerprint": _sha256_bytes(
                        _canonical_json(receipt_without_fingerprint).encode("utf-8")
                    ),
                }
                existing = self.run_store.publish_governance_startup_receipt(
                    run_id,
                    filename="failed-closed-recovery.json",
                    receipt=receipt,
                )
            projected = dict(existing)
            receipt_fingerprint = projected.pop("receipt_fingerprint", None)
            if (
                set(existing)
                != {
                    "schema_version",
                    "artifact_kind",
                    "authority_ref",
                    "authority_manifest_fingerprint",
                    "authority_manifest_raw_fingerprint",
                    "recovery_binding",
                    "business_tree_fingerprint",
                    "historical_failed_closed_summary",
                    "semantic_continuation_fingerprint",
                    "provider_calls",
                    "receipt_fingerprint",
                }
                or existing.get("schema_version")
                != FAILED_CLOSED_RECOVERY_SCHEMA_VERSION
                or existing.get("artifact_kind") != "failed_closed_recovery"
                or existing.get("authority_ref") != authority_ref
                or existing.get("authority_manifest_fingerprint")
                != manifest.get("manifest_fingerprint")
                or existing.get("authority_manifest_raw_fingerprint")
                != raw_fingerprint
                or existing.get("recovery_binding") != binding
                or existing.get("business_tree_fingerprint")
                != manifest.get("business_tree_fingerprint")
                or existing.get("historical_failed_closed_summary")
                != manifest.get("historical_failed_closed_summary")
                or not _sha256_value(
                    existing.get("semantic_continuation_fingerprint")
                )
                or existing.get("provider_calls") != 0
                or not _sha256_value(receipt_fingerprint)
                or receipt_fingerprint
                != _sha256_bytes(_canonical_json(projected).encode("utf-8"))
            ):
                raise WechatDigestError("历史失败恢复 receipt 不匹配。")
            observed_continuation = (
                self._semantic_port().failed_closed_recovery_continuation(
                    authority_ref=authority_ref,
                    authority_manifest_fingerprint=str(
                        manifest["manifest_fingerprint"]
                    ),
                    authority_manifest_raw_fingerprint=raw_fingerprint,
                )
            )
            if (
                observed_continuation is None
                or observed_continuation.get("continuation_fingerprint")
                != existing.get("semantic_continuation_fingerprint")
            ):
                raise WechatDigestError(
                    "历史失败恢复 semantic continuation 读回失败。"
                )
            plan = self.run_store.plan(run_id)
            capture, _ = self._load_active_capture_artifacts(
                run_id, plan, self.run_store.status(run_id)
            )
            checkpoint = self.run_store.checkpoint()
            source_id = str(binding["source_id"])
            representation_id = str(binding["representation_id"])
            source = self.source_repository.get(source_id)
            representation = self.representation_repository.get(
                representation_id
            )
            artifacts = [
                {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in representation.artifacts
            ]
            startup = self.run_store.governance_startup_recovery(run_id)
            retry = self.run_store.governance_startup_retry(run_id)
            previous_item = binding["current_failed_status"]["items"].get(
                binding["previous_item_id"]
            )
            previous_receipt = (
                previous_item.get("governance_receipt")
                if isinstance(previous_item, dict)
                else None
            )
            historical_failed_closed_summary = (
                self._historical_failed_closed_summary(
                    active_run_id=run_id
                )
            )
            if (
                _plan_fingerprint(plan) != binding.get("plan_fingerprint")
                or _sha256_bytes(
                    _canonical_json(self.run_store.plan_receipt(run_id)).encode(
                        "utf-8"
                    )
                )
                != binding.get("plan_receipt_fingerprint")
                or _capture_fingerprint(capture)
                != binding.get("capture_fingerprint")
                or _sha256_bytes(
                    _canonical_json(
                        None if checkpoint is None else checkpoint.to_dict()
                    ).encode("utf-8")
                )
                != binding.get("checkpoint_fingerprint")
                or not self.source_repository.verify(source_id).verified
                or _sha256_bytes(
                    _canonical_json(source.to_manifest_dict()).encode("utf-8")
                )
                != binding.get("source_manifest_fingerprint")
                or representation.source_id != source_id
                or not self.representation_repository.verify(
                    representation_id
                ).verified
                or _sha256_bytes(
                    _canonical_json(representation.to_manifest_dict()).encode(
                        "utf-8"
                    )
                )
                != binding.get("representation_manifest_fingerprint")
                or _sha256_bytes(
                    _canonical_json(artifacts).encode("utf-8")
                )
                != binding.get(
                    "representation_artifact_inventory_fingerprint"
                )
                or (
                    self.workspace
                    / "02_processing"
                    / "information"
                    / representation_id
                ).exists()
                or startup is None
                or _sha256_bytes(
                    _canonical_json(startup).encode("utf-8")
                )
                != binding.get("startup_recovery_receipt_fingerprint")
                or retry is None
                or _sha256_bytes(_canonical_json(retry).encode("utf-8"))
                != binding.get("startup_retry_receipt_fingerprint")
                or _sha256_bytes(
                    _canonical_json(previous_receipt).encode("utf-8")
                )
                != binding.get("previous_governance_receipt_fingerprint")
                or historical_failed_closed_summary
                != manifest.get("historical_failed_closed_summary")
            ):
                raise WechatDigestError("历史失败恢复 durable binding 漂移。")
            status = self.run_store.status(run_id)
            current_failed_status = binding.get("current_failed_status")
            assert isinstance(current_failed_status, dict)
            recovered_status = json.loads(
                _canonical_json(current_failed_status)
            )
            recovered_status["state"] = "processing"
            recovered_status["failure_category"] = None
            if status == current_failed_status:
                status = recovered_status
                self.run_store.update_status(run_id, status)
            elif status != recovered_status:
                raise WechatDigestError("历史失败恢复 status 漂移。")
            if self.run_store.status(run_id) != recovered_status:
                raise WechatDigestError("历史失败恢复 status 读回失败。")
            with SQLiteWorldModelRepository(self.database) as repository:
                if self._governance_business_tree_fingerprint(repository) != (
                    manifest["business_tree_fingerprint"]
                ):
                    raise WechatDigestError("历史失败恢复业务状态漂移。")
            return existing

    def _governance_effect_fingerprint(
        self, atomic_ids: Sequence[str]
    ) -> str:
        selected = set(atomic_ids)
        payload = {
            "information": [
                asdict(self.information_store.get_current(atomic_id))
                for atomic_id in atomic_ids
            ],
            "unresolved_proposals": [
                asdict(proposal)
                for proposal in self.proposal_store.list_unresolved()
                if proposal.atomic_information_id in selected
            ],
            "journal": [
                asdict(record)
                for record in self.journal.list_changes()
                if record.atomic_information_id in selected
            ],
        }
        return _sha256_bytes(_canonical_json(payload).encode("utf-8"))

    @staticmethod
    def _governance_apply_receipts(
        repository: SQLiteWorldModelRepository,
        atomic_ids: Sequence[str],
    ) -> tuple[dict[str, object], ...]:
        selected = set(atomic_ids)
        matched: list[dict[str, object]] = []
        for receipt in repository.list_apply_receipts():
            try:
                payload = json.loads(receipt.payload)
            except json.JSONDecodeError as exc:
                raise WechatDigestError(
                    "Batch Governance apply receipt 损坏。"
                ) from exc
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise WechatDigestError("Batch Governance apply receipt 损坏。")
            if any(
                isinstance(record, dict)
                and record.get("atomic_information_id") in selected
                for record in records
            ):
                matched.append(asdict(receipt))
        return tuple(matched)

    def _governance_effect_snapshot(
        self,
        repository: SQLiteWorldModelRepository,
        atomic_id: str,
    ) -> dict[str, object]:
        revisions = tuple(
            asdict(item) for item in self.information_store.list_revisions(atomic_id)
        )
        if not revisions:
            raise WechatDigestError("Batch Governance Atomic Information 缺失。")
        proposals = tuple(
            asdict(item)
            for item in self.proposal_store.list_history()
            if item.atomic_information_id == atomic_id
        )
        journal = tuple(
            asdict(item)
            for item in self.journal.list_changes()
            if item.atomic_information_id == atomic_id
        )
        apply_receipts = self._governance_apply_receipts(repository, (atomic_id,))
        object_ids = {
            object_id
            for revision in self.information_store.list_revisions(atomic_id)
            for object_id in revision.related_object_ids
        }
        objects = {
            record.object_id: record for record in repository.list_objects()
        }
        candidate_names = {
            " ".join(str(concern).split()).casefold()
            for concern in revisions[-1]["raw_concerns"]
        }
        object_ids.update(
            assignment.object_id
            for record in objects.values()
            if record.status == "active"
            for assignment in repository.list_names(record.object_id)
            if " ".join(assignment.name.split()).casefold()
            in candidate_names
        )
        for proposal in self.proposal_store.list_history():
            if proposal.atomic_information_id == atomic_id:
                object_ids.update(proposal.resolved_object_ids)
        for record in self.journal.list_changes():
            if record.atomic_information_id == atomic_id:
                object_ids.update(record.resolved_object_ids)
        for receipt in apply_receipts:
            payload = json.loads(str(receipt["payload"]))
            object_ids.update(payload.get("created_object_ids", ()))
            for record in payload.get("records", ()):
                if isinstance(record, dict):
                    object_ids.update(record.get("resolved_object_ids", ()))
        if any(object_id not in objects for object_id in object_ids):
            raise WechatDigestError("Batch Governance World binding 缺失。")
        world_projection = {
            "objects": [asdict(objects[object_id]) for object_id in sorted(object_ids)],
            "names": [
                asdict(item)
                for object_id in sorted(object_ids)
                for item in repository.list_names(object_id)
            ],
            "roles": [
                asdict(item)
                for object_id in sorted(object_ids)
                for item in repository.list_roles(object_id)
            ],
            "lifecycles": [
                asdict(item)
                for object_id in sorted(object_ids)
                for item in repository.list_lifecycles(object_id)
            ],
            "relationships": [
                asdict(item)
                for item in repository.list_relationships(active_only=False)
                if item.from_object_id in object_ids or item.to_object_id in object_ids
            ],
            "external_identity_mappings": [
                asdict(item)
                for item in repository.list_external_identity_mappings()
                if item.object_id in object_ids
            ],
        }
        payload = {
            "revisions": revisions,
            "proposal_history": proposals,
            "journal": journal,
            "apply_receipts": apply_receipts,
            "world_projection": world_projection,
        }
        return payload

    def _governance_effect_binding(
        self,
        repository: SQLiteWorldModelRepository,
        atomic_id: str,
    ) -> dict[str, object]:
        payload = self._governance_effect_snapshot(repository, atomic_id)
        revisions = tuple(payload["revisions"])
        proposals = tuple(payload["proposal_history"])
        journal = tuple(payload["journal"])
        apply_receipts = tuple(payload["apply_receipts"])
        world_projection = payload["world_projection"]
        return {
            "atomic_information_id": atomic_id,
            "current_revision_fingerprint": _sha256_bytes(
                _canonical_json(revisions[-1]).encode("utf-8")
            ),
            "revision_history_fingerprint": _sha256_bytes(
                _canonical_json(revisions).encode("utf-8")
            ),
            "revision_count": len(revisions),
            "proposal_history_fingerprint": _sha256_bytes(
                _canonical_json(proposals).encode("utf-8")
            ),
            "proposal_history_count": len(proposals),
            "journal_fingerprint": _sha256_bytes(
                _canonical_json(journal).encode("utf-8")
            ),
            "journal_count": len(journal),
            "apply_receipts_fingerprint": _sha256_bytes(
                _canonical_json(apply_receipts).encode("utf-8")
            ),
            "apply_receipt_count": len(apply_receipts),
            "world_projection_fingerprint": _sha256_bytes(
                _canonical_json(world_projection).encode("utf-8")
            ),
            "effect_fingerprint": _sha256_bytes(
                _canonical_json(payload).encode("utf-8")
            ),
        }

    def _governance_effect_snapshots(
        self,
        repository: SQLiteWorldModelRepository,
        atomic_ids: Sequence[str],
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            self._governance_effect_snapshot(repository, atomic_id)
            for atomic_id in atomic_ids
        )

    @staticmethod
    def _governance_snapshot_fingerprints(
        snapshots: Sequence[Mapping[str, object]],
    ) -> tuple[str, ...]:
        return tuple(
            _sha256_bytes(_canonical_json(snapshot).encode("utf-8"))
            for snapshot in snapshots
        )

    def _governance_effect_fingerprints(
        self,
        repository: SQLiteWorldModelRepository,
        atomic_ids: Sequence[str],
    ) -> tuple[str, ...]:
        return tuple(
            str(
                self._governance_effect_binding(repository, atomic_id)[
                    "effect_fingerprint"
                ]
            )
            for atomic_id in atomic_ids
        )

    def _verify_governance_receipt_effects(
        self, receipt: Mapping[str, object]
    ) -> None:
        if (
            receipt.get("schema_version") != GOVERNANCE_RECEIPT_SCHEMA_VERSION
            or receipt.get("phase") == "started"
        ):
            return
        batch_ids = tuple(receipt["batch_atomic_information_ids"])
        expected = tuple(receipt["cursor_effect_fingerprints"])
        with SQLiteWorldModelRepository(self.database) as repository:
            current_snapshots = self._governance_effect_snapshots(
                repository, batch_ids
            )
        current = self._governance_snapshot_fingerprints(current_snapshots)
        if current == expected:
            return
        if not self._governance_in_flight_effect_is_proven(
            receipt, current_snapshots
        ):
            raise WechatDigestError(
                "微信 Governance receipt effect/cursor binding 漂移。"
            )

    @staticmethod
    def _snapshot_sequence(value: object) -> tuple[dict[str, object], ...]:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, dict) for item in value
        ):
            raise WechatDigestError("微信 Governance recovery evidence 损坏。")
        return tuple(dict(item) for item in value)

    @staticmethod
    def _snapshot_world_rows(
        snapshots: Sequence[Mapping[str, object]],
    ) -> dict[str, dict[str, dict[str, object]]]:
        keys = {
            "objects": "object_id",
            "names": "name_assignment_id",
            "roles": "role_assignment_id",
            "lifecycles": "lifecycle_record_id",
            "relationships": "relationship_id",
            "external_identity_mappings": "identity_key",
        }
        collected = {name: {} for name in keys}
        for snapshot in snapshots:
            projection = snapshot.get("world_projection")
            if not isinstance(projection, dict):
                raise WechatDigestError(
                    "微信 Governance recovery evidence 损坏。"
                )
            for name, key in keys.items():
                rows = projection.get(name)
                if not isinstance(rows, (list, tuple)):
                    raise WechatDigestError(
                        "微信 Governance recovery evidence 损坏。"
                    )
                for raw in rows:
                    if not isinstance(raw, dict) or not isinstance(
                        raw.get(key), str
                    ):
                        raise WechatDigestError(
                            "微信 Governance recovery evidence 损坏。"
                        )
                    identity = str(raw[key])
                    row = dict(raw)
                    existing = collected[name].get(identity)
                    if existing is not None and existing != row:
                        raise WechatDigestError(
                            "微信 Governance recovery evidence 冲突。"
                        )
                    collected[name][identity] = row
        return collected

    def _governance_in_flight_effect_is_proven(
        self,
        receipt: Mapping[str, object],
        current_snapshots: Sequence[Mapping[str, object]],
    ) -> bool:
        # A cursor may lag one committed item; accept only receipt-proven deltas.
        in_flight_index = receipt.get("in_flight_index")
        if not isinstance(in_flight_index, int) or isinstance(
            in_flight_index, bool
        ):
            return False
        baseline_snapshots = receipt.get("cursor_effect_snapshots")
        batch_ids = receipt.get("batch_atomic_information_ids")
        interpretations = receipt.get("interpretations")
        if (
            not isinstance(baseline_snapshots, list)
            or not isinstance(batch_ids, list)
            or not isinstance(interpretations, list)
            or len(baseline_snapshots) != len(current_snapshots)
            or not 0 <= in_flight_index < len(batch_ids)
        ):
            return False
        atomic_id = batch_ids[in_flight_index]
        if not isinstance(atomic_id, str):
            return False
        try:
            interpretation = parse_interpretation(
                interpretations[in_flight_index]
            )
            interpretation_fingerprint = (
                AtomicInformationDigestionService._interpretation_fingerprint(
                    interpretation
                )
            )
            baseline = tuple(
                dict(snapshot) for snapshot in baseline_snapshots
            )
            current = tuple(
                json.loads(_canonical_json(snapshot))
                for snapshot in current_snapshots
            )
            for index, (before, after) in enumerate(
                zip(baseline, current, strict=True)
            ):
                for name in (
                    "revisions",
                    "proposal_history",
                    "journal",
                    "apply_receipts",
                ):
                    before_rows = self._snapshot_sequence(before.get(name))
                    after_rows = self._snapshot_sequence(after.get(name))
                    if after_rows[: len(before_rows)] != before_rows:
                        return False
                    if index != in_flight_index and after_rows != before_rows:
                        return False

            before_in_flight = baseline[in_flight_index]
            after_in_flight = current[in_flight_index]
            before_receipts = self._snapshot_sequence(
                before_in_flight.get("apply_receipts")
            )
            after_receipts = self._snapshot_sequence(
                after_in_flight.get("apply_receipts")
            )
            added_receipts = after_receipts[len(before_receipts) :]
            before_proposals = self._snapshot_sequence(
                before_in_flight.get("proposal_history")
            )
            after_proposals = self._snapshot_sequence(
                after_in_flight.get("proposal_history")
            )
            added_proposals = after_proposals[len(before_proposals) :]
            identity_actions = {
                "bind_existing",
                "create_minimal",
                "edit_identity_and_create",
                "reject",
                "defer",
            }
            interpretation_payload = interpretation_to_dict(interpretation)
            for proposal in added_proposals:
                human_review = proposal.get("human_review")
                if not isinstance(human_review, dict):
                    return False
                allowed_actions = set(human_review.get("allowed_actions", ()))
                interpretation_proposal = (
                    proposal.get("interpretation_fingerprint")
                    == interpretation_fingerprint
                    and proposal.get("proposed_operations")
                    == interpretation_payload["operations"]
                    and proposal.get("rationale") == interpretation.rationale
                    and allowed_actions == {"approve", "reject", "defer"}
                )
                identity_proposal = (
                    proposal.get("proposed_operations")
                    == [
                        interpretation_to_dict(
                            InterpretationResult(
                                operations=(
                                    WorldModelOperation(kind="unresolved"),
                                ),
                                rationale="identity-proof",
                                evidence_sufficient=False,
                                conflict=False,
                                ambiguous=True,
                            )
                        )["operations"][0]
                    ]
                    and proposal.get("rationale")
                    == "Identity Gate requires a human identity decision."
                    and allowed_actions == identity_actions
                )
                if (
                    proposal.get("atomic_information_id") != atomic_id
                    or proposal.get("status") != "pending"
                    or proposal.get("decided_at") is not None
                    or not (interpretation_proposal or identity_proposal)
                ):
                    return False

            receipt_records: list[dict[str, object]] = []
            created_object_ids: set[str] = set()
            for apply_receipt in added_receipts:
                payload = json.loads(str(apply_receipt.get("payload")))
                records = payload.get("records")
                created = payload.get("created_object_ids")
                if (
                    not isinstance(records, list)
                    or not records
                    or not isinstance(created, list)
                    or any(not isinstance(item, str) for item in created)
                    or any(
                        not isinstance(record, dict)
                        or record.get("atomic_information_id") != atomic_id
                        for record in records
                    )
                ):
                    return False
                receipt_records.extend(dict(record) for record in records)
                created_object_ids.update(created)

            interpretation_operations = {
                operation.kind for operation in interpretation.operations
            }
            identity_operations = {"bind_existing", "create_minimal"}
            if any(
                record.get("operation") not in (
                    interpretation_operations | identity_operations
                )
                or (
                    record.get("operation") not in identity_operations
                    and record.get("interpretation_fingerprint")
                    != interpretation_fingerprint
                )
                for record in receipt_records
            ):
                return False

            before_journal = self._snapshot_sequence(
                before_in_flight.get("journal")
            )
            after_journal = self._snapshot_sequence(
                after_in_flight.get("journal")
            )
            added_journal = after_journal[len(before_journal) :]
            before_revisions = self._snapshot_sequence(
                before_in_flight.get("revisions")
            )
            after_revisions = self._snapshot_sequence(
                after_in_flight.get("revisions")
            )
            added_revisions = after_revisions[len(before_revisions) :]
            added_revision_ids = {
                revision.get("revision_id") for revision in added_revisions
            }
            receipt_change_ids = {
                record.get("change_id") for record in receipt_records
            }
            if any(
                record.get("atomic_information_id") != atomic_id
                or (
                    record.get("change_id") not in receipt_change_ids
                    and not (
                        record.get("operation") == "bind_atomic_information"
                        and record.get("interpretation_fingerprint")
                        == interpretation_fingerprint
                        and record.get("atomic_information_revision_id")
                        in added_revision_ids
                    )
                )
                for record in added_journal
            ):
                return False
            if not receipt_change_ids.issubset(
                {record.get("change_id") for record in added_journal}
            ):
                return False

            allowed_revision_reasons = {
                "identity_gate_bind_existing",
                "identity_gate_create_minimal",
                "object_binding",
                "claim_enrichment",
                "claim_enrichment_and_object_binding",
            }
            stable_revision_fields = {
                "atomic_information_id",
                "origin_source_id",
                "origin_candidate_id",
                "origin_fingerprint",
                "statement",
                "semantic_type",
                "raw_concerns",
                "source_evidence",
                "context",
                "confidence",
            }
            previous_revision = before_revisions[-1]
            for revision in added_revisions:
                reason = revision.get("revision_reason")
                next_number = previous_revision.get("revision_number")
                if not isinstance(next_number, int) or isinstance(
                    next_number, bool
                ):
                    return False
                next_number += 1
                if (
                    reason not in allowed_revision_reasons
                    or revision.get("atomic_information_id") != atomic_id
                    or revision.get("revision_number") != next_number
                    or revision.get("revision_id")
                    != f"{atomic_id}-r{next_number:04d}"
                    or any(
                        revision.get(field) != previous_revision.get(field)
                        for field in stable_revision_fields
                    )
                ):
                    return False
                binding_changed = reason in {
                    "identity_gate_bind_existing",
                    "identity_gate_create_minimal",
                    "object_binding",
                    "claim_enrichment_and_object_binding",
                }
                previous_object_ids = set(
                    previous_revision.get("related_object_ids", ())
                )
                revised_object_ids = set(
                    revision.get("related_object_ids", ())
                )
                if binding_changed:
                    added_object_ids = revised_object_ids - previous_object_ids
                    if (
                        not added_object_ids
                        or not previous_object_ids.issubset(revised_object_ids)
                    ):
                        return False
                    if reason.startswith("identity_gate_"):
                        expected_operation = reason.removeprefix(
                            "identity_gate_"
                        )
                        if not any(
                            record.get("operation") == expected_operation
                            and set(record.get("resolved_object_ids", ()))
                            == added_object_ids
                            for record in receipt_records
                        ):
                            return False
                    elif not any(
                        record.get("operation") == "bind_atomic_information"
                        and record.get("atomic_information_revision_id")
                        == revision.get("revision_id")
                        and set(record.get("resolved_object_ids", ()))
                        == revised_object_ids
                        and record.get("interpretation_fingerprint")
                        == interpretation_fingerprint
                        for record in added_journal
                    ):
                        return False
                elif revised_object_ids != previous_object_ids:
                    return False

                claim_changed = reason in {
                    "claim_enrichment",
                    "claim_enrichment_and_object_binding",
                }
                if claim_changed:
                    if (
                        previous_revision.get("claim") is not None
                        or interpretation_payload["claim"] is None
                        or revision.get("claim")
                        != interpretation_payload["claim"]
                    ):
                        return False
                elif revision.get("claim") != previous_revision.get("claim"):
                    return False
                previous_revision = revision
            revision_ids = {
                revision.get("revision_id") for revision in after_revisions
            }
            if any(
                proposal.get("atomic_information_revision_id")
                not in revision_ids
                for proposal in added_proposals
            ):
                return False
            if not (
                added_receipts
                or added_proposals
                or added_revisions
                or added_journal
            ):
                return False
            before_world = self._snapshot_world_rows(baseline)
            after_world = self._snapshot_world_rows(current)
            operation_by_kind = {
                operation.kind: operation for operation in interpretation.operations
            }
            for collection, before_rows in before_world.items():
                after_rows = after_world[collection]
                if any(identity not in after_rows for identity in before_rows):
                    return False
                changed = {
                    identity: row
                    for identity, row in after_rows.items()
                    if before_rows.get(identity) != row
                }
                for identity, row in changed.items():
                    previous = before_rows.get(identity)
                    if collection == "roles":
                        add = operation_by_kind.get("add_role")
                        end = operation_by_kind.get("end_role")
                        if previous is None:
                            if (
                                add is None
                                or row.get("object_id") != add.target_object_id
                                or row.get("role") != add.role
                                or row.get("source_atomic_information_id")
                                != atomic_id
                            ):
                                return False
                        elif (
                            end is None
                            or row.get("object_id") != end.target_object_id
                            or row.get("role") != end.role
                            or previous.get("valid_to") is not None
                            or row.get("valid_to") is None
                        ):
                            return False
                    elif collection == "objects":
                        delete = operation_by_kind.get("delete_object")
                        if previous is None:
                            if identity not in created_object_ids:
                                return False
                        elif delete is not None and identity == delete.target_object_id:
                            if row.get("status") != "deleted":
                                return False
                        else:
                            affected_object_ids = {
                                candidate
                                for operation in interpretation.operations
                                for candidate in (
                                    operation.target_object_id,
                                    operation.secondary_object_id,
                                )
                                if candidate is not None
                            }
                            stable_before = dict(previous)
                            stable_after = dict(row)
                            stable_before.pop("updated_at", None)
                            stable_after.pop("updated_at", None)
                            if (
                                identity not in affected_object_ids
                                or stable_before != stable_after
                            ):
                                return False
                    elif collection == "names":
                        rename = operation_by_kind.get("rename")
                        if previous is None:
                            if (
                                row.get("object_id") not in created_object_ids
                                and (
                                    rename is None
                                    or row.get("object_id")
                                    != rename.target_object_id
                                    or row.get("name") != rename.name
                                )
                            ):
                                return False
                        elif (
                            rename is None
                            or row.get("object_id") != rename.target_object_id
                            or previous.get("valid_to") is not None
                            or row.get("valid_to") is None
                        ):
                            return False
                    elif collection == "lifecycles":
                        lifecycle = operation_by_kind.get("set_lifecycle")
                        if (
                            lifecycle is None
                            or row.get("object_id")
                            != lifecycle.target_object_id
                        ):
                            return False
                    elif collection == "relationships":
                        create = operation_by_kind.get("create_relationship")
                        end = operation_by_kind.get("end_relationship")
                        if previous is None:
                            if (
                                create is None
                                or row.get("from_object_id")
                                != create.target_object_id
                                or row.get("to_object_id")
                                != create.secondary_object_id
                                or row.get("relation") != create.relation
                                or row.get("source_atomic_information_id")
                                != atomic_id
                            ):
                                return False
                        elif (
                            end is None
                            or identity != end.relationship_id
                            or previous.get("valid_to") is not None
                            or row.get("valid_to") is None
                        ):
                            return False
                    elif collection == "external_identity_mappings":
                        if previous is not None or row.get(
                            "object_id"
                        ) not in created_object_ids:
                            return False
            return True
        except (KeyError, TypeError, ValueError, WechatDigestError):
            return False

    def _governance_business_tree_fingerprint(
        self, repository: SQLiteWorldModelRepository
    ) -> str:
        current = self.information_store.list_atomic_information()
        payload = {
            "information_history": [
                asdict(revision)
                for item in current
                for revision in self.information_store.list_revisions(
                    item.atomic_information_id
                )
            ],
            "proposal_history": [
                asdict(item) for item in self.proposal_store.list_history()
            ],
            "journal": [asdict(item) for item in self.journal.list_changes()],
            "world": {
                "objects": [asdict(item) for item in repository.list_objects()],
                "names": [
                    asdict(name)
                    for item in repository.list_objects()
                    for name in repository.list_names(item.object_id)
                ],
                "roles": [
                    asdict(role)
                    for item in repository.list_objects()
                    for role in repository.list_roles(item.object_id)
                ],
                "lifecycles": [
                    asdict(lifecycle)
                    for item in repository.list_objects()
                    for lifecycle in repository.list_lifecycles(item.object_id)
                ],
                "relationships": [
                    asdict(item)
                    for item in repository.list_relationships(active_only=False)
                ],
                "external_identity_mappings": [
                    asdict(item)
                    for item in repository.list_external_identity_mappings()
                ],
                "apply_receipts": [
                    asdict(item) for item in repository.list_apply_receipts()
                ],
            },
        }
        return _sha256_bytes(_canonical_json(payload).encode("utf-8"))

    def _build_batch_governance_authority_manifest_unlocked(
        self,
        *,
        authority_ref: str,
        completed_atomic_information_ids: Sequence[str],
        remaining_atomic_information_ids: Sequence[str],
    ) -> dict[str, object]:
        run_id = self.run_store.active_run_id()
        if run_id is None:
            raise WechatDigestError("不存在可迁移 Batch Governance 的 active run。")
        plan = self.run_store.plan(run_id)
        receipt = self.run_store.plan_receipt(run_id)
        if self._plan_all_history_upper(plan) is None:
            raise WechatDigestError(
                "Batch Governance migration 只能绑定 frozen campaign。"
            )
        after = WechatCursor.from_dict(plan.get("after_cursor"), "plan.after_cursor")
        checkpoint = self.run_store.checkpoint()
        if checkpoint not in {None, after}:
            raise WechatDigestError(
                "Batch Governance migration checkpoint binding 不一致。"
            )
        status = self.run_store.status(run_id)
        capture, _ = self._load_active_capture_artifacts(
            run_id, plan, status
        )
        if (
            status.get("state") != "failed"
            or status.get("failure_category") != "BrokenPipeError"
            or status.get("checkpoint_published") is not False
        ):
            raise WechatDigestError(
                "Batch Governance migration active stop 边界不匹配。"
            )
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        candidates: list[tuple[str, dict[str, object]]] = []
        for item_id, value in items.items():
            if not isinstance(item_id, str) or not isinstance(value, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            receipt_value = value.get("governance_receipt")
            if receipt_value is None:
                continue
            governance_receipt = _validated_governance_receipt(receipt_value)
            if (
                value.get("state") == "represented"
                and governance_receipt.get("schema_version")
                == LEGACY_GOVERNANCE_RECEIPT_SCHEMA_VERSION
                and governance_receipt.get("phase") == "started"
            ):
                candidates.append((item_id, value))
        if len(candidates) != 1:
            raise WechatDigestError(
                "Batch Governance migration 必须唯一绑定旧 started item。"
            )
        item_id, item = candidates[0]
        representation_id = item.get("representation_id")
        source_id = item.get("source_id")
        if not isinstance(representation_id, str) or not isinstance(source_id, str):
            raise WechatDigestError("Batch Governance migration item binding 损坏。")
        representation = self.representation_repository.get(representation_id)
        verification = self.representation_repository.verify(representation_id)
        source = self.source_repository.get(source_id)
        source_verification = self.source_repository.verify(source_id)
        if (
            not verification.verified
            or not source_verification.verified
            or representation.source_id != source_id
        ):
            raise WechatDigestError(
                "Batch Governance Source/Representation 校验失败。"
            )
        ordered_ids = self._verify_semantic_receipts(
            representation_id, item, recover_missing_item_receipt=True
        )
        completed_ids = tuple(completed_atomic_information_ids)
        remaining_ids = tuple(remaining_atomic_information_ids)
        if (
            len(ordered_ids) != 18
            or completed_ids != ordered_ids[:15]
            or remaining_ids != ordered_ids[15:]
            or len(remaining_ids) != 3
        ):
            raise WechatDigestError(
                "Batch Governance authority IDs 未精确绑定 ordered18。"
            )
        legacy_receipt = _validated_governance_receipt(
            item.get("governance_receipt")
        )
        metrics = _validated_governance_metrics(item.get("governance_metrics"))
        if (
            item.get("atomic_information_ids") != []
            or legacy_receipt.get("atomic_information_fingerprint")
            != _governance_atomic_fingerprint(ordered_ids)
            or metrics.get("app_server_start_count") != 0
            or metrics.get("thread_count") != 15
            or metrics.get("turn_count") != 15
            or metrics.get("timeout_count") != 0
            or metrics.get("failure_count") != 1
            or metrics.get("failure_categories") != {"transport": 1}
        ):
            raise WechatDigestError(
                "Batch Governance legacy receipt binding 损坏。"
            )
        package = (
            self.workspace
            / "02_processing"
            / "information"
            / representation_id
        )
        artifact_inventory: list[dict[str, object]] = []
        for artifact in representation.artifacts:
            raw = self.representation_repository.read_artifact(
                representation_id, artifact.artifact_id
            )
            if (
                _sha256_bytes(raw) != artifact.content_hash
                or len(raw) != artifact.size_bytes
            ):
                raise WechatDigestError(
                    "Batch Governance Representation artifact 漂移。"
                )
            artifact_inventory.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                }
            )
        with SQLiteWorldModelRepository(self.database) as repository:
            completed_bindings = [
                self._governance_effect_binding(repository, atomic_id)
                for atomic_id in completed_ids
            ]
            pristine_bindings = [
                self._governance_effect_binding(repository, atomic_id)
                for atomic_id in remaining_ids
            ]
            for atomic_id, binding in zip(
                remaining_ids, pristine_bindings, strict=True
            ):
                current = self.information_store.get_current(atomic_id)
                if (
                    binding["revision_count"] != 1
                    or binding["proposal_history_count"] != 0
                    or binding["journal_count"] != 0
                    or binding["apply_receipt_count"] != 0
                    or current.related_object_ids
                ):
                    raise WechatDigestError(
                        "Batch Governance remaining3 并非 pristine。"
                    )
            business_tree_fingerprint = (
                self._governance_business_tree_fingerprint(repository)
            )
        semantic_summary = _validated_global_attempt_summary(
            self._semantic_port().global_attempt_summary(representation_id)
        )
        if semantic_summary != {
            "global_attempt_total": 220,
            "global_unknown": 0,
            "next_global_ordinal": 221,
            "absolute_cap": 1000,
        }:
            raise WechatDigestError(
                "Batch Governance migration Semantic ledger 不匹配。"
            )
        window_binding = self._semantic_authority_binding(
            run_id, allow_reviewed_head_extension=True
        )
        activation_binding = {
            "run_id": run_id,
            "plan_fingerprint": _plan_fingerprint(plan),
            "plan_receipt_fingerprint": _sha256_bytes(
                _canonical_json(receipt).encode("utf-8")
            ),
            "status_fingerprint": _sha256_bytes(
                _canonical_json(status).encode("utf-8")
            ),
            "capture_fingerprint": _capture_fingerprint(capture),
            "checkpoint_fingerprint": _sha256_bytes(
                _canonical_json(
                    None if checkpoint is None else checkpoint.to_dict()
                ).encode("utf-8")
            ),
            "item_id": item_id,
            "source_id": source_id,
            "source_manifest_fingerprint": _sha256_bytes(
                _canonical_json(source.to_manifest_dict()).encode("utf-8")
            ),
            "representation_id": representation_id,
            "representation_manifest_fingerprint": _sha256_bytes(
                _canonical_json(representation.to_manifest_dict()).encode("utf-8")
            ),
            "representation_artifact_inventory_fingerprint": _sha256_bytes(
                _canonical_json(artifact_inventory).encode("utf-8")
            ),
            "semantic_package_fingerprint": _package_fingerprint(package),
            "canonical_ordered_atomic_information_ids": list(ordered_ids),
            "legacy_started_receipt_fingerprint": _sha256_bytes(
                _canonical_json(legacy_receipt).encode("utf-8")
            ),
            "legacy_metrics_fingerprint": _sha256_bytes(
                _canonical_json(metrics).encode("utf-8")
            ),
            "semantic_window_binding_fingerprint": _sha256_bytes(
                _canonical_json(asdict(window_binding)).encode("utf-8")
            ),
        }
        manifest: dict[str, object] = {
            "schema_version": BATCH_GOVERNANCE_AUTHORITY_SCHEMA_VERSION,
            "authority_ref": authority_ref,
            "implementation_plan_ref": (
                "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
                "#issuecomment-5353218136"
            ),
            "activation_binding": activation_binding,
            "completed_atomic_information_ids": list(completed_ids),
            "remaining_atomic_information_ids": list(remaining_ids),
            "completed_effect_bindings": completed_bindings,
            "remaining_pristine_bindings": pristine_bindings,
            "business_tree_fingerprint": business_tree_fingerprint,
            "previous_effective_head": (
                "deaee94fe8c87ec84505a7de10d6f8d35eec87a5"
            ),
            "reviewed_git_head": self._semantic_port().reviewed_git_head,
            "semantic_summary": {
                **semantic_summary,
                "last_global_ordinal": 220,
            },
        }
        manifest["manifest_fingerprint"] = _sha256_bytes(
            _canonical_json(manifest).encode("utf-8")
        )
        return _validated_batch_governance_authority_manifest(manifest)

    def build_batch_governance_authority_manifest(
        self,
        *,
        authority_ref: str,
        completed_atomic_information_ids: Sequence[str],
        remaining_atomic_information_ids: Sequence[str],
    ) -> dict[str, object]:
        """Build a read-only candidate that still requires Lead approval."""

        with self.run_store.lock():
            return self._build_batch_governance_authority_manifest_unlocked(
                authority_ref=authority_ref,
                completed_atomic_information_ids=completed_atomic_information_ids,
                remaining_atomic_information_ids=remaining_atomic_information_ids,
            )

    def activate_batch_governance(
        self, *, authority_ref: str, authority_manifest_file: Path
    ) -> dict[str, object]:
        """Activate the exact Issue #135 migration without calling Providers."""

        if (
            re.fullmatch(
                r"https://github\.com/leevi2010-cursor/ArcheOS/issues/135"
                r"#issuecomment-[0-9]+",
                authority_ref,
            )
            is None
            or authority_ref
            == (
                "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
                "#issuecomment-5353218136"
            )
        ):
            raise WechatDigestError(
                "Batch Governance migration authority ref 不匹配。"
            )
        (
            authority_manifest,
            authority_manifest_raw_fingerprint,
        ) = _read_private_json_manifest(
            authority_manifest_file
        )
        authority_manifest = _validated_batch_governance_authority_manifest(
            authority_manifest
        )
        if authority_manifest["authority_ref"] != authority_ref:
            raise WechatDigestError(
                "Batch Governance authority manifest ref 不匹配。"
            )
        with self.run_store.lock():
            activation_binding = authority_manifest["activation_binding"]
            manifest_run_id = activation_binding.get("run_id")
            active_run_id = self.run_store.active_run_id()
            if (
                not isinstance(manifest_run_id, str)
                or (active_run_id is not None and active_run_id != manifest_run_id)
            ):
                raise WechatDigestError(
                    "Batch Governance migration run binding 不匹配。"
                )
            run_id = active_run_id or manifest_run_id
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Batch Governance migration 只能绑定 frozen campaign。"
                )
            after = WechatCursor.from_dict(
                plan.get("after_cursor"), "plan.after_cursor"
            )
            upper = WechatCursor.from_dict(
                plan.get("upper_bound"), "plan.upper_bound"
            )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(run_id, plan, status)
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            migrated = [
                (item_id, value)
                for item_id, value in items.items()
                if isinstance(item_id, str)
                and isinstance(value, dict)
                and value.get("governance_migration") is not None
            ]
            checkpoint = self.run_store.checkpoint()
            if not migrated:
                if (
                    active_run_id != run_id
                    or checkpoint not in {None, after}
                    or status.get("checkpoint_published") is not False
                ):
                    raise WechatDigestError(
                        "Batch Governance migration checkpoint binding 不一致。"
                    )
            elif (
                status.get("checkpoint_published") is False
                and checkpoint not in {None, after}
            ) or (
                status.get("checkpoint_published") is True
                and checkpoint != upper
            ):
                raise WechatDigestError(
                    "Batch Governance migration readback checkpoint 漂移。"
                )
            migration_pristine_phase = False
            if migrated:
                if len(migrated) != 1:
                    raise WechatDigestError(
                        "Batch Governance migration 已存在但状态不一致。"
                    )
                item_id, item = migrated[0]
                migration = _validated_governance_migration(
                    item.get("governance_migration")
                )
                governance_receipt = _validated_governance_receipt(
                    item.get("governance_receipt")
                )
                clean_status = (
                    status.get("state")
                    in {"processing", "converged", "completed"}
                    and status.get("failure_category") is None
                )
                recoverable_failed_status = (
                    status.get("state") == "failed"
                    and isinstance(status.get("failure_category"), str)
                    and governance_receipt.get("schema_version")
                    == GOVERNANCE_RECEIPT_SCHEMA_VERSION
                    and governance_receipt.get("phase")
                    in {"interpreted", "applying", "applied", "completed"}
                )
                if not (clean_status or recoverable_failed_status):
                    raise WechatDigestError(
                        "Batch Governance migration 已存在但状态不一致。"
                    )
                with SQLiteWorldModelRepository(self.database) as repository:
                    completed_effects_match = [
                        self._governance_effect_binding(repository, atomic_id)
                        for atomic_id in migration[
                            "completed_atomic_information_ids"
                        ]
                    ] == authority_manifest["completed_effect_bindings"]
                    remaining_effects = [
                        self._governance_effect_binding(repository, atomic_id)
                        for atomic_id in migration[
                            "remaining_atomic_information_ids"
                        ]
                    ]
                migration_pristine_phase = (
                    governance_receipt.get("schema_version")
                    == LEGACY_GOVERNANCE_RECEIPT_SCHEMA_VERSION
                    and governance_receipt.get("phase") == "started"
                )
                if migration_pristine_phase:
                    remaining_effects_match = (
                        remaining_effects
                        == authority_manifest["remaining_pristine_bindings"]
                    )
                else:
                    remaining_ids = migration[
                        "remaining_atomic_information_ids"
                    ]
                    next_index = governance_receipt.get("next_index")
                    if (
                        governance_receipt.get("schema_version")
                        != GOVERNANCE_RECEIPT_SCHEMA_VERSION
                        or governance_receipt.get(
                            "batch_atomic_information_ids"
                        )
                        != remaining_ids
                        or not isinstance(next_index, int)
                    ):
                        raise WechatDigestError(
                            "Batch Governance migration batch receipt 漂移。"
                        )
                    applied_fingerprints = governance_receipt[
                        "applied_effect_fingerprints"
                    ]
                    remaining_effects_match = (
                        remaining_effects[next_index:]
                        == authority_manifest["remaining_pristine_bindings"][
                            next_index:
                        ]
                        and all(
                            remaining_effects[index]["effect_fingerprint"]
                            == applied_fingerprints[index]
                            for index in range(next_index)
                        )
                    )
                if (
                    migration.get("authority_ref") != authority_ref
                    or migration.get("authority_manifest_fingerprint")
                    != authority_manifest.get("manifest_fingerprint")
                    or migration.get("authority_manifest_raw_fingerprint")
                    != authority_manifest_raw_fingerprint
                    or not completed_effects_match
                    or not remaining_effects_match
                    or self._governance_effect_fingerprint(
                        migration["completed_atomic_information_ids"]
                    )
                    != migration["legacy_effect_fingerprint"]
                    or (
                        migration_pristine_phase
                        and self._governance_effect_fingerprint(
                            migration["remaining_atomic_information_ids"]
                        )
                        != migration["pristine_remaining_fingerprint"]
                    )
                ):
                    raise WechatDigestError(
                        "Batch Governance migration durable state 漂移。"
                    )
            else:
                expected_manifest = (
                    self._build_batch_governance_authority_manifest_unlocked(
                        authority_ref=authority_ref,
                        completed_atomic_information_ids=(
                            authority_manifest[
                                "completed_atomic_information_ids"
                            ]
                        ),
                        remaining_atomic_information_ids=(
                            authority_manifest[
                                "remaining_atomic_information_ids"
                            ]
                        ),
                    )
                )
                if expected_manifest != authority_manifest:
                    raise WechatDigestError(
                        "Batch Governance authority manifest 与现场不一致。"
                    )
                if (
                    status.get("state") != "failed"
                    or status.get("failure_category") != "BrokenPipeError"
                ):
                    raise WechatDigestError(
                        "Batch Governance migration active stop 边界不匹配。"
                    )
                candidates: list[tuple[str, dict[str, object]]] = []
                for item_id, value in items.items():
                    if not isinstance(item_id, str) or not isinstance(value, dict):
                        raise WechatDigestError("微信运行状态 items 损坏。")
                    receipt_value = value.get("governance_receipt")
                    if receipt_value is None:
                        continue
                    receipt = _validated_governance_receipt(receipt_value)
                    if (
                        value.get("state") == "represented"
                        and receipt.get("schema_version")
                        == LEGACY_GOVERNANCE_RECEIPT_SCHEMA_VERSION
                        and receipt.get("phase") == "started"
                    ):
                        candidates.append((item_id, value))
                if len(candidates) != 1:
                    raise WechatDigestError(
                        "Batch Governance migration 必须唯一绑定旧 started item。"
                    )
                item_id, item = candidates[0]
                representation_id = item.get("representation_id")
                if not isinstance(representation_id, str):
                    raise WechatDigestError(
                        "Batch Governance migration Representation 损坏。"
                    )
                representation = self.representation_repository.get(
                    representation_id
                )
                privacy = self.privacy_gate.evaluate(
                    self._representation_texts(representation_id),
                    semantic_completeness_known=(
                        representation.status == "complete"
                        and representation.completeness == 1.0
                        and not representation.warnings
                    ),
                )
                if privacy.route != "approved" or privacy.categories:
                    raise WechatDigestError(
                        "Batch Governance migration privacy binding 不允许恢复。"
                    )
                ordered_ids = self._verify_semantic_receipts(
                    representation_id,
                    item,
                    recover_missing_item_receipt=True,
                )
                receipt = _validated_governance_receipt(
                    item.get("governance_receipt")
                )
                metrics = _validated_governance_metrics(
                    item.get("governance_metrics")
                )
                if (
                    len(ordered_ids) != 18
                    or item.get("atomic_information_ids") != []
                    or receipt.get("atomic_information_fingerprint")
                    != _governance_atomic_fingerprint(ordered_ids)
                    or metrics.get("app_server_start_count") != 0
                    or metrics.get("thread_count") != 15
                    or metrics.get("turn_count") != 15
                    or metrics.get("timeout_count") != 0
                    or metrics.get("failure_count") != 1
                    or metrics.get("failure_categories") != {"transport": 1}
                ):
                    raise WechatDigestError(
                        "Batch Governance migration 旧15条证据不完整。"
                    )
                completed_ids = tuple(
                    authority_manifest["completed_atomic_information_ids"]
                )
                remaining_ids = tuple(
                    authority_manifest["remaining_atomic_information_ids"]
                )
                if completed_ids + remaining_ids != ordered_ids:
                    raise WechatDigestError(
                        "Batch Governance authority IDs 顺序不一致。"
                    )
                unresolved = self.proposal_store.list_unresolved()
                journal = self.journal.list_changes()
                if any(
                    self.information_store.get_current(atomic_id).revision_number
                    != 1
                    for atomic_id in remaining_ids
                ) or any(
                    proposal.atomic_information_id in set(remaining_ids)
                    for proposal in unresolved
                ) or any(
                    record.atomic_information_id in set(remaining_ids)
                    for record in journal
                ):
                    raise WechatDigestError(
                        "Batch Governance migration 剩余3条无法唯一证明。"
                    )
                migration = {
                    "schema_version": GOVERNANCE_MIGRATION_SCHEMA_VERSION,
                    "phase": "activated",
                    "authority_ref": authority_ref,
                    "implementation_plan_ref": authority_manifest[
                        "implementation_plan_ref"
                    ],
                    "authority_manifest_fingerprint": authority_manifest[
                        "manifest_fingerprint"
                    ],
                    "authority_manifest_raw_fingerprint": (
                        authority_manifest_raw_fingerprint
                    ),
                    "legacy_governance_receipt": receipt,
                    "atomic_information_fingerprint": (
                        _governance_atomic_fingerprint(ordered_ids)
                    ),
                    "ordered_atomic_information_ids": list(ordered_ids),
                    "completed_atomic_information_ids": list(completed_ids),
                    "remaining_atomic_information_ids": list(remaining_ids),
                    "legacy_effect_fingerprint": (
                        self._governance_effect_fingerprint(completed_ids)
                    ),
                    "pristine_remaining_fingerprint": (
                        self._governance_effect_fingerprint(remaining_ids)
                    ),
                    "activation_business_tree_fingerprint": (
                        authority_manifest["business_tree_fingerprint"]
                    ),
                    "pending_human": any(
                        proposal.atomic_information_id in set(completed_ids)
                        for proposal in unresolved
                    ),
                    "context_object_ids": sorted(
                        {
                            object_id
                            for atomic_id in completed_ids
                            for object_id in self.information_store.get_current(
                                atomic_id
                            ).related_object_ids
                        }
                    ),
                    "semantic_continuation_fingerprint": "",
                }

            representation_id = item.get("representation_id")
            if not isinstance(representation_id, str):
                raise WechatDigestError(
                    "Batch Governance migration Representation 损坏。"
                )
            with SQLiteWorldModelRepository(self.database) as repository:
                activation_business_tree_fingerprint = (
                    self._governance_business_tree_fingerprint(repository)
                )
            if (
                (not migrated or migration_pristine_phase)
                and activation_business_tree_fingerprint
                != authority_manifest.get("business_tree_fingerprint")
            ):
                raise WechatDigestError(
                    "Batch Governance activation 前业务树漂移。"
                )
            summary = _validated_global_attempt_summary(
                self._semantic_port().global_attempt_summary(
                    representation_id
                )
            )
            if summary != {
                "global_attempt_total": 220,
                "global_unknown": 0,
                "next_global_ordinal": 221,
                "absolute_cap": 1000,
            }:
                raise WechatDigestError(
                    "Batch Governance migration Semantic ledger 不匹配。"
                )
            if migrated and not migration_pristine_phase:
                campaign = self._semantic_port().global_campaign_binding()
                if (
                    campaign is None
                    or campaign.reviewed_git_head
                    != authority_manifest["reviewed_git_head"]
                ):
                    raise WechatDigestError(
                        "Batch Governance reviewed head readback 漂移。"
                    )
                return migration
            continuation = (
                self._semantic_port().install_batch_governance_continuation(
                    window_binding=self._semantic_authority_binding(
                        run_id, allow_reviewed_head_extension=True
                    ),
                    authority_ref=authority_ref,
                )
            )
            continuation_fingerprint = continuation.get(
                "continuation_fingerprint"
            )
            if (
                not _sha256_value(continuation_fingerprint)
                or continuation.get("activation_total") != 220
                or continuation.get("activation_unknown_count") != 0
                or continuation.get("next_global_ordinal") != 221
                or continuation.get("absolute_cap") != 1000
            ):
                raise WechatDigestError(
                    "Batch Governance migration Semantic continuation 读回失败。"
                )
            if migrated:
                if migration.get("semantic_continuation_fingerprint") != (
                    continuation_fingerprint
                ):
                    raise WechatDigestError(
                        "Batch Governance migration authority binding 漂移。"
                    )
                return migration

            with SQLiteWorldModelRepository(self.database) as repository:
                if self._governance_business_tree_fingerprint(repository) != (
                    authority_manifest["business_tree_fingerprint"]
                ):
                    raise WechatDigestError(
                        "Batch Governance continuation 改变了业务树。"
                    )

            migration["semantic_continuation_fingerprint"] = (
                continuation_fingerprint
            )
            without_fingerprint = dict(migration)
            migration["migration_fingerprint"] = _sha256_bytes(
                _canonical_json(without_fingerprint).encode("utf-8")
            )
            _validated_governance_migration(migration)
            current = self.run_store.status(run_id)
            current_items = current.get("items")
            if not isinstance(current_items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            updated_item = {
                **self._item(current_items, item_id),
                "privacy_route": "approved",
                "privacy_categories": [],
                "governance_migration": migration,
            }
            updated_items = dict(current_items)
            updated_items[item_id] = updated_item
            current["items"] = updated_items
            current["state"] = "processing"
            current["failure_category"] = None
            current["updated_at"] = self.clock()
            self.run_store.update_status(run_id, current)
            observed = self.run_store.status(run_id)
            observed_items = observed.get("items")
            with SQLiteWorldModelRepository(self.database) as repository:
                business_tree_unchanged = (
                    self._governance_business_tree_fingerprint(repository)
                    == authority_manifest["business_tree_fingerprint"]
                )
            if (
                observed.get("state") != "processing"
                or observed.get("failure_category") is not None
                or not business_tree_unchanged
                or not isinstance(observed_items, dict)
                or _validated_governance_migration(
                    self._item(observed_items, item_id).get(
                        "governance_migration"
                    )
                )
                != migration
            ):
                raise WechatDigestError(
                    "Batch Governance migration activation 读回失败。"
                )
            return migration

    def _unknown_resolution_digest_binding(
        self,
        *,
        run_id: str,
        plan: Mapping[str, object],
        item_id: str,
        item: Mapping[str, object],
    ) -> dict[str, object]:
        representation_id = item.get("representation_id")
        source_id = item.get("source_id")
        if not isinstance(representation_id, str) or not isinstance(source_id, str):
            raise WechatDigestError(
                "Semantic unknown recovery item identity 损坏。"
            )
        representation = self.representation_repository.get(representation_id)
        verification = self.representation_repository.verify(representation_id)
        if (
            not verification.verified
            or representation.source_id != source_id
        ):
            raise WechatDigestError(
                "Semantic unknown recovery Representation 校验失败。"
            )
        manifest = representation.to_manifest_dict()
        artifact_inventory: list[dict[str, object]] = []
        for artifact in representation.artifacts:
            raw = self.representation_repository.read_artifact(
                representation_id, artifact.artifact_id
            )
            if (
                _sha256_bytes(raw) != artifact.content_hash
                or len(raw) != artifact.size_bytes
            ):
                raise WechatDigestError(
                    "Semantic unknown recovery artifact bytes 漂移。"
                )
            artifact_inventory.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                }
            )
        receipt = self.run_store.plan_receipt(run_id)
        return {
            "run_id": run_id,
            "plan_fingerprint": _plan_fingerprint(plan),
            "plan_receipt_fingerprint": _sha256_bytes(
                _canonical_json(receipt).encode("utf-8")
            ),
            "item_id": item_id,
            "source_id": source_id,
            "representation_id": representation_id,
            "representation_manifest": manifest,
            "representation_artifact_inventory_fingerprint": _sha256_bytes(
                _canonical_json(artifact_inventory).encode("utf-8")
            ),
        }

    def _commit_failed_closed_item(
        self,
        *,
        run_id: str,
        plan: Mapping[str, object],
        item_id: str,
        resolution_id: str,
        expected_digest_binding: Mapping[str, object],
    ) -> tuple[str, Mapping[str, object]]:
        current = self.run_store.status(run_id)
        items = current.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        item = self._item(items, item_id)
        binding = self._unknown_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
        )
        if binding != dict(expected_digest_binding):
            raise WechatDigestError(
                "Semantic unknown recovery Representation 首写前发生漂移。"
            )
        representation_id = str(binding["representation_id"])
        package = (
            self.workspace
            / "02_processing"
            / "information"
            / representation_id
        )
        expected_failure = {
            "resolution_id": resolution_id,
            "global_ordinal": 166,
            "failure_category": "runtime_nonzero_exit",
            "result_present": False,
            "preserved_but_unabsorbed": True,
        }
        if (
            item.get("state") not in {"represented", "failed_closed"}
            or os.path.lexists(package)
            or item.get("atomic_information_ids") != []
            or item.get("pending_human") is not False
            or item.get("context_object_ids") != []
            or item.get("privacy_route") not in {None, "approved"}
            or item.get("state") == "failed_closed"
            and item.get("semantic_failure") != expected_failure
        ):
            raise WechatDigestError(
                "Semantic unknown recovery item 不满足 preserved-but-unabsorbed 边界。"
            )
        if any(
            revision.origin_source_id == binding["source_id"]
            for revision in self.information_store.list_atomic_information()
        ):
            raise WechatDigestError(
                "Semantic unknown recovery item 已存在 Durable Atomic Information。"
            )
        if item.get("state") == "represented":
            updated_item = {
                **item,
                "state": "failed_closed",
                "privacy_route": "approved",
                "privacy_categories": [],
                "atomic_information_ids": [],
                "pending_human": False,
                "context_object_ids": [],
                "semantic_failure": expected_failure,
            }
            updated_items = dict(items)
            updated_items[item_id] = updated_item
            updated_status = {
                **current,
                "items": updated_items,
                "state": "processing",
                "failure_category": None,
                "updated_at": self.clock(),
            }
            self.run_store.update_status(run_id, updated_status)
            readback = self.run_store.status(run_id)
            if readback != updated_status:
                raise WechatDigestError(
                    "Semantic unknown recovery failed_closed 状态读回失败。"
                )
            current = readback
            items = current["items"]
            assert isinstance(items, dict)
            item = self._item(items, item_id)
        final_binding = self._unknown_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
        )
        if final_binding != dict(expected_digest_binding):
            raise WechatDigestError(
                "Semantic unknown recovery Representation status 后发生漂移。"
            )
        return (
            _sha256_bytes(_canonical_json(item).encode("utf-8")),
            final_binding,
        )

    def resolve_semantic_unknown(
        self,
        *,
        authority_manifest_file: Path,
    ) -> dict[str, object]:
        """Resolve the approved unknown item with zero Semantic/Governance calls."""

        try:
            manifest = json.loads(Path(authority_manifest_file).read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatDigestError(
                "Semantic unknown recovery authority manifest 不可读。"
            ) from exc
        digest = manifest.get("digest") if isinstance(manifest, dict) else None
        item_id = digest.get("item_id") if isinstance(digest, dict) else None
        if not isinstance(item_id, str):
            raise WechatDigestError(
                "Semantic unknown recovery authority manifest binding 损坏。"
            )
        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可恢复 Semantic unknown 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic unknown recovery 只能绑定 frozen campaign。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(
                run_id,
                plan,
                status,
                allow_pending_unknown_resolution=True,
            )
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            item = self._item(items, item_id)
            binding = self._unknown_resolution_digest_binding(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=item,
            )
            resolution = self._semantic_port().resolve_unknown(
                authority_manifest_file=authority_manifest_file,
                digest_binding=binding,
                commit_failed_closed_status=lambda resolution_id: (
                    self._commit_failed_closed_item(
                        run_id=run_id,
                        plan=plan,
                        item_id=item_id,
                        resolution_id=resolution_id,
                        expected_digest_binding=binding,
                    )
                ),
            )
            final_status = self.run_store.status(run_id)
            final_items = final_status.get("items")
            if not isinstance(final_items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            final_item = self._item(final_items, item_id)
            self._verify_failed_closed_item(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=final_item,
            )
            return resolution

    def _commit_timeout_212_failed_closed_item(
        self,
        *,
        run_id: str,
        plan: Mapping[str, object],
        item_id: str,
        resolution_id: str,
        expected_digest_binding: Mapping[str, object],
    ) -> tuple[str, Mapping[str, object]]:
        current = self.run_store.status(run_id)
        items = current.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        item = self._item(items, item_id)
        binding = self._unknown_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
        )
        if binding != dict(expected_digest_binding):
            raise WechatDigestError(
                "Semantic ordinal212 recovery Representation 首写前发生漂移。"
            )
        representation_id = str(binding["representation_id"])
        package = (
            self.workspace
            / "02_processing"
            / "information"
            / representation_id
        )
        expected_failure = {
            "resolution_id": resolution_id,
            "global_ordinal": 212,
            "failure_category": "timeout",
            "result_present": False,
            "preserved_but_unabsorbed": True,
        }
        if (
            item.get("state") not in {"represented", "failed_closed"}
            or os.path.lexists(package)
            or item.get("atomic_information_ids") != []
            or item.get("pending_human") is not False
            or item.get("context_object_ids") != []
            or item.get("privacy_route") not in {None, "approved"}
            or item.get("state") == "failed_closed"
            and item.get("semantic_failure") != expected_failure
        ):
            raise WechatDigestError(
                "Semantic ordinal212 recovery item 不满足 preserved-but-unabsorbed 边界。"
            )
        if any(
            revision.origin_source_id == binding["source_id"]
            for revision in self.information_store.list_atomic_information()
        ):
            raise WechatDigestError(
                "Semantic ordinal212 recovery item 已存在 Durable Atomic Information。"
            )
        if item.get("state") == "represented":
            updated_item = {
                **item,
                "state": "failed_closed",
                "privacy_route": "approved",
                "privacy_categories": [],
                "atomic_information_ids": [],
                "pending_human": False,
                "context_object_ids": [],
                "semantic_failure": expected_failure,
            }
            updated_items = dict(items)
            updated_items[item_id] = updated_item
            updated_status = {
                **current,
                "items": updated_items,
                "state": "processing",
                "failure_category": None,
                "updated_at": self.clock(),
            }
            self.run_store.update_status(run_id, updated_status)
            readback = self.run_store.status(run_id)
            if readback != updated_status:
                raise WechatDigestError(
                    "Semantic ordinal212 failed_closed 状态读回失败。"
                )
            items = readback["items"]
            assert isinstance(items, dict)
            item = self._item(items, item_id)
        final_binding = self._unknown_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
        )
        if final_binding != dict(expected_digest_binding):
            raise WechatDigestError(
                "Semantic ordinal212 recovery status 后发生漂移。"
            )
        return (
            _sha256_bytes(_canonical_json(item).encode("utf-8")),
            final_binding,
        )

    def _attempt_resolution_digest_binding(
        self,
        *,
        run_id: str,
        plan: Mapping[str, object],
        item_id: str,
        item: Mapping[str, object],
        pre_status_fingerprint: str,
    ) -> dict[str, object]:
        terminal = self._attempt_resolution_terminal_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
            pre_status_fingerprint=pre_status_fingerprint,
        )
        checkpoint = self.run_store.checkpoint()
        with SQLiteWorldModelRepository(self.database) as repository:
            business_tree_fingerprint = (
                self._governance_business_tree_fingerprint(repository)
            )
        return {
            **terminal,
            "checkpoint_fingerprint": _sha256_bytes(
                _canonical_json(
                    checkpoint.to_dict() if checkpoint is not None else None
                ).encode("utf-8")
            ),
            "business_tree_fingerprint": business_tree_fingerprint,
        }

    def _attempt_resolution_terminal_binding(
        self,
        *,
        run_id: str,
        plan: Mapping[str, object],
        item_id: str,
        item: Mapping[str, object],
        pre_status_fingerprint: str,
    ) -> dict[str, object]:
        base = self._unknown_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
        )
        status = self.run_store.status(run_id)
        failure = item.get("semantic_failure")
        if item.get("state") == "represented":
            observed_pre_status_fingerprint = _sha256_bytes(
                _canonical_json(status).encode("utf-8")
            )
        elif isinstance(failure, dict):
            observed_pre_status_fingerprint = failure.get(
                "pre_status_fingerprint"
            )
        else:
            observed_pre_status_fingerprint = None
        if observed_pre_status_fingerprint != pre_status_fingerprint:
            raise WechatDigestError(
                "Semantic attempt resolution status binding 漂移。"
            )
        capture_receipt = self.run_store.capture_receipt(run_id)
        source = self.source_repository.get(str(base["source_id"]))
        return {
            **base,
            "pre_status_fingerprint": pre_status_fingerprint,
            "capture_receipt_fingerprint": _sha256_bytes(
                _canonical_json(capture_receipt).encode("utf-8")
            ),
            "source_fingerprint": _sha256_bytes(
                _canonical_json(asdict(source)).encode("utf-8")
            ),
        }

    def _commit_attempt_failed_closed_item(
        self,
        *,
        run_id: str,
        plan: Mapping[str, object],
        item_id: str,
        resolution_id: str,
        global_ordinal: int,
        pre_status_fingerprint: str,
        expected_digest_binding: Mapping[str, object],
    ) -> tuple[str, Mapping[str, object]]:
        if self.run_store.plan(run_id) != dict(plan):
            raise WechatDigestError(
                "Semantic attempt resolution plan 首写前发生漂移。"
            )
        current = self.run_store.status(run_id)
        items = current.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        item = self._item(items, item_id)
        binding = self._attempt_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
            pre_status_fingerprint=pre_status_fingerprint,
        )
        if binding != dict(expected_digest_binding):
            raise WechatDigestError(
                "Semantic attempt resolution 首写前发生漂移。"
            )
        representation_id = str(binding["representation_id"])
        package = (
            self.workspace / "02_processing" / "information" / representation_id
        )
        expected_failure = {
            "resolution_id": resolution_id,
            "global_ordinal": global_ordinal,
            "failure_category": "timeout",
            "result_present": False,
            "diagnostic_persistence_status": "failed",
            "process_cleanup_status": "verified",
            "preserved_but_unabsorbed": True,
            "pre_status_fingerprint": pre_status_fingerprint,
        }
        if (
            item.get("state") not in {"represented", "failed_closed"}
            or os.path.lexists(package)
            or item.get("atomic_information_ids") != []
            or item.get("pending_human") is not False
            or item.get("context_object_ids") != []
            or item.get("privacy_route") not in {None, "approved"}
            or item.get("state") == "failed_closed"
            and item.get("semantic_failure") != expected_failure
        ):
            raise WechatDigestError(
                "Semantic attempt resolution item 不满足 preserved-but-unabsorbed 边界。"
            )
        if any(
            revision.origin_source_id == binding["source_id"]
            for revision in self.information_store.list_atomic_information()
        ):
            raise WechatDigestError(
                "Semantic attempt resolution item 已存在 Durable Atomic Information。"
            )
        if item.get("state") == "represented":
            updated_item = {
                **item,
                "state": "failed_closed",
                "privacy_route": "approved",
                "privacy_categories": [],
                "atomic_information_ids": [],
                "pending_human": False,
                "context_object_ids": [],
                "semantic_failure": expected_failure,
            }
            updated_items = dict(items)
            updated_items[item_id] = updated_item
            updated_status = {
                **current,
                "items": updated_items,
                "state": "processing",
                "failure_category": None,
                "updated_at": self.clock(),
            }
            self.run_store.update_status(run_id, updated_status)
            readback = self.run_store.status(run_id)
            if readback != updated_status:
                raise WechatDigestError(
                    "Semantic attempt resolution failed_closed 状态读回失败。"
                )
            items = readback["items"]
            assert isinstance(items, dict)
            item = self._item(items, item_id)
        final_binding = self._attempt_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
            pre_status_fingerprint=pre_status_fingerprint,
        )
        if final_binding != dict(expected_digest_binding):
            raise WechatDigestError(
                "Semantic attempt resolution status 后发生漂移。"
            )
        return (
            _sha256_bytes(_canonical_json(item).encode("utf-8")),
            final_binding,
        )

    def resolve_semantic_attempt(
        self,
        *,
        authority_manifest_file: Path,
    ) -> dict[str, object]:
        """Resolve one Lead-approved tail attempt with zero Providers."""

        try:
            manifest = json.loads(
                Path(authority_manifest_file).read_text("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatDigestError(
                "Semantic attempt resolution authority manifest 不可读。"
            ) from exc
        digest = manifest.get("digest") if isinstance(manifest, dict) else None
        item_id = digest.get("item_id") if isinstance(digest, dict) else None
        pre_status_fingerprint = (
            digest.get("pre_status_fingerprint")
            if isinstance(digest, dict)
            else None
        )
        if (
            not isinstance(item_id, str)
            or not isinstance(pre_status_fingerprint, str)
        ):
            raise WechatDigestError(
                "Semantic attempt resolution authority binding 损坏。"
            )
        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可恢复 Semantic attempt 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic attempt resolution 只能绑定 frozen campaign。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(
                run_id,
                plan,
                status,
                allow_pending_unknown_resolution=True,
            )
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            item = self._item(items, item_id)
            already_closed = item.get("state") == "failed_closed"
            if already_closed:
                if (
                    status.get("state") != "processing"
                    or status.get("failure_category") is not None
                ):
                    raise WechatDigestError(
                        "Semantic attempt resolution 恢复状态损坏。"
                    )
            elif (
                status.get("state") != "failed"
                or status.get("failure_category") != "SemanticHandoffError"
                or item.get("state") != "represented"
            ):
                raise WechatDigestError(
                    "当前运行不是可恢复的 Semantic attempt failure。"
                )
            other_states = [
                value.get("state")
                for key, value in items.items()
                if key != item_id and isinstance(value, dict)
            ]
            if any(
                state not in TERMINAL_ITEM_STATES | {"planned"}
                for state in other_states
            ):
                raise WechatDigestError(
                    "Semantic attempt resolution 存在另一个未收敛 item。"
                )
            binding = self._attempt_resolution_digest_binding(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=item,
                pre_status_fingerprint=pre_status_fingerprint,
            )
            resolution = self._semantic_port().resolve_attempt(
                authority_manifest_file=authority_manifest_file,
                digest_binding=binding,
                commit_failed_closed_status=(
                    lambda resolution_id, global_ordinal: (
                        self._commit_attempt_failed_closed_item(
                            run_id=run_id,
                            plan=plan,
                            item_id=item_id,
                            resolution_id=resolution_id,
                            global_ordinal=global_ordinal,
                            pre_status_fingerprint=pre_status_fingerprint,
                            expected_digest_binding=binding,
                        )
                    )
                ),
            )
            final_status = self.run_store.status(run_id)
            final_items = final_status.get("items")
            if not isinstance(final_items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            self._verify_failed_closed_item(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=self._item(final_items, item_id),
            )
            return resolution

    def build_semantic_attempt_resolution_manifest(
        self,
        *,
        candidate_file: Path,
        authority_ref: str,
        observed_at: str,
    ) -> dict[str, object]:
        """Build one private candidate from exact durable state, Provider0."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可生成 Semantic attempt candidate 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic attempt candidate 只能绑定 frozen campaign。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(
                run_id,
                plan,
                status,
                allow_pending_unknown_resolution=True,
            )
            items = status.get("items")
            if (
                status.get("state") != "failed"
                or status.get("failure_category") != "SemanticHandoffError"
                or not isinstance(items, dict)
            ):
                raise WechatDigestError(
                    "当前运行不是可生成 candidate 的 Semantic failure。"
                )
            represented = [
                item_id
                for item_id, value in items.items()
                if isinstance(value, dict) and value.get("state") == "represented"
            ]
            if len(represented) != 1:
                raise WechatDigestError(
                    "Semantic attempt candidate 必须唯一绑定 represented item。"
                )
            item_id = represented[0]
            if any(
                not isinstance(value, dict)
                or value.get("state")
                not in TERMINAL_ITEM_STATES | {"planned"}
                for key, value in items.items()
                if key != item_id
            ):
                raise WechatDigestError(
                    "Semantic attempt candidate 存在另一个未收敛 item。"
                )
            pre_status_fingerprint = _sha256_bytes(
                _canonical_json(status).encode("utf-8")
            )
            binding = self._attempt_resolution_digest_binding(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=self._item(items, item_id),
                pre_status_fingerprint=pre_status_fingerprint,
            )
            candidate = self._semantic_port().build_attempt_resolution_manifest(
                candidate_file=candidate_file,
                authority_ref=authority_ref,
                observed_at=observed_at,
                digest_binding=binding,
            )
            if self.run_store.status(run_id) != status:
                raise WechatDigestError(
                    "Semantic attempt candidate 生成期间状态发生漂移。"
                )
            return candidate

    def resolve_semantic_timeout_212(
        self,
        *,
        authority_manifest_file: Path,
    ) -> dict[str, object]:
        """Resolve the approved ordinal-212 timeout with zero Providers."""

        try:
            manifest = json.loads(
                Path(authority_manifest_file).read_text("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatDigestError(
                "Semantic ordinal212 authority manifest 不可读。"
            ) from exc
        digest = manifest.get("digest") if isinstance(manifest, dict) else None
        item_id = digest.get("item_id") if isinstance(digest, dict) else None
        if not isinstance(item_id, str):
            raise WechatDigestError(
                "Semantic ordinal212 authority manifest binding 损坏。"
            )
        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可恢复 Semantic ordinal212 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Semantic ordinal212 recovery 只能绑定 frozen campaign。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(
                run_id,
                plan,
                status,
                allow_pending_unknown_resolution=True,
            )
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            item = self._item(items, item_id)
            binding = self._unknown_resolution_digest_binding(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=item,
            )
            resolution = self._semantic_port().resolve_timeout_212(
                authority_manifest_file=authority_manifest_file,
                digest_binding=binding,
                commit_failed_closed_status=lambda resolution_id: (
                    self._commit_timeout_212_failed_closed_item(
                        run_id=run_id,
                        plan=plan,
                        item_id=item_id,
                        resolution_id=resolution_id,
                        expected_digest_binding=binding,
                    )
                ),
            )
            final_status = self.run_store.status(run_id)
            final_items = final_status.get("items")
            if not isinstance(final_items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            self._verify_failed_closed_item(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=self._item(final_items, item_id),
            )
            return resolution

    def seal_governance_timeout(self) -> dict[str, object]:
        """Seal one timed-out Governance item without any Provider retry."""

        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可封存 Governance timeout 的 active run。"
                )
            plan = self.run_store.plan(run_id)
            if self._plan_all_history_upper(plan) is None:
                raise WechatDigestError(
                    "Governance timeout 封存只能绑定 frozen campaign。"
                )
            after = WechatCursor.from_dict(
                plan.get("after_cursor"), "plan.after_cursor"
            )
            checkpoint = self.run_store.checkpoint()
            if checkpoint is not None and checkpoint != after:
                raise WechatDigestError(
                    "Governance timeout 封存的 checkpoint binding 不一致。"
                )
            status = self.run_store.status(run_id)
            self._load_active_capture_artifacts(run_id, plan, status)
            if status.get("checkpoint_published") is not False:
                raise WechatDigestError(
                    "Governance timeout 封存不得发生在 checkpoint 推进后。"
                )
            items = status.get("items")
            if not isinstance(items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            candidates: list[tuple[str, dict[str, object]]] = []
            for item_id, value in items.items():
                if not isinstance(item_id, str) or not isinstance(value, dict):
                    raise WechatDigestError("微信运行状态 items 损坏。")
                receipt_value = value.get("governance_receipt")
                if receipt_value is not None and (
                    _validated_governance_receipt(receipt_value).get("phase")
                    == "started"
                ):
                    candidates.append((item_id, value))
                elif value.get("state") not in TERMINAL_ITEM_STATES | {"planned"}:
                    raise WechatDigestError(
                        "Governance timeout 封存存在另一个未收敛 item。"
                    )
            if len(candidates) != 1:
                raise WechatDigestError(
                    "Governance timeout 封存必须唯一绑定一个 started item。"
                )
            item_id, item = candidates[0]
            already_sealed = item.get("state") == "failed_closed"
            if already_sealed:
                if (
                    status.get("state") != "processing"
                    or status.get("failure_category") is not None
                ):
                    raise WechatDigestError(
                        "Governance timeout 封存状态未安全收敛。"
                    )
                self._verify_governance_failed_closed_item(item)
            elif (
                item.get("state") != "represented"
                or status.get("state") != "failed"
                or status.get("failure_category")
                != "CodexInterpretationTimeout"
            ):
                raise WechatDigestError(
                    "当前运行不是可封存的 Governance turn timeout。"
                )
            if (
                item.get("semantic_failure") is not None
                or item.get("privacy_route") not in {None, "approved"}
                or item.get("privacy_categories") not in (None, [])
                or item.get("pending_human") is not False
                or item.get("context_object_ids") != []
            ):
                raise WechatDigestError(
                    "Governance timeout item preservation binding 损坏。"
                )
            representation_id = item.get("representation_id")
            if not isinstance(representation_id, str):
                raise WechatDigestError(
                    "Governance timeout Representation binding 损坏。"
                )
            representation = self.representation_repository.get(
                representation_id
            )
            privacy = self.privacy_gate.evaluate(
                self._representation_texts(representation_id),
                semantic_completeness_known=(
                    representation.status == "complete"
                    and representation.completeness == 1.0
                    and not representation.warnings
                ),
            )
            if privacy.route != "approved" or privacy.categories:
                raise WechatDigestError(
                    "Governance timeout privacy binding 不允许封存。"
                )
            atomic_ids = self._verify_semantic_receipts(
                representation_id,
                item,
                recover_missing_item_receipt=True,
            )
            if not atomic_ids:
                raise WechatDigestError(
                    "Governance timeout item 缺少完整 Atomic Information。"
                )
            receipt = _validated_governance_receipt(
                item.get("governance_receipt")
            )
            metrics = _validated_governance_metrics(
                item.get("governance_metrics")
            )
            if (
                receipt.get("phase") != "started"
                or receipt.get("atomic_information_fingerprint")
                != _governance_atomic_fingerprint(atomic_ids)
                or int(metrics["timeout_count"]) < 1
                or int(dict(metrics["failure_categories"]).get("timeout", 0))
                < 1
            ):
                raise WechatDigestError(
                    "Governance timeout evidence 不完整。"
                )
            try:
                global_summary = _validated_global_attempt_summary(
                    self._semantic_port().global_attempt_summary(
                        representation_id
                    )
                )
            except SemanticHandoffError as exc:
                raise WechatDigestError(
                    "Semantic global attempt ledger 未能安全读回。"
                ) from exc
            governance_failure = {
                "failure_category": "turn_timeout",
                "preserved_but_partially_governed": True,
                "provider_retry_permitted": False,
            }
            if already_sealed:
                if item.get("governance_failure") != governance_failure:
                    raise WechatDigestError(
                        "Governance timeout 封存记录不一致。"
                    )
            else:
                updated_item = {
                    **item,
                    "state": "failed_closed",
                    "privacy_route": "approved",
                    "privacy_categories": [],
                    "atomic_information_ids": list(atomic_ids),
                    "governance_failure": governance_failure,
                }
                updated_items = dict(items)
                updated_items[item_id] = updated_item
                updated_status = {
                    **status,
                    "items": updated_items,
                    "state": "processing",
                    "failure_category": None,
                    "updated_at": self.clock(),
                }
                self.run_store.update_status(run_id, updated_status)
            final_status = self.run_store.status(run_id)
            final_items = final_status.get("items")
            if not isinstance(final_items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            final_item = self._item(final_items, item_id)
            self._verify_governance_failed_closed_item(final_item)
            return {
                **global_summary,
                "semantic_provider_calls": 0,
                "governance_provider_calls": 0,
                "governance_preserved_but_incomplete": 1,
                "provider_retry_permitted": False,
            }

    def run(
        self,
        *,
        since: str | None = None,
        from_now: bool = False,
        all_history: bool = False,
        max_terminal_items: int | None = None,
    ) -> WechatDigestResult:
        run_started = time.monotonic()
        bootstrap_count = sum((since is not None, from_now, all_history))
        if bootstrap_count > 1:
            raise WechatDigestError(
                "首次起点只能选择 --since、--from-now 或 --all-history 之一。"
            )
        if max_terminal_items is not None and (
            isinstance(max_terminal_items, bool)
            or not isinstance(max_terminal_items, int)
            or max_terminal_items < 1
        ):
            raise WechatDigestError("每个微信短执行段的完成项目数必须为正整数。")
        _require_openai_codex_sdk()
        with self.run_store.lock():
            self._reject_cleanup_failed_active_run()
            session_factory = getattr(self.interpretation_provider, "session", None)
            session = session_factory() if callable(session_factory) else nullcontext()
            results: list[WechatDigestResult] = []
            pending_final_run_id: str | None = None
            session_body_completed = False
            completed_in_segment = 0
            pending_segment_result: WechatDigestResult | None = None
            try:
                with session:
                    active_run_id = self.run_store.active_run_id()
                    history_scope = all_history
                    if active_run_id is not None:
                        history_scope = (
                            self._plan_all_history_upper(
                                self.run_store.plan(active_run_id)
                            )
                            is not None
                        )
                    while True:
                        result = self._run_locked(
                            since=since,
                            from_now=from_now,
                            all_history=all_history,
                            max_terminal_items=(
                                None
                                if max_terminal_items is None
                                else max_terminal_items - completed_in_segment
                            ),
                        )
                        results.append(result)
                        completed_in_segment += result.segment_items_completed
                        if self.run_store.status(result.run_id).get("state") == (
                            "converged"
                        ):
                            pending_final_run_id = result.run_id
                            break
                        if result.segment_safe_stopped or (
                            max_terminal_items is not None
                            and completed_in_segment >= max_terminal_items
                        ):
                            if result.segment_safe_stopped:
                                pending_segment_result = result
                            else:
                                pending_segment_result = replace(
                                    result,
                                    segment_safe_stopped=True,
                                    segment_items_completed=completed_in_segment,
                                    segment_remaining_items=0,
                                    segment_stop_reason="item_limit",
                                )
                                results[-1] = pending_segment_result
                            break
                        if history_scope and self.run_store.active_run_id() is None:
                            break
                        if from_now or result.new_messages == 0:
                            break
                        since = None
                        from_now = False
                        all_history = False
                    session_body_completed = True
            except BaseException as exc:
                cleanup_run_id = pending_final_run_id
                if cleanup_run_id is None and pending_segment_result is not None:
                    cleanup_run_id = pending_segment_result.run_id
                if session_body_completed and cleanup_run_id is not None:
                    self._record_cleanup_failure(cleanup_run_id)
                if not isinstance(exc, Exception) or isinstance(
                    exc, WechatDigestError
                ):
                    raise
                raise WechatDigestError(
                    "微信信息消化未安全完成；checkpoint 未推进。"
                ) from exc
            if pending_final_run_id is not None:
                try:
                    converged_result = results[-1]
                    results[-1] = replace(
                        self._finalize_converged_run(
                            pending_final_run_id,
                            replayed=results[-1].replayed,
                        ),
                        segment_items_completed=(
                            converged_result.segment_items_completed
                        ),
                    )
                except Exception as exc:
                    failed = self.run_store.status(pending_final_run_id)
                    failed["state"] = "failed"
                    failed["failure_category"] = exc.__class__.__name__
                    failed["updated_at"] = self.clock()
                    self.run_store.update_status(pending_final_run_id, failed)
                    if isinstance(exc, WechatDigestError):
                        raise
                    raise WechatDigestError(
                        "微信信息消化未安全完成；checkpoint 未推进。"
                    ) from exc
            aggregate = self._aggregate_results(results)
            if pending_segment_result is not None:
                segment_status = self.run_store.status(pending_segment_result.run_id)
                receipt = self.run_store.publish_segment_receipt(
                    pending_segment_result.run_id,
                    status=segment_status,
                    completed_items=completed_in_segment,
                    remaining_items=pending_segment_result.segment_remaining_items,
                    stop_reason="item_limit",
                )
                aggregate = replace(
                    aggregate,
                    segment_safe_stopped=True,
                    segment_items_completed=completed_in_segment,
                    segment_remaining_items=pending_segment_result.segment_remaining_items,
                    segment_stop_reason="item_limit",
                    segment_receipt_fingerprint=str(receipt["receipt_fingerprint"]),
                )
            return replace(
                aggregate,
                total_wall_ms=round((time.monotonic() - run_started) * 1000),
            )

    def _reject_cleanup_failed_active_run(self) -> None:
        run_id = self.run_store.active_run_id()
        if run_id is None:
            return
        status = self.run_store.status(run_id)
        if status.get("failure_category") == "governance_session_cleanup":
            raise WechatDigestError(
                "上次 Governance session cleanup 失败；禁止开始下一次运行。"
            )

    def _record_cleanup_failure(self, run_id: str) -> None:
        failed = self.run_store.status(run_id)
        failed["state"] = "failed"
        failed["failure_category"] = "governance_session_cleanup"
        failed["updated_at"] = self.clock()
        self.run_store.update_status(run_id, failed)

    def _finalize_converged_run(
        self, run_id: str, *, replayed: bool
    ) -> WechatDigestResult:
        plan = self.run_store.plan(run_id)
        status = self.run_store.status(run_id)
        if status.get("state") != "converged" or status.get(
            "checkpoint_published"
        ):
            raise WechatDigestError("微信运行不满足 checkpoint 发布条件。")
        upper = WechatCursor.from_dict(plan["upper_bound"], "plan.upper_bound")
        checkpoint_started = time.monotonic()
        try:
            self.run_store.publish_checkpoint(run_id, upper)
            completed = dict(status)
            completed["checkpoint_published"] = True
            completed["state"] = "completed"
            completed["failure_category"] = None
            completed["updated_at"] = self.clock()
            self.run_store.update_status(run_id, completed)
            self.run_store.clear_active()
            if self.run_store.checkpoint() != upper:
                raise WechatDigestError("微信 checkpoint 完成读回失败。")
        finally:
            self._segment_performance["checkpoint_wall_ms"] = (
                self._segment_performance.get("checkpoint_wall_ms", 0)
                + round((time.monotonic() - checkpoint_started) * 1000)
            )
        return self._result(run_id, completed, replayed=replayed)

    def prepare_next_semantic(
        self, *, batch_size: int = DEFAULT_EXTERNAL_AGENT_BATCH_SIZE
    ) -> WechatSemanticPreparation:
        """Recover an active run only up to its next single semantic batch."""
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < 1
        ):
            raise WechatDigestError("semantic batch size 必须是正整数。")
        with self.run_store.lock():
            self._reject_cleanup_failed_active_run()
            session_factory = getattr(self.interpretation_provider, "session", None)
            session = session_factory() if callable(session_factory) else nullcontext()
            with session:
                run_id = self.run_store.active_run_id()
                if run_id is None:
                    raise WechatDigestError(
                        "不存在可恢复的微信运行；未创建新 capture。"
                    )
                plan = self.run_store.plan(run_id)
                effective_batch_size = self._plan_batch_size(plan)
                if batch_size != effective_batch_size:
                    raise WechatDigestError("semantic batch size 与 durable run 不一致。")
                pre_attempt_representation_ids: tuple[str, ...] = ()
                persisted_status = self.run_store.status(run_id)
                if self._semantic_handoff_error_was_pre_provider(
                    plan, persisted_status
                ):
                    persisted_items = persisted_status.get("items")
                    assert isinstance(persisted_items, dict)
                    pre_attempt_representation_ids = tuple(
                        str(item["representation_id"])
                        for item in persisted_items.values()
                        if isinstance(item, dict)
                        and item.get("state") == "represented"
                    )
                capture, plan, status = self._load_or_upgrade_active_capture(
                    run_id,
                    plan,
                    persisted_status,
                )
                self._verify_capture_against_plan(capture, plan)
                self._verify_plan_and_status(
                    run_id, capture, plan, status
                )
                if pre_attempt_representation_ids:
                    return self._prepared_batch(
                        run_id,
                        pre_attempt_representation_ids[0],
                        effective_batch_size,
                    )
                return self._prepare_next_semantic_locked(
                    run_id,
                    capture,
                    plan,
                    status,
                    batch_size=effective_batch_size,
                )

    def upgrade_active_v1(self) -> str:
        """Explicit, audited, zero-Provider upgrade for the active legacy run only."""
        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError("不存在可升级的 active v1 微信运行。")
            if self.run_store.has_plan_receipt(run_id):
                raise WechatDigestError("active 微信运行已经完成升级。")
            persisted_plan = self.run_store.plan(run_id)
            if persisted_plan.get("run_id") != run_id:
                raise WechatDigestError("active 微信运行计划损坏。")
            capture = self.capture_provider.capture(
                WechatCursor.from_dict(
                    persisted_plan["after_cursor"], "plan.after_cursor"
                ),
                upper_bound=WechatCursor.from_dict(
                    persisted_plan["upper_bound"], "plan.upper_bound"
                ),
            )
            self._verify_capture_against_plan(capture, persisted_plan)
            created_at = persisted_plan.get("created_at")
            if not isinstance(created_at, str):
                raise WechatDigestError("active v1 微信运行损坏。")
            plan, _ = _build_plan(
                capture,
                clock=lambda: created_at,
                run_id=run_id,
                created_at=created_at,
                semantic_batch_size=DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
            )
            expected_legacy = dict(plan)
            expected_legacy["schema_version"] = LEGACY_RUN_PLAN_SCHEMA_VERSION
            expected_legacy.pop("semantic_batch_size")
            expected_legacy.pop("all_history_upper_bound")
            status = self.run_store.status(run_id)
            # A receipt-less interruption may leave either independently
            # validated schema shape on disk.  No other shape is recoverable.
            if persisted_plan == expected_legacy:
                self._verify_plan_and_status(
                    run_id, capture, persisted_plan, status, require_receipt=False
                )
            elif persisted_plan == plan:
                self._verify_plan_and_status(
                    run_id, capture, persisted_plan, status, require_receipt=False,
                    allow_legacy_status=True,
                )
            else:
                raise WechatDigestError("active v1 与 fixed capture 不一致。")
            status = dict(status)
            status["plan_fingerprint"] = _plan_fingerprint(plan)
            self.run_store.complete_upgrade(run_id, plan, status)
            active_run_id = self.run_store.active_run_id()
            if active_run_id != run_id:
                raise WechatDigestError("微信升级 active run 读回不一致。")
            disk_plan = self.run_store.plan(active_run_id)
            disk_status = self.run_store.status(active_run_id)
            receipt = self.run_store.plan_receipt(active_run_id)
            if (
                disk_plan.get("schema_version") != RUN_PLAN_SCHEMA_VERSION
                or disk_plan.get("run_id") != active_run_id
                or disk_status.get("run_id") != active_run_id
                or receipt.get("run_id") != active_run_id
            ):
                raise WechatDigestError("微信升级 durable run 读回不一致。")
            self._verify_capture_against_plan(capture, disk_plan)
            self._verify_plan_and_status(
                active_run_id, capture, disk_plan, disk_status
            )
            return active_run_id

    def upgrade_active_v2_all_history(self) -> str:
        """Explicitly freeze one global upper for an active v2 history run."""
        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError(
                    "不存在可冻结全历史边界的 active v2 微信运行。"
                )
            persisted_plan = self.run_store.plan(run_id)
            if persisted_plan.get("run_id") != run_id:
                raise WechatDigestError("active v2 微信运行计划损坏。")
            capture = self.capture_provider.capture(
                WechatCursor.from_dict(
                    persisted_plan["after_cursor"], "plan.after_cursor"
                ),
                upper_bound=WechatCursor.from_dict(
                    persisted_plan["upper_bound"], "plan.upper_bound"
                ),
            )
            self._verify_capture_against_plan(capture, persisted_plan)
            created_at = persisted_plan.get("created_at")
            if not isinstance(created_at, str):
                raise WechatDigestError("active v2 微信运行损坏。")
            status = self.run_store.status(run_id)
            receipt = self.run_store.plan_receipt(run_id)
            receipt_phase = receipt.get("phase")
            if receipt.get("schema_version") == LEGACY_RUN_PLAN_RECEIPT_SCHEMA_VERSION:
                receipt_phase = "committed"

            if receipt_phase == "committed":
                committed_fingerprint = _committed_receipt_fingerprint(receipt)
                if persisted_plan.get("schema_version") == RUN_PLAN_SCHEMA_VERSION:
                    if committed_fingerprint != _plan_fingerprint(persisted_plan):
                        raise WechatDigestError(
                            "微信全历史边界升级 receipt 不一致。"
                        )
                    self._verify_plan_and_status(
                        run_id, capture, persisted_plan, status
                    )
                    if self._plan_all_history_upper(persisted_plan) is None:
                        raise WechatDigestError(
                            "active v3 微信运行不是全历史恢复范围。"
                        )
                    return run_id
                if (
                    persisted_plan.get("schema_version")
                    != PREVIOUS_RUN_PLAN_SCHEMA_VERSION
                ):
                    raise WechatDigestError(
                        "active 微信运行不是可升级的 v2 全历史范围。"
                    )
                self._verify_plan_and_status(
                    run_id, capture, persisted_plan, status
                )
                observed = self.capture_provider.capture(
                    ZERO_CURSOR, observe_only=True
                )
                all_history_upper = observed.upper_bound
                if all_history_upper < capture.upper_bound:
                    raise WechatDigestError(
                        "微信全历史边界早于 active 窗口；未执行升级。"
                    )
                plan, _ = _build_plan(
                    capture,
                    clock=lambda: created_at,
                    run_id=run_id,
                    created_at=created_at,
                    semantic_batch_size=self._plan_batch_size(persisted_plan),
                    all_history_upper_bound=all_history_upper,
                )
                if _previous_plan_projection(plan) != persisted_plan:
                    raise WechatDigestError(
                        "active v2 与 fixed capture 不一致。"
                    )
                pending_receipt = {
                    "schema_version": RUN_PLAN_RECEIPT_SCHEMA_VERSION,
                    "run_id": run_id,
                    "phase": "pending",
                    "previous_plan_fingerprint": committed_fingerprint,
                    "all_history_upper_bound": all_history_upper.to_dict(),
                    "target_plan_fingerprint": _plan_fingerprint(plan),
                }
            elif receipt_phase == "pending":
                try:
                    all_history_upper = WechatCursor.from_dict(
                        receipt.get("all_history_upper_bound"),
                        "receipt.all_history_upper_bound",
                    )
                except (TypeError, ValueError) as exc:
                    raise WechatDigestError(
                        "微信全历史边界 pending receipt 损坏。"
                    ) from exc
                if all_history_upper < capture.upper_bound:
                    raise WechatDigestError(
                        "微信全历史边界早于 active 窗口。"
                    )
                plan, _ = _build_plan(
                    capture,
                    clock=lambda: created_at,
                    run_id=run_id,
                    created_at=created_at,
                    semantic_batch_size=self._plan_batch_size(persisted_plan),
                    all_history_upper_bound=all_history_upper,
                )
                previous_plan = _previous_plan_projection(plan)
                previous_fingerprint = _plan_fingerprint(previous_plan)
                target_fingerprint = _plan_fingerprint(plan)
                if (
                    receipt.get("previous_plan_fingerprint")
                    != previous_fingerprint
                    or receipt.get("target_plan_fingerprint")
                    != target_fingerprint
                ):
                    raise WechatDigestError(
                        "微信全历史边界 pending receipt 不一致。"
                    )
                if persisted_plan == previous_plan:
                    if status.get("plan_fingerprint") != previous_fingerprint:
                        raise WechatDigestError(
                            "微信全历史边界升级状态不一致。"
                        )
                    self._verify_plan_and_status(
                        run_id,
                        capture,
                        previous_plan,
                        status,
                        require_receipt=False,
                    )
                elif persisted_plan == plan:
                    if status.get("plan_fingerprint") == previous_fingerprint:
                        self._verify_plan_and_status(
                            run_id,
                            capture,
                            previous_plan,
                            status,
                            require_receipt=False,
                        )
                    elif status.get("plan_fingerprint") == target_fingerprint:
                        self._verify_plan_and_status(
                            run_id,
                            capture,
                            plan,
                            status,
                            require_receipt=False,
                        )
                    else:
                        raise WechatDigestError(
                            "微信全历史边界升级状态不一致。"
                        )
                else:
                    raise WechatDigestError(
                        "微信全历史边界升级 plan 不一致。"
                    )
                pending_receipt = dict(receipt)
            else:
                raise WechatDigestError(
                    "微信全历史边界升级 receipt 损坏。"
                )

            if persisted_plan.get("schema_version") == RUN_PLAN_SCHEMA_VERSION:
                if persisted_plan != plan:
                    raise WechatDigestError(
                        "微信全历史边界升级 plan 不一致。"
                    )
                if status.get("plan_fingerprint") == _plan_fingerprint(plan):
                    self._verify_plan_and_status(
                        run_id,
                        capture,
                        plan,
                        status,
                        require_receipt=False,
                    )

            upgraded_status = dict(status)
            upgraded_status["plan_fingerprint"] = _plan_fingerprint(plan)
            self.run_store.complete_all_history_upper_upgrade(
                run_id, plan, upgraded_status, pending_receipt
            )
            if self.run_store.active_run_id() != run_id:
                raise WechatDigestError(
                    "微信全历史边界升级 active run 读回不一致。"
                )
            disk_plan = self.run_store.plan(run_id)
            disk_status = self.run_store.status(run_id)
            disk_receipt = self.run_store.plan_receipt(run_id)
            if (
                disk_plan != plan
                or disk_status != upgraded_status
                or disk_receipt.get("run_id") != run_id
                or _committed_receipt_fingerprint(disk_receipt)
                != _plan_fingerprint(disk_plan)
            ):
                raise WechatDigestError(
                    "微信全历史边界升级 durable readback 不一致。"
                )
            self._verify_capture_against_plan(capture, disk_plan)
            self._verify_plan_and_status(
                run_id, capture, disk_plan, disk_status
            )
            return run_id

    def _prepare_next_semantic_locked(
        self,
        run_id: str,
        capture: WechatCapture,
        plan: dict[str, object],
        status: dict[str, object],
        *,
        batch_size: int,
    ) -> WechatSemanticPreparation:
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        attachments = _plan_sequence(plan.get("attachments"), "attachments")
        attachment_source_ids = {
            str(item["attachment_key"]): (
                None if item.get("source_id") is None else str(item["source_id"])
            )
            for item in attachments
        }
        capture_index = self.run_store.load_capture_index(
            run_id, capture=capture
        )
        captured_attachments = {
            str(entry["attachment_key"]): next(
                attachment
                for attachment in capture.messages[int(entry["message_index"])].attachments
                if attachment.attachment_key == entry["attachment_key"]
            )
            for entry in _plan_sequence(
                capture_index.get("attachments"),
                "capture.index.attachments",
            )
        }
        conversation_indexes = {
            str(value["conversation_key"]): tuple(value["message_indexes"])
            for value in _plan_sequence(
                capture_index.get("conversations"),
                "capture.index.conversations",
            )
        }
        for item_plan in attachments:
            item_id = f"attachment:{item_plan['attachment_key']}"
            if self._terminal_item_valid(self._item(items, item_id)):
                continue
            if item_plan["status"] != "available":
                self._update_item(run_id, status, item_id, state="unsupported")
                continue
            captured = captured_attachments.get(str(item_plan["attachment_key"]))
            if captured is None or captured.path is None:
                raise WechatDigestError("微信附件重放无法精确定位。")
            ready = self._process_attachment(
                run_id, status, item_id, item_plan, captured, prepare_only=True
            )
            if ready is not None:
                return self._prepared_batch(run_id, ready, batch_size)
        for item_plan in _plan_sequence(plan.get("conversations"), "conversations"):
            item_id = f"conversation:{item_plan['conversation_key']}"
            if self._terminal_item_valid(self._item(items, item_id)):
                continue
            payload = _conversation_source_payload(
                capture,
                str(item_plan["conversation_key"]),
                attachment_source_ids,
                message_indexes=conversation_indexes.get(
                    str(item_plan["conversation_key"])
                ),
            )
            if (
                _sha256_bytes(payload) != item_plan["content_hash"]
                or len(payload) != item_plan["size_bytes"]
            ):
                raise WechatDigestError("微信 Conversation Source 重放不一致。")
            ready = self._process_conversation(
                run_id, status, item_id, item_plan, payload, prepare_only=True
            )
            if ready is not None:
                return self._prepared_batch(run_id, ready, batch_size)
        raise WechatDigestError("当前运行没有可准备的 semantic item。")

    def _prepared_batch(
        self, run_id: str, representation_id: str, batch_size: int
    ) -> WechatSemanticPreparation:
        representation = self.representation_repository.get(representation_id)
        batches = _analysis_batches(
            _units_from_representation(
                representation, self.representation_repository
            ),
            batch_size,
        )
        if not batches:
            raise WechatDigestError("semantic item 缺少 eligible units。")
        return WechatSemanticPreparation(
            run_id,
            representation_id,
            tuple(unit.unit_id for unit in batches[0].anchor_units),
        )

    @staticmethod
    def _plan_batch_size(plan: Mapping[str, object]) -> int:
        if plan.get("schema_version") == LEGACY_RUN_PLAN_SCHEMA_VERSION:
            value = DEFAULT_EXTERNAL_AGENT_BATCH_SIZE
        else:
            if "semantic_batch_size" not in plan:
                raise WechatDigestError("微信运行计划 semantic batch size 缺失。")
            value = plan["semantic_batch_size"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise WechatDigestError("微信运行计划 semantic batch size 损坏。")
        return value

    @staticmethod
    def _plan_all_history_upper(
        plan: Mapping[str, object],
    ) -> WechatCursor | None:
        if plan.get("schema_version") not in {
            RUN_PLAN_SCHEMA_VERSION,
            SNAPSHOT_RUN_PLAN_SCHEMA_VERSION,
        }:
            return None
        if "all_history_upper_bound" not in plan:
            raise WechatDigestError("微信运行计划缺少全历史边界。")
        value = plan["all_history_upper_bound"]
        if value is None:
            return None
        try:
            upper = WechatCursor.from_dict(
                value, "plan.all_history_upper_bound"
            )
            window_upper = WechatCursor.from_dict(
                plan.get("upper_bound"), "plan.upper_bound"
            )
        except (TypeError, ValueError) as exc:
            raise WechatDigestError("微信运行计划全历史边界损坏。") from exc
        if window_upper > upper:
            raise WechatDigestError("微信窗口超出冻结的全历史边界。")
        return upper

    def _verify_plan_and_status(
        self,
        active_run_id: str,
        capture: WechatCapture | None,
        plan: dict[str, object],
        status: dict[str, object],
        *,
        require_receipt: bool = True,
        allow_legacy_status: bool = False,
        allow_pending_unknown_resolution: bool = False,
    ) -> None:
        run_id = plan.get("run_id")
        created_at = plan.get("created_at")
        if not isinstance(run_id, str) or run_id != active_run_id or not isinstance(created_at, str):
            raise WechatDigestError("微信运行计划损坏。")
        if plan.get("schema_version") == SNAPSHOT_RUN_PLAN_SCHEMA_VERSION:
            durable_capture, _ = self.run_store.load_capture_artifacts(
                active_run_id,
                plan=plan,
            )
            if capture is not None and durable_capture != capture:
                raise WechatDigestError("微信 durable capture 读回不一致。")
        plan_is_legacy = plan.get("schema_version") == LEGACY_RUN_PLAN_SCHEMA_VERSION
        if plan.get("schema_version") not in {
            SNAPSHOT_RUN_PLAN_SCHEMA_VERSION,
            RUN_PLAN_SCHEMA_VERSION,
            PREVIOUS_RUN_PLAN_SCHEMA_VERSION,
            LEGACY_RUN_PLAN_SCHEMA_VERSION,
        }:
            raise WechatDigestError("微信运行计划损坏。")
        if require_receipt and plan_is_legacy:
            raise WechatDigestError("active legacy 微信运行必须显式升级。")
        fingerprint = _plan_fingerprint(plan)
        if require_receipt:
            receipt = self.run_store.plan_receipt(active_run_id)
            if (
                receipt.get("run_id") != active_run_id
                or _committed_receipt_fingerprint(receipt) != fingerprint
            ):
                raise WechatDigestError("微信运行计划 receipt 不一致。")
        if status.get("run_id") != active_run_id:
            raise WechatDigestError("微信运行计划 receipt 不一致。")
        status_fingerprint = status.get("plan_fingerprint")
        expected_status_keys = {
            "schema_version",
            "run_id",
            "state",
            "failure_category",
            "checkpoint_published",
            "items",
            "updated_at",
        }
        if plan_is_legacy:
            if status_fingerprint is not None:
                raise WechatDigestError("微信运行状态 legacy 形态损坏。")
        elif status_fingerprint != fingerprint and not (
            allow_legacy_status and status_fingerprint is None
        ):
            raise WechatDigestError("微信运行计划 receipt 不一致。")
        if status_fingerprint is not None:
            expected_status_keys.add("plan_fingerprint")
        if set(status) != expected_status_keys:
            raise WechatDigestError("微信运行状态形态损坏。")
        if capture is not None:
            expected, _ = _build_plan(
                capture,
                clock=lambda: created_at,
                run_id=run_id,
                created_at=created_at,
                semantic_batch_size=self._plan_batch_size(plan),
                all_history_upper_bound=self._plan_all_history_upper(plan),
                capture_receipt_fingerprint=(
                    str(plan["capture_receipt_fingerprint"])
                    if plan.get("schema_version")
                    == SNAPSHOT_RUN_PLAN_SCHEMA_VERSION
                    else None
                ),
            )
            comparable = dict(plan)
            if comparable.get("schema_version") == LEGACY_RUN_PLAN_SCHEMA_VERSION:
                expected.pop("semantic_batch_size")
                expected.pop("all_history_upper_bound")
                expected["schema_version"] = LEGACY_RUN_PLAN_SCHEMA_VERSION
            elif comparable.get("schema_version") == PREVIOUS_RUN_PLAN_SCHEMA_VERSION:
                expected = _previous_plan_projection(expected)
            if comparable != expected:
                raise WechatDigestError("微信 durable plan 与 fixed capture 不一致。")
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        expected_items = {
            **{f"conversation:{item['conversation_key']}": ("conversation", item["source_id"]) for item in _plan_sequence(plan.get("conversations"), "conversations")},
            **{f"attachment:{item['attachment_key']}": ("attachment", item["source_id"]) for item in _plan_sequence(plan.get("attachments"), "attachments")},
        }
        if set(items) != set(expected_items):
            raise WechatDigestError("微信运行状态与 durable plan 不收敛。")
        for item_id, (kind, source_id) in expected_items.items():
            item = self._item(items, item_id)
            if item.get("kind") != kind or item.get("source_id") != source_id:
                raise WechatDigestError("微信运行状态 binding 损坏。")
            if item.get("governance_metrics") is not None:
                _validated_governance_metrics(item["governance_metrics"])
            if item.get("governance_receipt") is not None:
                _validated_governance_receipt(item["governance_receipt"])
            if item.get("governance_migration") is not None:
                _validated_governance_migration(item["governance_migration"])
            if item.get("state") in {
                "represented",
                "local_only",
                "processed",
                "pending_human",
                "failed_closed",
            }:
                representation_id = item.get("representation_id")
                if not isinstance(representation_id, str):
                    raise WechatDigestError("微信运行状态 receipt 损坏。")
                source = self.source_repository.get(str(source_id))
                expected_source = next(
                    candidate
                    for candidate in (
                        _plan_sequence(plan.get("conversations"), "conversations")
                        + _plan_sequence(plan.get("attachments"), "attachments")
                    )
                    if candidate.get("source_id") == source_id
                )
                if (
                    source.content_hash != expected_source.get("content_hash")
                    or source.size_bytes != expected_source.get("size_bytes")
                ):
                    raise WechatDigestError("微信运行状态 Source binding 损坏。")
                if self.representation_repository.get(representation_id).source_id != source_id:
                    raise WechatDigestError("微信运行状态 Representation binding 损坏。")
                representation = self.representation_repository.get(representation_id)
                privacy = self.privacy_gate.evaluate(self._representation_texts(representation_id), semantic_completeness_known=(representation.status == "complete" and representation.completeness == 1.0 and not representation.warnings))
                if item.get("state") == "represented" and privacy.route != "approved":
                    raise WechatDigestError("微信运行状态 privacy receipt 损坏。")
                if item.get("state") == "failed_closed" and privacy.route != "approved":
                    raise WechatDigestError("微信运行状态 privacy receipt 损坏。")
                if item.get("state") == "local_only" and privacy.route != "local_only":
                    raise WechatDigestError("微信运行状态 privacy receipt 损坏。")
                if item.get("state") in {"processed", "pending_human"}:
                    self._verify_semantic_receipts(representation_id, item)
                if (
                    item.get("state") == "failed_closed"
                    and not allow_pending_unknown_resolution
                ):
                    self._verify_failed_closed_item(
                        run_id=active_run_id,
                        plan=plan,
                        item_id=item_id,
                        item=item,
                    )

    def _verify_failed_closed_item(
        self,
        *,
        run_id: str,
        plan: Mapping[str, object],
        item_id: str,
        item: Mapping[str, object],
    ) -> None:
        semantic_failure = item.get("semantic_failure")
        governance_failure = item.get("governance_failure")
        if (semantic_failure is None) == (governance_failure is None):
            raise WechatDigestError(
                "微信 failed_closed item failure variant 损坏。"
            )
        if governance_failure is not None:
            self._verify_governance_failed_closed_item(item)
            return
        failure = semantic_failure
        global_ordinal = (
            failure.get("global_ordinal")
            if isinstance(failure, dict)
            else None
        )
        expected_category = (
            "runtime_nonzero_exit"
            if global_ordinal == 166
            else "timeout"
            if global_ordinal == 212
            else None
        )
        resolution_pattern = (
            r"unknown_resolution_[0-9a-f]{32}"
            if global_ordinal == 166
            else r"unknown_resolution_212_[0-9a-f]{32}"
        )
        generic = global_ordinal not in {166, 212}
        expected_keys = (
            {
                "resolution_id",
                "global_ordinal",
                "failure_category",
                "result_present",
                "diagnostic_persistence_status",
                "process_cleanup_status",
                "preserved_but_unabsorbed",
                "pre_status_fingerprint",
            }
            if generic
            else {
                "resolution_id",
                "global_ordinal",
                "failure_category",
                "result_present",
                "preserved_but_unabsorbed",
            }
        )
        if generic:
            resolution_pattern = r"attempt_resolution_[0-9a-f]{32}"
        if (
            not isinstance(failure, dict)
            or set(failure) != expected_keys
            or re.fullmatch(
                resolution_pattern,
                str(failure.get("resolution_id")),
            )
            is None
            or isinstance(global_ordinal, bool)
            or not isinstance(global_ordinal, int)
            or global_ordinal < 1
            or failure.get("failure_category")
            != ("timeout" if generic else expected_category)
            or generic
            and (
                failure.get("diagnostic_persistence_status") != "failed"
                or failure.get("process_cleanup_status") != "verified"
                or not _sha256_value(failure.get("pre_status_fingerprint"))
            )
            or failure.get("result_present") is not False
            or failure.get("preserved_but_unabsorbed") is not True
            or item.get("privacy_route") != "approved"
            or item.get("privacy_categories") != []
            or item.get("atomic_information_ids") != []
            or item.get("pending_human") is not False
            or item.get("context_object_ids") != []
        ):
            raise WechatDigestError(
                "微信 failed_closed item receipt 损坏。"
            )
        representation_id = item.get("representation_id")
        if not isinstance(representation_id, str) or os.path.lexists(
            self.workspace
            / "02_processing"
            / "information"
            / representation_id
        ):
            raise WechatDigestError(
                "微信 failed_closed item 不得存在 Semantic package。"
            )
        source_id = item.get("source_id")
        if any(
            revision.origin_source_id == source_id
            for revision in self.information_store.list_atomic_information()
        ):
            raise WechatDigestError(
                "微信 failed_closed item 不得存在 Durable Atomic Information。"
            )
        binding = (
            self._attempt_resolution_terminal_binding(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=item,
                pre_status_fingerprint=str(
                    failure["pre_status_fingerprint"]
                ),
            )
            if generic
            else self._unknown_resolution_digest_binding(
                run_id=run_id,
                plan=plan,
                item_id=item_id,
                item=item,
            )
        )
        try:
            validator = (
                self._semantic_port().validate_attempt_resolution_digest
                if generic
                else self._semantic_port().validate_unknown_resolution_digest
                if global_ordinal == 166
                else self._semantic_port().validate_timeout_212_resolution_digest
            )
            validator(
                digest_binding=binding,
                failed_closed_status_fingerprint=_sha256_bytes(
                    _canonical_json(item).encode("utf-8")
                ),
                resolution_id=str(failure["resolution_id"]),
            )
        except SemanticHandoffError as exc:
            raise WechatDigestError(
                "微信 failed_closed item authority receipt 损坏。"
            ) from exc

    def _verify_governance_failed_closed_item(
        self, item: Mapping[str, object]
    ) -> None:
        failure = item.get("governance_failure")
        receipt = _validated_governance_receipt(
            item.get("governance_receipt")
        )
        metrics = _validated_governance_metrics(
            item.get("governance_metrics")
        )
        atomic_ids = item.get("atomic_information_ids")
        if (
            not isinstance(failure, dict)
            or set(failure)
            != {
                "failure_category",
                "preserved_but_partially_governed",
                "provider_retry_permitted",
            }
            or failure.get("failure_category") != "turn_timeout"
            or failure.get("preserved_but_partially_governed") is not True
            or failure.get("provider_retry_permitted") is not False
            or item.get("semantic_failure") is not None
            or item.get("state") != "failed_closed"
            or item.get("privacy_route") != "approved"
            or item.get("privacy_categories") != []
            or not isinstance(atomic_ids, list)
            or not atomic_ids
            or any(not isinstance(value, str) for value in atomic_ids)
            or len(atomic_ids) != len(set(atomic_ids))
            or receipt.get("phase") != "started"
            or receipt.get("atomic_information_fingerprint")
            != _governance_atomic_fingerprint(atomic_ids)
            or int(metrics["timeout_count"]) < 1
            or int(dict(metrics["failure_categories"]).get("timeout", 0)) < 1
            or item.get("pending_human") is not False
            or item.get("context_object_ids") != []
        ):
            raise WechatDigestError(
                "微信 failed_closed Governance receipt 损坏。"
            )
        representation_id = item.get("representation_id")
        if not isinstance(representation_id, str):
            raise WechatDigestError(
                "微信 failed_closed Governance Representation 损坏。"
            )
        self._verify_semantic_receipts(representation_id, item)

    def _verify_semantic_receipts(
        self,
        representation_id: str,
        item: Mapping[str, object],
        *,
        recover_missing_item_receipt: bool = False,
    ) -> tuple[str, ...]:
        package = (
            self.workspace
            / "02_processing"
            / "information"
            / representation_id
        )
        if not package.is_dir():
            raise WechatDigestError("微信运行状态 semantic package 缺失。")
        try:
            manifest, candidates = validate_representation_information_package(
                package
            )
            source = manifest.get("source")
            representation = manifest.get("representation")
            persisted_representation = self.representation_repository.get(
                representation_id
            )
            if (
                not isinstance(source, dict)
                or not isinstance(representation, dict)
                or representation.get("representation_id") != representation_id
                or source.get("id") != persisted_representation.source_id
                or source.get("content_hash")
                != persisted_representation.source_content_hash
            ):
                raise WechatDigestError(
                    "微信运行状态 semantic package binding 损坏。"
                )
            source_id = source.get("id")
            if not isinstance(source_id, str):
                raise WechatDigestError(
                    "微信运行状态 semantic package binding 损坏。"
                )
            package_schema = manifest.get("schema_version")
            if not isinstance(package_schema, str):
                raise WechatDigestError(
                    "微信运行状态 semantic package schema 损坏。"
                )
            revisions = _load_candidates(
                package, package_schema, source_id
            )
            expected = {
                revision.atomic_information_id: revision
                for revision in revisions
            }
            if len(expected) != len(candidates):
                raise WechatDigestError(
                    "微信运行状态 semantic package Candidate 冲突。"
                )
            validate_completed_published_audits(
                representation_service=RepresentationInformationService(
                    self.source_repository,
                    self.representation_repository,
                    self.workspace / "02_processing" / "information",
                ),
                representation_id=representation_id,
                manifest=manifest,
                audit_root=(
                    self.workspace
                    / "02_processing"
                    / "semantic_handoff_runs"
                ),
                package_fingerprint=_package_fingerprint(package),
            )
            observed = item.get("atomic_information_ids")
            if recover_missing_item_receipt and observed == []:
                observed = list(expected)
            if (
                not isinstance(observed, list)
                or any(not isinstance(value, str) for value in observed)
                or len(observed) != len(set(observed))
                or set(observed) != set(expected)
            ):
                raise WechatDigestError(
                    "微信运行状态 semantic receipt 不收敛。"
                )
            if item.get("state") == "pending_human" and not expected:
                raise WechatDigestError(
                    "微信运行状态 pending_human receipt 为空。"
                )
            for atomic_id, expected_revision in expected.items():
                revision = self.information_store.get_current(atomic_id)
                if (
                    revision.atomic_information_id != atomic_id
                    or revision.origin_source_id
                    != expected_revision.origin_source_id
                    or revision.origin_candidate_id
                    != expected_revision.origin_candidate_id
                    or revision.origin_fingerprint
                    != expected_revision.origin_fingerprint
                ):
                    raise WechatDigestError(
                        "微信运行状态 Atomic Information receipt 不收敛。"
                    )
            return tuple(expected)
        except WechatDigestError:
            raise
        except (
            OSError,
            TypeError,
            ValueError,
            RepresentationInformationError,
            SemanticHandoffError,
        ) as exc:
            raise WechatDigestError(
                "微信运行状态 semantic receipt 损坏。"
            ) from exc

    def _persist_new_capture_plan(
        self,
        capture: WechatCapture,
        *,
        all_history_upper_bound: WechatCursor | None,
        created_at: str | None = None,
        capture_ms: int = 0,
    ) -> tuple[dict[str, object], dict[str, object]]:
        created_at = created_at or self.clock()
        slice_started = time.monotonic()
        preliminary_plan, preliminary_status = _build_plan(
            capture,
            clock=lambda: created_at,
            created_at=created_at,
            semantic_batch_size=self.semantic_batch_size,
            all_history_upper_bound=all_history_upper_bound,
        )
        if self._segment_performance:
            self._segment_performance["slice_build_ms"] += round(
                (time.monotonic() - slice_started) * 1000
            )
        run_id = str(preliminary_plan["run_id"])
        publish_started = time.monotonic()
        self.run_store.publish_capture_pending(
            preliminary_plan,
            capture,
            capture_ms=capture_ms,
        )
        receipt = self.run_store.publish_capture_artifacts(
            run_id,
            capture,
            plan_binding_fingerprint=_plan_fingerprint(preliminary_plan),
            capture_ms=capture_ms,
        )
        if self._segment_performance:
            self._segment_performance["snapshot_publish_ms"] = round(
                (time.monotonic() - publish_started) * 1000
            )
        plan = {
            **preliminary_plan,
            "schema_version": SNAPSHOT_RUN_PLAN_SCHEMA_VERSION,
            "capture_receipt_fingerprint": str(receipt["receipt_fingerprint"]),
        }
        status = {
            **preliminary_status,
            "plan_fingerprint": _plan_fingerprint(plan),
        }
        self.run_store.create(plan, status)
        return plan, status

    def _semantic_trace_exists(self, representation_id: str) -> bool:
        package = (
            self.workspace
            / "02_processing"
            / "information"
            / representation_id
        )
        if os.path.lexists(package):
            return True
        audit_root = (
            self.workspace / "02_processing" / "semantic_handoff_runs"
        )
        if not os.path.lexists(audit_root):
            return False
        if not audit_root.is_dir():
            raise WechatDigestError(
                "微信 represented pre-provider Semantic inventory 损坏。"
            )
        try:
            needle = representation_id.encode("utf-8")
            return any(
                needle in path.read_bytes()
                for path in audit_root.rglob("*.json")
                if path.is_file()
            )
        except OSError as exc:
            raise WechatDigestError(
                "微信 represented pre-provider Semantic inventory 不可读。"
            ) from exc

    def _verify_represented_pre_provider_item(
        self,
        plan: Mapping[str, object],
        item: Mapping[str, object],
        *,
        allow_run_receipt_only: bool = False,
    ) -> str:
        source_id = item.get("source_id")
        representation_id = item.get("representation_id")
        if not isinstance(source_id, str) or not isinstance(
            representation_id, str
        ):
            raise WechatDigestError(
                "微信 represented pre-provider binding 损坏。"
            )
        expected_source = next(
            (
                candidate
                for candidate in (
                    _plan_sequence(plan.get("conversations"), "conversations")
                    + _plan_sequence(plan.get("attachments"), "attachments")
                )
                if candidate.get("source_id") == source_id
            ),
            None,
        )
        if expected_source is None:
            raise WechatDigestError(
                "微信 represented pre-provider Source binding 损坏。"
            )
        try:
            source = self.source_repository.get(source_id)
            if (
                source.content_hash != expected_source.get("content_hash")
                or source.size_bytes != expected_source.get("size_bytes")
                or not self.source_service.verify(source_id).verified
            ):
                raise WechatDigestError(
                    "微信 represented pre-provider Source 读回失败。"
                )
            representation = self.representation_repository.get(
                representation_id
            )
            if (
                representation.source_id != source_id
                or representation.status != "complete"
                or representation.completeness != 1.0
                or representation.warnings
                or not self.representation_service.verify(
                    representation_id
                ).verified
            ):
                raise WechatDigestError(
                    "微信 represented pre-provider Representation 读回失败。"
                )
            trace_exists = self._semantic_trace_exists(representation_id)
            inventory_kind = "absent"
            if trace_exists and not allow_run_receipt_only:
                raise WechatDigestError(
                    "微信 represented item 已存在 Semantic 执行痕迹。"
                )
            if allow_run_receipt_only:
                privacy = self.privacy_gate.evaluate(
                    self._representation_texts(representation_id),
                    semantic_completeness_known=True,
                )
                inventory = self._semantic_port().validate_pre_attempt_inventory(
                    representation_id,
                    privacy_binding=self._semantic_privacy_binding(
                        representation_id, privacy
                    ),
                )
                if (
                    set(inventory)
                    != {
                        "inventory_kind",
                        "semantic_run_id",
                        "run_receipt_fingerprint",
                        "attempt_count",
                        "reserved_count",
                        "started_count",
                        "result_count",
                    }
                    or re.fullmatch(
                        r"semantic_run_[0-9a-f]{32}",
                        str(inventory.get("semantic_run_id")),
                    )
                    is None
                    or inventory.get("inventory_kind")
                    not in {"absent", "run_receipt_only"}
                    or trace_exists
                    != (
                        inventory.get("inventory_kind")
                        == "run_receipt_only"
                    )
                    or (
                        inventory.get("inventory_kind") == "absent"
                        and inventory.get("run_receipt_fingerprint") is not None
                    )
                    or (
                        inventory.get("inventory_kind")
                        == "run_receipt_only"
                        and not _sha256_value(
                            inventory.get("run_receipt_fingerprint")
                        )
                    )
                    or any(
                        inventory.get(key) != 0
                        for key in (
                            "attempt_count",
                            "reserved_count",
                            "started_count",
                            "result_count",
                        )
                    )
                ):
                    raise WechatDigestError(
                        "微信 represented pre-attempt inventory 损坏。"
                    )
                inventory_kind = str(inventory["inventory_kind"])
            if any(
                revision.origin_source_id == source_id
                for revision in self.information_store.list_atomic_information()
            ):
                raise WechatDigestError(
                    "微信 represented item 已存在 Durable Atomic Information。"
                )
            return inventory_kind
        except WechatDigestError:
            raise
        except (
            OSError,
            TypeError,
            ValueError,
            RepresentationError,
            SemanticHandoffError,
            SourceNotFoundError,
        ) as exc:
            raise WechatDigestError(
                "微信 represented pre-provider 证据未能完整读回。"
            ) from exc

    def _verify_pre_provider_items(
        self,
        plan: Mapping[str, object],
        status: Mapping[str, object],
        *,
        allow_run_receipt_only: bool = False,
    ) -> tuple[str, ...]:
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信 pre-provider 恢复状态 items 损坏。")
        represented: list[Mapping[str, object]] = []
        for item in items.values():
            if not isinstance(item, dict):
                raise WechatDigestError("微信 pre-provider 恢复 item 损坏。")
            if item.get("state") in TERMINAL_ITEM_STATES:
                continue
            if (
                item.get("atomic_information_ids") != []
                or item.get("governance_receipt") is not None
                or item.get("governance_metrics") is not None
                or item.get("governance_migration") is not None
                or item.get("semantic_failure") is not None
                or item.get("governance_failure") is not None
                or item.get("pending_human") is not False
                or item.get("context_object_ids") != []
            ):
                raise WechatDigestError(
                    "微信 pre-provider 恢复存在 Semantic 或 Governance 痕迹。"
                )
            if item.get("state") == "planned":
                if (
                    item.get("representation_id") is not None
                    or item.get("privacy_route") is not None
                    or item.get("privacy_categories") != []
                ):
                    raise WechatDigestError(
                        "微信 planned pre-provider item 形态损坏。"
                    )
                continue
            if item.get("state") == "represented":
                if (
                    item.get("privacy_route") is not None
                    or item.get("privacy_categories") != []
                ):
                    raise WechatDigestError(
                        "微信 represented pre-provider privacy 形态损坏。"
                    )
                represented.append(item)
                continue
            raise WechatDigestError(
                "微信 pre-provider 恢复存在未知非终态形态。"
            )
        return tuple(
            self._verify_represented_pre_provider_item(
                plan,
                item,
                allow_run_receipt_only=allow_run_receipt_only,
            )
            for item in represented
        )

    def _value_error_was_pre_provider(
        self,
        plan: Mapping[str, object],
        status: Mapping[str, object],
    ) -> bool:
        if (
            status.get("state") != "failed"
            or status.get("failure_category") != "ValueError"
            or status.get("checkpoint_published") is not False
        ):
            return False
        self._verify_pre_provider_items(plan, status)
        return True

    def _semantic_handoff_error_was_pre_provider(
        self,
        plan: Mapping[str, object],
        status: Mapping[str, object],
    ) -> bool:
        if (
            status.get("state") != "failed"
            or status.get("failure_category") != "SemanticHandoffError"
            or status.get("checkpoint_published") is not False
        ):
            return False
        inventories = self._verify_pre_provider_items(
            plan,
            status,
            allow_run_receipt_only=True,
        )
        return bool(inventories) and "run_receipt_only" in inventories

    def _inspect_committed_result_wave(
        self,
        run_id: str,
        plan: Mapping[str, object],
        status: Mapping[str, object],
        *,
        allow_reviewed_head_extension: bool = False,
    ) -> dict[str, dict[str, object]] | None:
        """Read the durable prepared prefix before normalizing a failed run."""

        if (
            status.get("state") != "failed"
            or status.get("failure_category") != "SemanticHandoffError"
            or status.get("checkpoint_published") is not False
        ):
            return None
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError(
                "微信 committed-result wave 状态 items 损坏。"
            )
        ordered: list[tuple[str, Mapping[str, object], bool]] = []
        ordered.extend(
            (
                f"attachment:{item['attachment_key']}",
                item,
                False,
            )
            for item in _plan_sequence(plan.get("attachments"), "attachments")
        )
        ordered.extend(
            (
                f"conversation:{item['conversation_key']}",
                item,
                True,
            )
            for item in _plan_sequence(plan.get("conversations"), "conversations")
        )
        tail_started = False
        pristine_started = False
        wave_items: list[
            tuple[str, str, SemanticPrivacyBinding]
        ] = []
        adapter = WechatConversationV2RepresentationAdapter()
        configuration_fingerprint = canonical_configuration_fingerprint({})
        for item_id, item_plan, is_conversation in ordered:
            item = self._item(items, item_id)
            if item.get("state") in TERMINAL_ITEM_STATES:
                if tail_started:
                    raise WechatDigestError(
                        "微信 committed-result wave 非终态尾部不连续。"
                    )
                continue
            tail_started = True
            if not is_conversation or item.get("state") not in {
                "planned",
                "represented",
            }:
                return None
            if (
                item.get("atomic_information_ids") != []
                or item.get("governance_receipt") is not None
                or item.get("governance_metrics") is not None
                or item.get("governance_migration") is not None
                or item.get("semantic_failure") is not None
                or item.get("governance_failure") is not None
                or item.get("pending_human") is not False
                or item.get("context_object_ids") != []
                or item.get("privacy_route") is not None
                or item.get("privacy_categories") != []
            ):
                raise WechatDigestError(
                    "微信 committed-result wave 存在长期业务写入痕迹。"
                )
            source_id = str(item_plan["source_id"])
            expected_representation_id = representation_id(
                source_id=source_id,
                source_content_hash=item_plan["content_hash"],
                kind=adapter.kind,
                adapter_name=adapter.name,
                adapter_version=adapter.version,
                configuration_fingerprint=configuration_fingerprint,
            )
            try:
                source = self.source_repository.get(source_id)
            except SourceNotFoundError:
                source = None
            try:
                representation = self.representation_repository.get(
                    expected_representation_id
                )
            except RepresentationError:
                representation = None
            trace_exists = self._semantic_trace_exists(
                expected_representation_id
            )
            if source is None and representation is None and not trace_exists:
                pristine_started = True
                if item.get("state") != "planned" or item.get(
                    "representation_id"
                ) is not None:
                    raise WechatDigestError(
                        "微信 committed-result wave pristine tail 损坏。"
                    )
                continue
            if source is not None and representation is not None and not trace_exists:
                return None
            if pristine_started:
                raise WechatDigestError(
                    "微信 committed-result wave durable prefix 不连续。"
                )
            if (
                source is None
                or source.content_hash != item_plan.get("content_hash")
                or source.size_bytes != item_plan.get("size_bytes")
                or not self.source_service.verify(source_id).verified
                or representation is None
                or representation.source_id != source_id
                or representation.representation_id
                != expected_representation_id
                or representation.adapter_name != adapter.name
                or representation.adapter_version != adapter.version
                or representation.status != "complete"
                or representation.completeness != 1.0
                or representation.warnings
                or not self.representation_service.verify(
                    expected_representation_id
                ).verified
                or not trace_exists
                or item.get("state") == "represented"
                and item.get("representation_id")
                != expected_representation_id
                or item.get("state") == "planned"
                and item.get("representation_id") is not None
            ):
                raise WechatDigestError(
                    "微信 committed-result wave Source/Representation binding 损坏。"
                )
            privacy = self.privacy_gate.evaluate(
                self._representation_texts(expected_representation_id),
                semantic_completeness_known=True,
            )
            if privacy.route != "approved":
                raise WechatDigestError(
                    "微信 committed-result wave privacy binding 漂移。"
                )
            wave_items.append(
                (
                    item_id,
                    expected_representation_id,
                    self._semantic_privacy_binding(
                        expected_representation_id, privacy
                    ),
                )
            )
        if not wave_items:
            return None
        authority_binding = self._semantic_authority_binding(
            run_id,
            allow_reviewed_head_extension=allow_reviewed_head_extension,
        )
        try:
            inspections = self._semantic_port().inspect_recovery_wave(
                tuple(
                    SemanticResultOnlyRequest(
                        representation_id=representation_id_value,
                        privacy_binding=privacy_binding,
                        authority_binding=authority_binding,
                    )
                    for _item_id, representation_id_value, privacy_binding in wave_items
                )
            )
        except SemanticHandoffError as exc:
            raise WechatDigestError(
                "微信 committed-result wave 未通过只读 preflight。"
            ) from exc
        if len(inspections) != len(wave_items):
            raise WechatDigestError(
                "微信 committed-result wave inspect 数量不匹配。"
            )
        if all(
            inspection.get("classification") == "pre_provider"
            for inspection in inspections
        ):
            return None
        if any(
            inspection.get("classification") == "pre_provider"
            for inspection in inspections
        ):
            raise WechatDigestError(
                "微信 committed-result wave 不接受混合恢复分类。"
            )
        result: dict[str, dict[str, object]] = {}
        previous_upper: int | None = None
        for (item_id, representation_id_value, _privacy), inspection in zip(
            wave_items, inspections, strict=True
        ):
            ordinal_range = inspection.get("global_ordinal_range")
            if (
                inspection.get("classification")
                != "recoverable_committed_result_wave"
                or inspection.get("representation_id")
                != representation_id_value
                or inspection.get("phase")
                not in {
                    "package_pending_ingestion",
                    "result_pending_package",
                    "already_ingested_pending_status",
                }
                or not isinstance(ordinal_range, list)
                or len(ordinal_range) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in ordinal_range
                )
                or previous_upper is not None
                and ordinal_range[0] != previous_upper + 1
            ):
                raise WechatDigestError(
                    "微信 committed-result wave inspect binding 损坏。"
                )
            previous_upper = int(ordinal_range[1])
            result[item_id] = dict(inspection)
        return result

    def _installed_item_governance_startup_recovery(
        self, run_id: str, status: Mapping[str, object]
    ) -> bool:
        if status.get("state") != "failed":
            return False
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        recoveries, retries = self._governance_startup_history(run_id)
        current_items = [
            (item_id, item)
            for item_id, item in items.items()
            if isinstance(item_id, str)
            and isinstance(item, dict)
            and item.get("state") == "represented"
            and isinstance(item.get("governance_receipt"), dict)
            and item["governance_receipt"].get("phase") == "started"
        ]
        if len(current_items) != 1:
            return False
        current_item_id, current_item = current_items[0]
        current_started = _validated_governance_receipt(
            current_item.get("governance_receipt")
        )
        current_started_fingerprint = _sha256_bytes(
            _canonical_json(current_started).encode("utf-8")
        )
        generic = [
            recovery
            for recovery in recoveries
            if isinstance(recovery.get("recovery_binding"), dict)
            and "semantic_snapshot_fingerprint" in recovery["recovery_binding"]
            and recovery["recovery_binding"].get("item_id") == current_item_id
            and recovery["recovery_binding"].get(
                "governance_started_receipt_fingerprint"
            )
            == current_started_fingerprint
        ]
        if not generic:
            return False
        if len(generic) != 1:
            raise WechatDigestError("Governance 单项启动恢复 receipt 不唯一。")
        recovery = generic[0]
        if any(
            retry.get("recovery_receipt_fingerprint") == recovery.get("receipt_fingerprint")
            for retry in retries
        ):
            raise WechatDigestError("Governance 单项启动恢复机会已消费；禁止再次调用 Provider。")
        binding = recovery["recovery_binding"]
        assert isinstance(binding, dict)
        item_id = binding.get("item_id")
        item = items.get(item_id) if isinstance(item_id, str) else None
        if not isinstance(item, dict):
            raise WechatDigestError("Governance 单项启动恢复 item 缺失。")
        receipt = _validated_governance_receipt(item.get("governance_receipt"))
        if (
            item.get("state") != "represented"
            or item.get("atomic_information_ids") != binding.get("ordered_atomic_information_ids")
            or receipt.get("phase") != "started"
            or _sha256_bytes(_canonical_json(receipt).encode("utf-8"))
            != binding.get("governance_started_receipt_fingerprint")
        ):
            raise WechatDigestError("Governance 单项启动恢复 item binding 漂移。")
        authority_ref = recovery.get("authority_ref")
        if not isinstance(authority_ref, str):
            raise WechatDigestError("Governance 单项启动恢复 authority ref 损坏。")
        expected = self._build_item_governance_startup_recovery_manifest_unlocked(
            authority_ref=authority_ref,
            allow_existing=True,
        )
        semantic = expected.get("semantic_snapshot")
        if (
            recovery.get("authority_manifest_fingerprint")
            != expected.get("manifest_fingerprint")
            or recovery.get("recovery_binding")
            != expected.get("recovery_binding")
            or recovery.get("atomic_effect_bindings")
            != expected.get("atomic_effect_bindings")
            or recovery.get("business_tree_fingerprint")
            != expected.get("business_tree_fingerprint")
            or not isinstance(semantic, dict)
            or recovery.get("semantic_continuation_fingerprint")
            != semantic.get("global_authority_fingerprint")
        ):
            raise WechatDigestError("Governance 单项启动恢复完整现场漂移。")
        return True

    def _load_or_upgrade_active_capture(
        self,
        run_id: str,
        plan: dict[str, object],
        status: dict[str, object],
        *,
        governance_startup_recovery: bool = False,
    ) -> tuple[WechatCapture, dict[str, object], dict[str, object]]:
        schema = plan.get("schema_version")
        if schema not in {
            RUN_PLAN_SCHEMA_VERSION,
            SNAPSHOT_RUN_PLAN_SCHEMA_VERSION,
        }:
            raise WechatDigestError(
                "active legacy 微信运行必须先完成既有显式升级。"
            )
        committed_result_wave = self._inspect_committed_result_wave(
            run_id,
            plan,
            status,
        )
        normalize_recoverable_failure = (
            committed_result_wave is not None
            or self._value_error_was_pre_provider(plan, status)
            or self._semantic_handoff_error_was_pre_provider(plan, status)
            or governance_startup_recovery
        )
        if normalize_recoverable_failure:
            self._verify_plan_and_status(
                run_id,
                None,
                plan,
                status,
            )
        previous_plan = (
            _capture_plan_projection(plan)
            if schema == SNAPSHOT_RUN_PLAN_SCHEMA_VERSION
            else dict(plan)
        )
        previous_fingerprint = _plan_fingerprint(previous_plan)
        existing_receipt = self.run_store.capture_receipt(run_id)
        if existing_receipt is None:
            pending_started = time.monotonic()
            pending = self.run_store.pending_capture_for_run(run_id)
            if pending is not None and self._segment_performance:
                self._segment_performance["snapshot_readback_ms"] += round(
                    (time.monotonic() - pending_started) * 1000
                )
            if pending is None:
                after = WechatCursor.from_dict(
                    previous_plan["after_cursor"], "plan.after_cursor"
                )
                upper = WechatCursor.from_dict(
                    previous_plan["upper_bound"], "plan.upper_bound"
                )
                capture, capture_ms = self._observed_capture(
                    after,
                    upper_bound=upper,
                    reason="active_capture_recovery",
                )
                self._verify_capture_against_plan(capture, previous_plan)
                self.run_store.publish_capture_pending(
                    previous_plan,
                    capture,
                    capture_ms=capture_ms,
                )
            else:
                capture, pending_receipt = pending
                capture_ms = int(pending_receipt["capture_ms"])
                self._verify_capture_against_plan(capture, previous_plan)
            existing_receipt = self.run_store.publish_capture_artifacts(
                run_id,
                capture,
                plan_binding_fingerprint=previous_fingerprint,
                capture_ms=capture_ms,
            )
        else:
            readback_started = time.monotonic()
            capture, existing_receipt = self.run_store.load_capture_artifacts(
                run_id,
                expected_plan_binding=previous_fingerprint,
            )
            if self._segment_performance:
                self._segment_performance["snapshot_readback_ms"] += round(
                    (time.monotonic() - readback_started) * 1000
                )
        self._verify_capture_against_plan(capture, previous_plan)
        self._committed_result_wave = committed_result_wave or {}
        if schema == SNAPSHOT_RUN_PLAN_SCHEMA_VERSION:
            self._verify_plan_and_status(run_id, capture, plan, status)
            normalized_status = dict(status)
            if normalize_recoverable_failure:
                normalized_status["state"] = "processing"
                normalized_status["failure_category"] = None
                normalized_status["updated_at"] = self.clock()
                self.run_store.update_status(run_id, normalized_status)
                if self.run_store.status(run_id) != normalized_status:
                    raise WechatDigestError(
                        "微信 recoverable failure 规范化读回失败。"
                    )
            self._verify_plan_and_status(
                run_id,
                capture,
                plan,
                normalized_status,
            )
            return capture, plan, normalized_status
        if schema == RUN_PLAN_SCHEMA_VERSION:
            self._verify_plan_and_status(
                run_id,
                capture,
                previous_plan,
                status,
            )
        created_at = previous_plan.get("created_at")
        if not isinstance(created_at, str):
            raise WechatDigestError("微信 capture upgrade created_at 损坏。")
        target_plan, _ = _build_plan(
            capture,
            clock=lambda: created_at,
            run_id=run_id,
            created_at=created_at,
            semantic_batch_size=self._plan_batch_size(previous_plan),
            all_history_upper_bound=self._plan_all_history_upper(previous_plan),
            capture_receipt_fingerprint=str(
                existing_receipt["receipt_fingerprint"]
            ),
        )
        if _capture_plan_projection(target_plan) != previous_plan:
            raise WechatDigestError("微信 capture upgrade 与 v3 plan 不一致。")
        target_status = dict(status)
        target_status["plan_fingerprint"] = _plan_fingerprint(target_plan)
        if normalize_recoverable_failure:
            target_status["state"] = "processing"
            target_status["failure_category"] = None
            target_status["updated_at"] = self.clock()
        self.run_store.complete_capture_upgrade(
            run_id,
            target_plan,
            target_status,
        )
        loaded, _ = self.run_store.load_capture_artifacts(
            run_id,
            plan=target_plan,
        )
        self._verify_plan_and_status(
            run_id,
            loaded,
            target_plan,
            target_status,
        )
        return loaded, target_plan, target_status

    def _run_locked(
        self,
        *,
        since: str | None,
        from_now: bool,
        all_history: bool,
        max_terminal_items: int | None = None,
    ) -> WechatDigestResult:
        self._committed_result_wave = {}
        self._capture_reasons = []
        self._segment_performance = {
            "upper_bound_probe_calls": 0,
            "capture_attempts": 0,
            "capture_successes": 0,
            "capture_provider_calls": 0,
            "materialized_cursor_rows": 0,
            "cursor_discovery_ms": 0,
            "capture_ms": 0,
            "snapshot_publish_ms": 0,
            "snapshot_readback_ms": 0,
            "slice_build_ms": 0,
            "semantic_peak_concurrency": 0,
            "semantic_wall_ms": 0,
            "semantic_serial_estimate_ms": 0,
            "commit_wall_ms": 0,
            "governance_wall_ms": 0,
            "governance_peak_concurrency": 0,
            "checkpoint_wall_ms": 0,
            "resume_provider_calls": 0,
            "completed_window_connector_replays": 0,
        }
        checkpoint = self.run_store.checkpoint()
        active_run_id = self.run_store.active_run_id()
        replayed = active_run_id is not None
        if active_run_id is not None:
            if any((since is not None, from_now, all_history)):
                raise WechatDigestError("存在未完成运行；请先原样恢复，不要更改首次起点。")
            plan = self.run_store.plan(active_run_id)
            if plan.get("schema_version") == PREVIOUS_RUN_PLAN_SCHEMA_VERSION:
                raise WechatDigestError(
                    "active v2 微信运行必须显式冻结全历史边界。"
                )
            status = self.run_store.status(active_run_id)
            governance_startup_recovery = self._installed_item_governance_startup_recovery(
                active_run_id, status
            )
            capture, plan, status = self._load_or_upgrade_active_capture(
                active_run_id,
                plan,
                status,
                governance_startup_recovery=governance_startup_recovery,
            )
            after = WechatCursor.from_dict(plan["after_cursor"], "plan.after_cursor")
            upper = WechatCursor.from_dict(plan["upper_bound"], "plan.upper_bound")
            if self.semantic_batch_size != self._plan_batch_size(plan):
                raise WechatDigestError("semantic batch size 与 durable run 不一致。")
            self._verify_plan_and_status(active_run_id, capture, plan, status)
            all_history_upper = self._plan_all_history_upper(plan)
            if (
                status.get("state") == "completed"
                and all_history_upper is not None
                and upper < all_history_upper
            ):
                if not status.get("checkpoint_published") or checkpoint != upper:
                    raise WechatDigestError(
                        "微信全历史窗口 checkpoint 与完成状态不一致。"
                    )
                next_capture, capture_ms = self._observed_capture(
                    upper,
                    all_history_upper_bound=all_history_upper,
                    reason="next_history_window",
                )
                if next_capture.upper_bound <= upper:
                    raise WechatDigestError(
                        "冻结的微信全历史边界无法继续读回。"
                    )
                created_at = plan.get("created_at")
                if not isinstance(created_at, str):
                    raise WechatDigestError("微信运行计划损坏。")
                plan, status = self._persist_new_capture_plan(
                    next_capture,
                    created_at=created_at,
                    all_history_upper_bound=all_history_upper,
                    capture_ms=capture_ms,
                )
                active_run_id = str(plan["run_id"])
                capture = next_capture
        else:
            all_history_upper: WechatCursor | None = None
            capture_ms = 0
            pending_started = time.monotonic()
            pending_capture = self.run_store.pending_capture()
            if pending_capture is not None:
                self._segment_performance["snapshot_readback_ms"] += round(
                    (time.monotonic() - pending_started) * 1000
                )
            if pending_capture is not None:
                capture, pending = pending_capture
                pending_upper = pending.get("all_history_upper_bound")
                all_history_upper = (
                    None
                    if pending_upper is None
                    else WechatCursor.from_dict(
                        pending_upper,
                        "capture.pending.all_history_upper_bound",
                    )
                )
                capture_ms = int(pending["capture_ms"])
                plan, status = self._persist_new_capture_plan(
                    capture,
                    created_at=str(pending["created_at"]),
                    all_history_upper_bound=all_history_upper,
                    capture_ms=capture_ms,
                )
                active_run_id = str(plan["run_id"])
            elif checkpoint is None:
                if not any((since is not None, from_now, all_history)):
                    raise WechatDigestError(
                        "首次使用必须明确选择 --since、--from-now 或 --all-history。"
                    )
                if from_now:
                    observed, _ = self._observed_capture(
                        ZERO_CURSOR,
                        observe_only=True,
                        upper_probe=True,
                        reason="upper_bound_probe",
                    )
                    capture = WechatCapture(
                        observed.provider_version,
                        observed.upper_bound,
                        observed.upper_bound,
                        (),
                    )
                elif all_history:
                    observed, _ = self._observed_capture(
                        ZERO_CURSOR,
                        observe_only=True,
                        upper_probe=True,
                        reason="upper_bound_probe",
                    )
                    all_history_upper = observed.upper_bound
                    capture, capture_ms = self._observed_capture(
                        ZERO_CURSOR,
                        all_history_upper_bound=all_history_upper,
                        reason="initial_history_window",
                    )
                    if (
                        all_history_upper > ZERO_CURSOR
                        and capture.upper_bound <= ZERO_CURSOR
                    ):
                        raise WechatDigestError(
                            "冻结的微信全历史边界无法读回。"
                        )
                else:
                    after = parse_since(str(since))
                    capture, capture_ms = self._observed_capture(
                        after, reason="since_window"
                    )
            else:
                if any((since is not None, from_now, all_history)):
                    raise WechatDigestError(
                        "checkpoint 已存在；日常运行不要再指定首次起点。"
                    )
                capture, capture_ms = self._observed_capture(
                    checkpoint, reason="checkpoint_window"
                )
            if pending_capture is None:
                plan, status = self._persist_new_capture_plan(
                    capture,
                    all_history_upper_bound=all_history_upper,
                    capture_ms=capture_ms,
                )
                active_run_id = str(plan["run_id"])

        assert active_run_id is not None
        try:
            result = self._process(
                capture,
                plan,
                status,
                replayed=replayed,
                max_terminal_items=max_terminal_items,
            )
        except Exception as exc:
            failed = self.run_store.status(active_run_id)
            self._verify_plan_and_status(
                active_run_id,
                capture,
                plan,
                failed,
            )
            if (
                failed.get("run_id") != active_run_id
                or failed.get("plan_fingerprint")
                != _plan_fingerprint(plan)
            ):
                raise WechatDigestError(
                    "微信失败收敛前 durable status binding 漂移。"
                ) from exc
            failed["state"] = "failed"
            failed["failure_category"] = exc.__class__.__name__
            failed["updated_at"] = self.clock()
            self.run_store.update_status(active_run_id, failed)
            if isinstance(exc, WechatDigestError):
                raise
            raise WechatDigestError(
                "微信信息消化未安全完成；checkpoint 未推进。"
            ) from exc
        return result

    def _verify_capture_against_plan(
        self, capture: WechatCapture, plan: Mapping[str, object]
    ) -> None:
        if (
            capture.provider_version != plan.get("provider_version")
            or _capture_fingerprint(capture) != plan.get("capture_fingerprint")
        ):
            raise WechatDigestError("微信 fixed range 重放内容发生变化。")

    def _process(
        self,
        capture: WechatCapture,
        plan: dict[str, object],
        status: dict[str, object],
        *,
        replayed: bool,
        max_terminal_items: int | None = None,
    ) -> WechatDigestResult:
        run_id = str(plan["run_id"])
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        attachment_plans = _plan_sequence(plan.get("attachments"), "attachments")
        attachment_source_ids = {
            str(item["attachment_key"]): (
                None if item.get("source_id") is None else str(item["source_id"])
            )
            for item in attachment_plans
        }
        slice_started = time.monotonic()
        capture_index = self.run_store.load_capture_index(
            run_id, capture=capture
        )
        by_attachment = {
            str(entry["attachment_key"]): next(
                attachment
                for attachment in capture.messages[int(entry["message_index"])].attachments
                if attachment.attachment_key == entry["attachment_key"]
            )
            for entry in _plan_sequence(
                capture_index.get("attachments"),
                "capture.index.attachments",
            )
        }
        conversation_indexes = {
            str(value["conversation_key"]): tuple(value["message_indexes"])
            for value in _plan_sequence(
                capture_index.get("conversations"),
                "capture.index.conversations",
            )
        }
        self._segment_performance["slice_build_ms"] += round(
            (time.monotonic() - slice_started) * 1000
        )
        completed_items = 0

        def safe_stop_if_needed() -> WechatDigestResult | None:
            if max_terminal_items is None or completed_items < max_terminal_items:
                return None
            current_status = self.run_store.status(run_id)
            current_items = current_status.get("items")
            if not isinstance(current_items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            remaining = sum(
                not isinstance(candidate, dict)
                or candidate.get("state") not in TERMINAL_ITEM_STATES
                for candidate in current_items.values()
            )
            if remaining == 0:
                return None
            return self._result(
                run_id,
                current_status,
                replayed=replayed,
                segment_safe_stopped=True,
                segment_items_completed=completed_items,
                segment_remaining_items=remaining,
                segment_stop_reason="item_limit",
            )

        for attachment_plan in attachment_plans:
            item_id = f"attachment:{attachment_plan['attachment_key']}"
            item = self._item(items, item_id)
            if self._terminal_item_valid(item):
                continue
            if attachment_plan["status"] != "available":
                self._update_item(run_id, status, item_id, state="unsupported")
            else:
                captured = by_attachment.get(str(attachment_plan["attachment_key"]))
                if captured is None or captured.path is None:
                    raise WechatDigestError("微信附件重放无法精确定位。")
                self._process_attachment(
                    run_id, status, item_id, attachment_plan, captured
                )
            completed_items += 1
            if (segment_result := safe_stop_if_needed()) is not None:
                return segment_result

        conversation_plans = _plan_sequence(
            plan.get("conversations"), "conversations"
        )
        conversation_index = 0
        while conversation_index < len(conversation_plans):
            remaining_capacity = self.semantic_parallelism
            if max_terminal_items is not None:
                remaining_capacity = min(
                    remaining_capacity,
                    max_terminal_items - completed_items,
                )
            if remaining_capacity <= 0:
                if (segment_result := safe_stop_if_needed()) is not None:
                    return segment_result
                break
            prepared: list[
                tuple[
                    dict[str, object],
                    str,
                    bytes,
                    str,
                    SemanticPrivacyBinding,
                ]
            ] = []
            while (
                conversation_index < len(conversation_plans)
                and len(prepared) < remaining_capacity
            ):
                conversation_plan = conversation_plans[conversation_index]
                conversation_index += 1
                item_id = (
                    f"conversation:{conversation_plan['conversation_key']}"
                )
                item = self._item(items, item_id)
                if self._terminal_item_valid(item):
                    continue
                slice_started = time.monotonic()
                payload = _conversation_source_payload(
                    capture,
                    str(conversation_plan["conversation_key"]),
                    attachment_source_ids,
                    message_indexes=conversation_indexes.get(
                        str(conversation_plan["conversation_key"])
                    ),
                )
                self._segment_performance["slice_build_ms"] += round(
                    (time.monotonic() - slice_started) * 1000
                )
                if (
                    _sha256_bytes(payload) != conversation_plan["content_hash"]
                    or len(payload) != conversation_plan["size_bytes"]
                ):
                    raise WechatDigestError(
                        "微信 Conversation Source 重放不一致。"
                    )
                if item_id in self._committed_result_wave:
                    self._process_conversation(
                        run_id,
                        status,
                        item_id,
                        conversation_plan,
                        payload,
                        persist_prepared_state=(
                            item.get("state") == "planned"
                        ),
                        recover_committed_result=True,
                    )
                    completed_items += 1
                    if (segment_result := safe_stop_if_needed()) is not None:
                        return segment_result
                    continue
                representation_id = self._process_conversation(
                    run_id,
                    status,
                    item_id,
                    conversation_plan,
                    payload,
                    prepare_only=True,
                    persist_prepared_state=not prepared,
                )
                if representation_id is None:
                    current_item = self._item(
                        self.run_store.status(run_id)["items"], item_id
                    )
                    if current_item.get("state") not in TERMINAL_ITEM_STATES:
                        conversation_index -= 1
                        break
                    completed_items += 1
                    if (segment_result := safe_stop_if_needed()) is not None:
                        return segment_result
                    continue
                privacy = self.privacy_gate.evaluate(
                    self._representation_texts(representation_id),
                    semantic_completeness_known=True,
                )
                prepared.append(
                    (
                        conversation_plan,
                        item_id,
                        payload,
                        representation_id,
                        self._semantic_privacy_binding(
                            representation_id, privacy
                        ),
                    )
                )
            if prepared:
                authority_binding = self._semantic_authority_binding(run_id)
                semantic_started = time.monotonic()
                semantic_port = self._semantic_port()
                elapsed_by_representation = semantic_port.prepare_results(
                    tuple(
                        SemanticResultOnlyRequest(
                            representation_id=representation_id,
                            privacy_binding=privacy_binding,
                            authority_binding=authority_binding,
                        )
                        for (
                            _conversation_plan,
                            _item_id,
                            _payload,
                            representation_id,
                            privacy_binding,
                        ) in prepared
                    ),
                    parallelism=self.semantic_parallelism,
                )
                self._segment_performance["semantic_wall_ms"] += round(
                    (time.monotonic() - semantic_started) * 1000
                )
                self._segment_performance["semantic_serial_estimate_ms"] += sum(
                    elapsed_by_representation.values()
                )
                observed_metrics = getattr(
                    semantic_port, "last_prepare_metrics", {}
                )
                self._segment_performance["semantic_peak_concurrency"] = max(
                    self._segment_performance["semantic_peak_concurrency"],
                    int(observed_metrics.get("semantic_peak_concurrency", 0)),
                )
                if replayed:
                    self._segment_performance["resume_provider_calls"] += int(
                        observed_metrics.get("resume_provider_calls", 0)
                    )
                for (
                    conversation_plan,
                    item_id,
                    payload,
                    _representation_id,
                    _privacy_binding,
                ) in prepared:
                    self._process_conversation(
                        run_id,
                        status,
                        item_id,
                        conversation_plan,
                        payload,
                    )
                    completed_items += 1
                    if (segment_result := safe_stop_if_needed()) is not None:
                        return segment_result

        current_status = self.run_store.status(run_id)
        current_items = current_status.get("items")
        if not isinstance(current_items, dict) or any(
            not isinstance(item, dict) or item.get("state") not in TERMINAL_ITEM_STATES
            for item in current_items.values()
        ):
            raise WechatDigestError("微信运行尚未达到 terminal convergence。")
        converged = dict(current_status)
        converged["state"] = "converged"
        converged["failure_category"] = None
        converged["updated_at"] = self.clock()
        self.run_store.update_status(run_id, converged)
        upper = WechatCursor.from_dict(plan["upper_bound"], "plan.upper_bound")
        all_history_upper = self._plan_all_history_upper(plan)
        if all_history_upper is None or upper == all_history_upper:
            return self._result(
                run_id,
                converged,
                replayed=replayed,
                segment_items_completed=completed_items,
            )
        checkpoint_started = time.monotonic()
        try:
            self.run_store.publish_checkpoint(run_id, upper)
            converged["checkpoint_published"] = True
            converged["state"] = "completed"
            converged["updated_at"] = self.clock()
            self.run_store.update_status(run_id, converged)
        finally:
            self._segment_performance["checkpoint_wall_ms"] += round(
                (time.monotonic() - checkpoint_started) * 1000
            )
        return self._result(
            run_id,
            converged,
            replayed=replayed,
            segment_items_completed=completed_items,
        )

    @staticmethod
    def _item(items: dict[object, object], item_id: str) -> dict[str, object]:
        item = items.get(item_id)
        if not isinstance(item, dict):
            raise WechatDigestError("微信运行 item 记录损坏。")
        return item

    def _update_item(
        self,
        run_id: str,
        status: dict[str, object],
        item_id: str,
        **changes: object,
    ) -> None:
        current = self.run_store.status(run_id)
        items = current.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        item = self._item(items, item_id)
        updated_item = dict(item)
        updated_item.update(changes)
        updated_items = dict(items)
        updated_items[item_id] = updated_item
        current["items"] = updated_items
        current["state"] = "processing"
        current["updated_at"] = self.clock()
        self.run_store.update_status(run_id, current)
        status.clear()
        status.update(current)

    def _terminal_item_valid(self, item: Mapping[str, object]) -> bool:
        if item.get("state") not in TERMINAL_ITEM_STATES:
            return False
        governance_failed_closed = False
        if item.get("state") == "failed_closed":
            semantic_failure = item.get("semantic_failure")
            governance_failure = item.get("governance_failure")
            if (semantic_failure is None) == (governance_failure is None):
                raise WechatDigestError(
                    "微信 terminal failed_closed failure variant 损坏。"
                )
            if governance_failure is not None:
                self._verify_governance_failed_closed_item(item)
                governance_failed_closed = True
        source_id = item.get("source_id")
        if source_id is not None:
            if not isinstance(source_id, str):
                raise WechatDigestError("微信 terminal item Source identity 损坏。")
            verification = self.source_service.verify(source_id)
            if not verification.verified:
                raise WechatDigestError("微信 terminal item Source 读回失败。")
        representation_id = item.get("representation_id")
        if representation_id is not None and (
            not isinstance(representation_id, str)
            or not self.representation_service.verify(representation_id).verified
        ):
            raise WechatDigestError("微信 terminal item Representation 读回失败。")
        atomic_ids = item.get("atomic_information_ids")
        if not isinstance(atomic_ids, list):
            raise WechatDigestError("微信 terminal item Information 记录损坏。")
        if item.get("governance_metrics") is not None:
            _validated_governance_metrics(item["governance_metrics"])
        if item.get("governance_receipt") is not None:
            receipt = _validated_governance_receipt(item["governance_receipt"])
            if (
                receipt["phase"] != "completed"
                and not governance_failed_closed
            ):
                raise WechatDigestError("微信 terminal item Governance receipt 未完成。")
            if atomic_ids and receipt["atomic_information_fingerprint"] != (
                _governance_atomic_fingerprint(atomic_ids)
            ):
                raise WechatDigestError("微信 terminal item Governance receipt 不一致。")
        if item.get("governance_migration") is not None:
            migration = _validated_governance_migration(
                item["governance_migration"]
            )
            if atomic_ids != migration["ordered_atomic_information_ids"]:
                raise WechatDigestError(
                    "微信 terminal item Governance migration 不一致。"
                )
            if self._governance_effect_fingerprint(
                migration["completed_atomic_information_ids"]
            ) != migration["legacy_effect_fingerprint"]:
                raise WechatDigestError(
                    "微信 terminal item Governance legacy effect 漂移。"
                )
        for atomic_id in atomic_ids:
            if not isinstance(atomic_id, str):
                raise WechatDigestError(
                    "微信 terminal item Information identity 损坏。"
                )
            self.information_store.get_current(atomic_id)
        return True

    def _ensure_source_bytes(
        self,
        *,
        source_id: str,
        payload: bytes,
        expected_hash: str,
        expected_size: int,
        filename_hint: str,
        media_type: str,
        ingested_from: str,
    ) -> None:
        try:
            existing = self.source_service.show(source_id)
        except SourceNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.content_hash != expected_hash
                or existing.size_bytes != expected_size
                or not self.source_service.verify(source_id).verified
            ):
                raise WechatDigestError("Managed Source exact replay collision。")
            return
        with tempfile.TemporaryDirectory(prefix="archeos-wechat-source-") as temp:
            path = Path(temp) / filename_hint
            path.write_bytes(payload)
            admitted = self.source_service.admit(
                path,
                source_id=source_id,
                metadata={
                    "media_type": media_type,
                    "filename_hint": filename_hint,
                    "ingested_from": {
                        "location": ingested_from,
                        "observed_at": self.clock(),
                    },
                },
            ).source
        if (
            admitted.content_hash != expected_hash
            or admitted.size_bytes != expected_size
            or not self.source_service.verify(source_id).verified
        ):
            raise WechatDigestError("Managed Source 写入读回不一致。")

    def _ensure_source_file(
        self,
        *,
        source_id: str,
        path: Path,
        expected_hash: str,
        expected_size: int,
        filename_hint: str,
        media_type: str,
        ingested_from: str,
    ) -> None:
        try:
            existing = self.source_service.show(source_id)
        except SourceNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.content_hash != expected_hash
                or existing.size_bytes != expected_size
                or not self.source_service.verify(source_id).verified
            ):
                raise WechatDigestError("附件 Managed Source exact replay collision。")
            return
        current_hash, current_size = _hash_file(path)
        if current_hash != expected_hash or current_size != expected_size:
            raise WechatDigestError("微信附件在准入前发生变化。")
        admitted = self.source_service.admit(
            path,
            source_id=source_id,
            metadata={
                "media_type": media_type,
                "filename_hint": filename_hint,
                "ingested_from": {
                    "location": ingested_from,
                    "observed_at": self.clock(),
                },
            },
        ).source
        if (
            admitted.content_hash != expected_hash
            or admitted.size_bytes != expected_size
            or not self.source_service.verify(source_id).verified
        ):
            raise WechatDigestError("微信附件 Source 写入读回不一致。")

    def _process_attachment(
        self,
        run_id: str,
        status: dict[str, object],
        item_id: str,
        plan: Mapping[str, object],
        attachment: CapturedAttachment,
        *, prepare_only: bool = False,
    ) -> str | None:
        source_id = str(plan["source_id"])
        assert attachment.content_hash is not None and attachment.size_bytes is not None
        self._ensure_source_file(
            source_id=source_id,
            path=attachment.path,  # type: ignore[arg-type]
            expected_hash=str(plan["content_hash"]),
            expected_size=int(plan["size_bytes"]),
            filename_hint=str(plan["filename_hint"]),
            media_type=str(plan["media_type"]),
            ingested_from=f"wechat://attachment/{attachment.attachment_key}",
        )
        media_type = str(plan["media_type"])
        adapter_name = SEMANTIC_ATTACHMENT_ADAPTERS.get(media_type)
        if adapter_name is None and media_type == "application/octet-stream":
            guessed = mimetypes.guess_type(str(plan["filename_hint"]))[0]
            adapter_name = SEMANTIC_ATTACHMENT_ADAPTERS.get(guessed or "")
        if media_type in IMAGE_MEDIA_TYPES:
            representation = self.representation_service.build(
                source_id,
                production_adapter("image-preflight"),
                {"privacy_route": "restricted"},
            ).representation
            self._update_item(
                run_id,
                status,
                item_id,
                state="unsupported",
                representation_id=representation.representation_id,
                privacy_route="local_only",
                privacy_categories=["unresolved_high_sensitivity"],
            )
            return None
        if adapter_name is None:
            self._update_item(run_id, status, item_id, state="unsupported")
            return None
        representation = self.representation_service.build(
            source_id, production_adapter(adapter_name), {}
        ).representation
        self._update_item(
            run_id,
            status,
            item_id,
            state="represented",
            representation_id=representation.representation_id,
        )
        texts = self._representation_texts(representation.representation_id)
        privacy = self.privacy_gate.evaluate(
            texts,
            semantic_completeness_known=(
                representation.status == "complete"
                and representation.completeness == 1.0
                and not representation.warnings
            ),
        )
        if privacy.route != "approved":
            self._update_item(
                run_id,
                status,
                item_id,
                state="local_only",
                privacy_route=privacy.route,
                privacy_categories=list(privacy.categories),
            )
            return None
        if not self._has_semantic_units(representation.representation_id):
            self._update_item(
                run_id,
                status,
                item_id,
                state="unsupported",
                privacy_route="approved",
                privacy_categories=[],
            )
            return None
        if prepare_only and not self._existing_semantic_package(
            representation.representation_id
        ):
            return representation.representation_id
        commit_started = time.monotonic()
        atomic_ids = self._semantic(run_id, representation.representation_id, privacy)
        self._update_item(
            run_id,
            status,
            item_id,
            atomic_information_ids=list(atomic_ids),
        )
        self._segment_performance["commit_wall_ms"] = self._segment_performance.get(
            "commit_wall_ms", 0
        ) + round((time.monotonic() - commit_started) * 1000)
        pending, object_ids = self._govern_item(
            run_id, status, item_id, atomic_ids
        )
        commit_started = time.monotonic()
        self._update_item(
            run_id,
            status,
            item_id,
            state="pending_human" if pending else "processed",
            privacy_route="approved",
            privacy_categories=[],
            atomic_information_ids=list(atomic_ids),
            pending_human=pending,
            context_object_ids=list(object_ids),
        )
        self._segment_performance["commit_wall_ms"] = self._segment_performance.get(
            "commit_wall_ms", 0
        ) + round((time.monotonic() - commit_started) * 1000)
        return None

    def _process_conversation(
        self,
        run_id: str,
        status: dict[str, object],
        item_id: str,
        plan: Mapping[str, object],
        payload: bytes,
        *,
        prepare_only: bool = False,
        persist_prepared_state: bool = True,
        recover_committed_result: bool = False,
    ) -> str | None:
        source_id = str(plan["source_id"])
        self._ensure_source_bytes(
            source_id=source_id,
            payload=payload,
            expected_hash=str(plan["content_hash"]),
            expected_size=int(plan["size_bytes"]),
            filename_hint=str(plan["filename_hint"]),
            media_type="application/json",
            ingested_from=f"wechat://conversation/{plan['conversation_key']}",
        )
        representation = self.representation_service.build(
            source_id, WechatConversationV2RepresentationAdapter(), {}
        ).representation
        if persist_prepared_state:
            self._update_item(
                run_id,
                status,
                item_id,
                state="represented",
                representation_id=representation.representation_id,
            )
        texts = self._representation_texts(representation.representation_id)
        privacy = self.privacy_gate.evaluate(
            texts,
            semantic_completeness_known=(
                representation.status == "complete"
                and representation.completeness == 1.0
                and not representation.warnings
            ),
        )
        if privacy.route != "approved":
            if prepare_only and not persist_prepared_state:
                return None
            self._update_item(
                run_id,
                status,
                item_id,
                state="local_only",
                privacy_route=privacy.route,
                privacy_categories=list(privacy.categories),
            )
            return None
        if not self._has_semantic_units(representation.representation_id):
            if prepare_only and not persist_prepared_state:
                return None
            self._update_item(
                run_id,
                status,
                item_id,
                state="unsupported",
                privacy_route="approved",
                privacy_categories=[],
            )
            return None
        if prepare_only and not self._existing_semantic_package(
            representation.representation_id
        ):
            return representation.representation_id
        commit_started = time.monotonic()
        atomic_ids = self._semantic(
            run_id,
            representation.representation_id,
            privacy,
            recover_committed_result=recover_committed_result,
        )
        self._update_item(
            run_id,
            status,
            item_id,
            atomic_information_ids=list(atomic_ids),
        )
        self._segment_performance["commit_wall_ms"] = self._segment_performance.get(
            "commit_wall_ms", 0
        ) + round((time.monotonic() - commit_started) * 1000)
        pending, object_ids = self._govern_item(
            run_id, status, item_id, atomic_ids
        )
        commit_started = time.monotonic()
        self._update_item(
            run_id,
            status,
            item_id,
            state="pending_human" if pending else "processed",
            privacy_route="approved",
            privacy_categories=[],
            atomic_information_ids=list(atomic_ids),
            pending_human=pending,
            context_object_ids=list(object_ids),
        )
        self._segment_performance["commit_wall_ms"] = self._segment_performance.get(
            "commit_wall_ms", 0
        ) + round((time.monotonic() - commit_started) * 1000)
        return None

    def _existing_semantic_package(self, representation_id: str) -> bool:
        return (
            self.workspace / "02_processing" / "information" / representation_id
        ).exists()

    def _representation_texts(self, representation_id: str) -> tuple[str, ...]:
        representation = self.representation_repository.get(representation_id)
        texts: list[str] = []
        for artifact in representation.artifacts:
            raw = self.representation_repository.read_artifact(
                representation_id, artifact.artifact_id
            )
            try:
                texts.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        return tuple(texts)

    def _semantic(
        self,
        run_id: str,
        representation_id: str,
        privacy: PrivacyDecision,
        *,
        recover_committed_result: bool = False,
    ) -> tuple[str, ...]:
        privacy_binding = self._semantic_privacy_binding(
            representation_id, privacy
        )
        package_exists = self._existing_semantic_package(representation_id)
        result = self._semantic_port().execute(
            representation_id,
            privacy_binding=privacy_binding,
            authority_binding=(
                None
                if package_exists and not recover_committed_result
                else self._semantic_authority_binding(run_id)
            ),
        )
        atomic_ids = tuple(result.ingestion.atomic_information_ids)
        for atomic_id in atomic_ids:
            self.information_store.get_current(atomic_id)
        return atomic_ids

    def _semantic_privacy_binding(
        self,
        representation_id: str,
        privacy: PrivacyDecision,
    ) -> SemanticPrivacyBinding:
        representation = self.representation_repository.get(representation_id)
        privacy_payload = {
            "policy": SEMANTIC_PRIVACY_POLICY,
            "policy_version": SEMANTIC_PRIVACY_POLICY_VERSION,
            "route": privacy.route,
            "categories": list(privacy.categories),
            "representation_id": representation.representation_id,
            "representation_manifest_fingerprint": _sha256_bytes(
                _canonical_json(representation.to_manifest_dict()).encode("utf-8")
            ),
        }
        return SemanticPrivacyBinding(
            policy=SEMANTIC_PRIVACY_POLICY,
            policy_version=SEMANTIC_PRIVACY_POLICY_VERSION,
            route=privacy.route,
            receipt_fingerprint=_sha256_bytes(_canonical_json(privacy_payload).encode()),
        )

    def _semantic_port(self) -> SemanticHandoffPort:
        if self._semantic_handoff is None:
            self._semantic_handoff = self.semantic_handoff_factory()
        return self._semantic_handoff

    def _has_semantic_units(self, representation_id: str) -> bool:
        representation = self.representation_repository.get(representation_id)
        units = _units_from_representation(
            representation, self.representation_repository
        )
        return any(unit.analysis_eligible for unit in units)

    def _govern(self, atomic_ids: Sequence[str]) -> tuple[bool, tuple[str, ...]]:
        if not atomic_ids:
            return False, ()
        with SQLiteWorldModelRepository(self.database) as repository:
            interpretation_provider = self.interpretation_provider
            if self._before_governance_provider_call is not None:
                interpretation_provider = _GovernanceProviderCallBoundary(
                    interpretation_provider,
                    self._before_governance_provider_call,
                )
            digestion = AtomicInformationDigestionService(
                self.information_store,
                repository,
                ObjectResolver(repository),
                interpretation_provider,
                self.proposal_store,
                self.journal,
                BusinessLanguageHumanJudgmentPort(),
            )
            resume = self._governance_resume_state
            if resume is None:
                migration = self._governance_migration_state
                pending = (
                    bool(migration["pending_human"])
                    if migration is not None
                    else False
                )
                affected: set[str] = (
                    set(migration["context_object_ids"])
                    if migration is not None
                    else set()
                )
                scope_ids = (
                    tuple(migration["remaining_atomic_information_ids"])
                    if migration is not None
                    else tuple(atomic_ids)
                )
                batch_ids = scope_ids
                interpretations = (
                    digestion.interpret_batch(batch_ids) if batch_ids else ()
                )
                if batch_ids and self._after_governance_batch_interpretation:
                    self._after_governance_batch_interpretation(
                        batch_ids,
                        interpretations,
                        pending,
                        tuple(sorted(affected)),
                        self._governance_effect_snapshots(
                            repository, batch_ids
                        ),
                    )
                start_index = 0
            else:
                batch_ids = tuple(resume["batch_atomic_information_ids"])
                interpretations = tuple(resume["interpretations"])
                start_index = int(resume["next_index"])
                pending = bool(resume["pending_human"])
                affected = set(resume["context_object_ids"])
                if not set(batch_ids).issubset(set(atomic_ids)):
                    raise WechatDigestError(
                        "微信 Governance resume batch 不属于当前 item。"
                    )

            identity = IdentityGateService(
                self.information_store,
                repository,
                self.proposal_store,
                self.journal,
                BusinessLanguageHumanJudgmentPort(),
            )
            retriever = BoundedInformationCandidateRetriever()
            for index in range(start_index, len(batch_ids)):
                if self._before_governance_application is not None:
                    self._before_governance_application(index)
                atomic_id = batch_ids[index]
                identity_pending = False
                current = self.information_store.get_current(atomic_id)
                for concern in current.raw_concerns:
                    current = self.information_store.get_current(atomic_id)
                    exact_object_ids = {
                        assignment.object_id
                        for record in repository.list_objects()
                        if record.status == "active"
                        for assignment in repository.list_names(record.object_id)
                        if " ".join(assignment.name.split()).casefold()
                        == " ".join(concern.split()).casefold()
                    }
                    if (
                        len(exact_object_ids) == 1
                        and exact_object_ids.issubset(current.related_object_ids)
                    ):
                        affected.update(exact_object_ids)
                        continue
                    result = identity.process(
                        atomic_id,
                        IdentityEvidence(
                            name=concern,
                            supporting_revision_ids=(current.revision_id,),
                            identity_bases=(),
                        ),
                    )
                    if result.outcome == "human_review":
                        identity_pending = True
                        pending = True
                        break
                    if result.object_id is not None:
                        affected.add(result.object_id)
                current = self.information_store.get_current(atomic_id)
                pool = self.information_store.list_atomic_information()
                retriever.retrieve(
                    current,
                    pool[-512:],
                    pool_complete=len(pool) <= 512,
                )
                if identity_pending:
                    affected.update(current.related_object_ids)
                    if self._after_governance_application is not None:
                        self._after_governance_application(
                            index + 1,
                            pending,
                            tuple(sorted(affected)),
                            self._governance_effect_snapshots(
                                repository, batch_ids
                            ),
                        )
                    continue
                digest_result = digestion.apply_interpretation(
                    atomic_id, interpretations[index]
                )
                if digest_result.proposal_id is not None:
                    pending = True
                affected.update(digest_result.atomic_information.related_object_ids)
                if self._after_governance_application is not None:
                    self._after_governance_application(
                        index + 1,
                        pending,
                        tuple(sorted(affected)),
                        self._governance_effect_snapshots(
                            repository, batch_ids
                        ),
                    )

            builder = ContextBuilder(
                repository,
                ObjectResolver(repository),
                self.information_store,
                self.journal,
                self.proposal_store,
            )
            for object_id in sorted(affected):
                bundle = builder.build(
                    ContextRequest(scope="object", object_id=object_id)
                )
                if bundle.root.object_id != object_id:
                    raise WechatDigestError("受影响 Object 的 Context 读回不一致。")
        return pending, tuple(sorted(affected))

    def _governance_startup_history(
        self, run_id: str
    ) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
        recoveries = self.run_store.governance_startup_recoveries(run_id)
        retries = self.run_store.governance_startup_retries(run_id)
        by_fingerprint: dict[str, list[dict[str, object]]] = {}
        for recovery in recoveries:
            fingerprint = str(recovery["receipt_fingerprint"])
            by_fingerprint.setdefault(fingerprint, []).append(recovery)
        for retry in retries:
            matches = by_fingerprint.get(
                str(retry["recovery_receipt_fingerprint"]), []
            )
            if len(matches) != 1:
                raise WechatDigestError(
                    "Governance 启动恢复 retry 历史无法唯一绑定。"
                )
            binding = matches[0]["recovery_binding"]
            assert isinstance(binding, dict)
            if (
                retry.get("run_id") != run_id
                or retry.get("item_id") != binding.get("item_id")
                or retry.get("atomic_information_fingerprint")
                != _governance_atomic_fingerprint(
                    binding.get("ordered_atomic_information_ids", [])
                )
            ):
                raise WechatDigestError(
                    "Governance 启动恢复 retry 历史 binding 损坏。"
                )
        return recoveries, retries

    def _startup_recovery_for_item(
        self,
        *,
        run_id: str,
        item_id: str,
        atomic_ids: Sequence[str],
        started_receipt: Mapping[str, object],
    ) -> dict[str, object] | None:
        recoveries, retries = self._governance_startup_history(run_id)
        started_fingerprint = _sha256_bytes(
            _canonical_json(started_receipt).encode("utf-8")
        )
        matching: list[dict[str, object]] = []
        for recovery in recoveries:
            binding = recovery["recovery_binding"]
            assert isinstance(binding, dict)
            if (
                binding.get("run_id") == run_id
                and binding.get("item_id") == item_id
                and binding.get("ordered_atomic_information_ids")
                == list(atomic_ids)
                and binding.get("governance_started_receipt_fingerprint")
                == started_fingerprint
            ):
                matching.append(recovery)
        if len(matching) > 1:
            raise WechatDigestError(
                "Governance 启动恢复 receipt 重复匹配当前项目。"
            )
        if not matching:
            return None
        recovery = matching[0]
        recovery_fingerprint = recovery["receipt_fingerprint"]
        matching_retries = [
            retry
            for retry in retries
            if retry.get("recovery_receipt_fingerprint")
            == recovery_fingerprint
        ]
        if len(matching_retries) > 1:
            raise WechatDigestError(
                "Governance 启动恢复 retry receipt 重复匹配。"
            )
        if matching_retries:
            raise WechatDigestError(
                "Governance 启动恢复机会已消耗；禁止再次调用 Provider。"
            )
        return recovery

    @contextmanager
    def _governance_execution(self):
        with self._governance_execution_lock:
            with self._governance_observation_lock:
                self._governance_active += 1
                self._segment_performance["governance_peak_concurrency"] = max(
                    self._segment_performance.get(
                        "governance_peak_concurrency", 0
                    ),
                    self._governance_active,
                )
            try:
                yield
            finally:
                with self._governance_observation_lock:
                    self._governance_active -= 1

    def _govern_item(
        self,
        run_id: str,
        status: dict[str, object],
        item_id: str,
        atomic_ids: Sequence[str],
    ) -> tuple[bool, tuple[str, ...]]:
        if not atomic_ids:
            return False, ()
        started = time.monotonic()
        try:
            with self._governance_execution():
                return self._govern_item_active(
                    run_id, status, item_id, atomic_ids
                )
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            with self._governance_observation_lock:
                self._segment_performance["governance_wall_ms"] = (
                    self._segment_performance.get("governance_wall_ms", 0)
                    + elapsed_ms
                )

    def _govern_item_active(
        self,
        run_id: str,
        status: dict[str, object],
        item_id: str,
        atomic_ids: Sequence[str],
    ) -> tuple[bool, tuple[str, ...]]:
        atomic_fingerprint = _governance_atomic_fingerprint(atomic_ids)
        current = self.run_store.status(run_id)
        current_items = current.get("items")
        if not isinstance(current_items, dict):
            raise WechatDigestError("微信运行状态 items 损坏。")
        current_item = self._item(current_items, item_id)
        existing_receipt = current_item.get("governance_receipt")
        startup_recovery: dict[str, object] | None = None
        resume_state: dict[str, object] | None = None
        migration_state: dict[str, object] | None = None
        migration_binding: dict[str, object] | None = None
        migration_value = current_item.get("governance_migration")
        if migration_value is not None:
            migration_binding = _validated_governance_migration(
                migration_value
            )
            if (
                migration_binding["ordered_atomic_information_ids"]
                != list(atomic_ids)
                or self._governance_effect_fingerprint(
                    migration_binding["completed_atomic_information_ids"]
                )
                != migration_binding["legacy_effect_fingerprint"]
            ):
                raise WechatDigestError(
                    "微信 Governance migration binding 漂移。"
                )
        if existing_receipt is not None:
            receipt = _validated_governance_receipt(existing_receipt)
            if receipt["atomic_information_fingerprint"] != atomic_fingerprint:
                raise WechatDigestError("微信 Governance receipt binding 不一致。")
            if receipt["phase"] == "started":
                recovery_value = self._startup_recovery_for_item(
                    run_id=run_id,
                    item_id=item_id,
                    atomic_ids=atomic_ids,
                    started_receipt=receipt,
                )
                if recovery_value is not None:
                    recovery_binding = recovery_value.get("recovery_binding")
                    effect_bindings = recovery_value.get(
                        "atomic_effect_bindings"
                    )
                    if (
                        not isinstance(recovery_binding, dict)
                        or not isinstance(effect_bindings, list)
                        or len(effect_bindings) != len(atomic_ids)
                    ):
                        raise WechatDigestError(
                            "Governance 启动恢复 receipt binding 损坏。"
                        )
                    with SQLiteWorldModelRepository(self.database) as repository:
                        current_bindings = [
                            self._governance_effect_binding(
                                repository, atomic_id
                            )
                            for atomic_id in atomic_ids
                        ]
                        current_tree = self._governance_business_tree_fingerprint(
                            repository
                        )
                    if (
                        current_bindings != effect_bindings
                        or current_tree
                        != recovery_value.get("business_tree_fingerprint")
                    ):
                        raise WechatDigestError(
                            "Governance 启动恢复业务状态漂移。"
                        )
                    startup_recovery = recovery_value
                elif migration_binding is None:
                    raise WechatDigestError(
                        "微信 Governance completion 未知；禁止再次调用 Provider。"
                    )
                if migration_binding is not None:
                    migration_state = migration_binding
                    if (
                        migration_state["legacy_governance_receipt"] != receipt
                        or migration_state["ordered_atomic_information_ids"]
                        != list(atomic_ids)
                        or self._governance_effect_fingerprint(
                            migration_state["completed_atomic_information_ids"]
                        )
                        != migration_state["legacy_effect_fingerprint"]
                        or self._governance_effect_fingerprint(
                            migration_state["remaining_atomic_information_ids"]
                        )
                        != migration_state["pristine_remaining_fingerprint"]
                    ):
                        raise WechatDigestError(
                            "微信 Governance migration binding 漂移。"
                        )
            if (
                migration_binding is not None
                and receipt["schema_version"]
                == GOVERNANCE_RECEIPT_SCHEMA_VERSION
                and receipt["phase"] != "started"
                and receipt["batch_atomic_information_ids"]
                != migration_binding["remaining_atomic_information_ids"]
            ):
                raise WechatDigestError(
                    "微信 Governance migration batch binding 漂移。"
                )
            self._verify_governance_receipt_effects(receipt)
            if receipt["phase"] == "completed":
                return bool(receipt["pending_human"]), tuple(
                    str(object_id) for object_id in receipt["context_object_ids"]
                )
            if (
                receipt["schema_version"] != GOVERNANCE_RECEIPT_SCHEMA_VERSION
                and migration_state is None
            ):
                raise WechatDigestError("微信 Governance receipt 无法恢复。")
            if migration_state is None and startup_recovery is None:
                try:
                    resume_state = {
                        "batch_atomic_information_ids": tuple(
                            receipt["batch_atomic_information_ids"]
                        ),
                        "interpretations": tuple(
                            parse_interpretation(item)
                            for item in receipt["interpretations"]
                        ),
                        "next_index": receipt["next_index"],
                        "pending_human": receipt["pending_human"],
                        "context_object_ids": tuple(
                            receipt["context_object_ids"]
                        ),
                    }
                except (TypeError, ValueError) as exc:
                    raise WechatDigestError(
                        "微信 Governance receipt 无法恢复。"
                    ) from exc

        provider_started = False
        batch_persisted = resume_state is not None
        latest_batch_receipt = (
            None if existing_receipt is None else dict(existing_receipt)
        )

        def mark_provider_started() -> None:
            nonlocal provider_started
            if provider_started:
                return
            if startup_recovery is not None:
                recovery_fingerprint = startup_recovery.get(
                    "receipt_fingerprint"
                )
                without_fingerprint: dict[str, object] = {
                    "schema_version": GOVERNANCE_STARTUP_RETRY_SCHEMA_VERSION,
                    "artifact_kind": "governance_startup_retry_consumption",
                    "recovery_receipt_fingerprint": recovery_fingerprint,
                    "run_id": run_id,
                    "item_id": item_id,
                    "atomic_information_fingerprint": atomic_fingerprint,
                    "retry_attempt": 1,
                    "consumed_at": self.clock(),
                }
                consumption = {
                    **without_fingerprint,
                    "receipt_fingerprint": _sha256_bytes(
                        _canonical_json(without_fingerprint).encode("utf-8")
                    ),
                }
                authority_ref = str(startup_recovery.get("authority_ref", ""))
                if authority_ref.startswith(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
                ):
                    self.run_store.publish_governance_startup_receipt(
                        run_id,
                        filename="governance-startup-retry.json",
                        receipt=consumption,
                    )
                else:
                    self.run_store.publish_item_scoped_governance_startup_receipt(
                        run_id,
                        receipt=consumption,
                    )
                provider_started = True
                return
            self._update_item(
                run_id,
                status,
                item_id,
                governance_receipt={
                    "schema_version": GOVERNANCE_RECEIPT_SCHEMA_VERSION,
                    "phase": "started",
                    "atomic_information_fingerprint": atomic_fingerprint,
                },
            )
            provider_started = True

        def persist_batch_interpretation(
            batch_ids: tuple[str, ...],
            interpretations: tuple[InterpretationResult, ...],
            pending: bool,
            object_ids: tuple[str, ...],
            baseline_effect_snapshots: tuple[dict[str, object], ...],
        ) -> None:
            nonlocal batch_persisted, latest_batch_receipt
            baseline_effect_fingerprints = (
                self._governance_snapshot_fingerprints(
                    baseline_effect_snapshots
                )
            )
            serialized_effect_snapshots = json.loads(
                _canonical_json(baseline_effect_snapshots)
            )
            serialized = [
                interpretation_to_dict(interpretation)
                for interpretation in interpretations
            ]
            latest_batch_receipt = {
                "schema_version": GOVERNANCE_RECEIPT_SCHEMA_VERSION,
                "phase": "interpreted",
                "atomic_information_fingerprint": atomic_fingerprint,
                "batch_atomic_information_ids": list(batch_ids),
                "batch_fingerprint": _governance_batch_fingerprint(
                    batch_ids, interpretations
                ),
                "interpretations": serialized,
                "next_index": 0,
                "baseline_effect_fingerprints": list(
                    baseline_effect_fingerprints
                ),
                "cursor_effect_fingerprints": list(
                    baseline_effect_fingerprints
                ),
                "cursor_effect_snapshots": serialized_effect_snapshots,
                "applied_effect_fingerprints": [],
                "in_flight_index": None,
                "pending_human": pending,
                "context_object_ids": sorted(set(object_ids)),
            }
            self._update_item(
                run_id,
                status,
                item_id,
                governance_receipt=latest_batch_receipt,
            )
            batch_persisted = True

        def persist_application_intent(index: int) -> None:
            nonlocal latest_batch_receipt
            if latest_batch_receipt is None:
                raise WechatDigestError(
                    "微信 Governance batch result 尚未持久化。"
                )
            if (
                latest_batch_receipt["next_index"] != index
                or latest_batch_receipt["in_flight_index"] not in {None, index}
            ):
                raise WechatDigestError(
                    "微信 Governance apply intent cursor 不一致。"
                )
            latest_batch_receipt = {
                **latest_batch_receipt,
                "phase": "applying",
                "in_flight_index": index,
            }
            _validated_governance_receipt(latest_batch_receipt)
            self._update_item(
                run_id,
                status,
                item_id,
                governance_receipt=latest_batch_receipt,
            )

        def persist_application_progress(
            next_index: int,
            pending: bool,
            object_ids: tuple[str, ...],
            cursor_effect_snapshots: tuple[dict[str, object], ...],
        ) -> None:
            nonlocal latest_batch_receipt
            if latest_batch_receipt is None:
                raise WechatDigestError(
                    "微信 Governance batch result 尚未持久化。"
                )
            batch_ids = latest_batch_receipt["batch_atomic_information_ids"]
            cursor_effect_fingerprints = (
                self._governance_snapshot_fingerprints(
                    cursor_effect_snapshots
                )
            )
            serialized_effect_snapshots = json.loads(
                _canonical_json(cursor_effect_snapshots)
            )
            previous_effects = latest_batch_receipt[
                "applied_effect_fingerprints"
            ]
            if (
                len(previous_effects) != next_index - 1
                or len(cursor_effect_fingerprints) != len(batch_ids)
                or latest_batch_receipt["in_flight_index"] != next_index - 1
            ):
                raise WechatDigestError(
                    "微信 Governance apply effect cursor 不一致。"
                )
            phase = "applied" if next_index == len(batch_ids) else "applying"
            latest_batch_receipt = {
                **latest_batch_receipt,
                "phase": phase,
                "next_index": next_index,
                "applied_effect_fingerprints": list(
                    cursor_effect_fingerprints[:next_index]
                ),
                "cursor_effect_fingerprints": list(
                    cursor_effect_fingerprints
                ),
                "cursor_effect_snapshots": serialized_effect_snapshots,
                "in_flight_index": None,
                "pending_human": pending,
                "context_object_ids": sorted(set(object_ids)),
            }
            _validated_governance_receipt(latest_batch_receipt)
            self._update_item(
                run_id,
                status,
                item_id,
                governance_receipt=latest_batch_receipt,
            )

        cursor_reader = getattr(self.interpretation_provider, "metrics_cursor", None)
        metrics_reader = getattr(self.interpretation_provider, "metrics_since", None)
        invalidator = getattr(self.interpretation_provider, "invalidate", None)
        cursor = cursor_reader() if callable(cursor_reader) else None
        started = time.monotonic_ns()
        failure: BaseException | None = None
        outcome: tuple[bool, tuple[str, ...]] | None = None
        previous_provider_hook = self._before_governance_provider_call
        previous_resume_state = self._governance_resume_state
        previous_migration_state = self._governance_migration_state
        previous_batch_hook = self._after_governance_batch_interpretation
        previous_intent_hook = self._before_governance_application
        previous_progress_hook = self._after_governance_application
        self._before_governance_provider_call = mark_provider_started
        self._governance_resume_state = resume_state
        self._governance_migration_state = migration_state
        self._after_governance_batch_interpretation = persist_batch_interpretation
        self._before_governance_application = persist_application_intent
        self._after_governance_application = persist_application_progress
        try:
            outcome = self._govern(atomic_ids)
            return outcome
        except BaseException as exc:
            failure = exc
            if callable(invalidator):
                observed = (
                    metrics_reader(cursor)
                    if cursor is not None and callable(metrics_reader)
                    else _empty_governance_metrics()
                )
                if int(observed["failure_count"]) == 0:
                    invalidator("apply_or_readback")
            raise
        finally:
            self._before_governance_provider_call = previous_provider_hook
            self._governance_resume_state = previous_resume_state
            self._governance_migration_state = previous_migration_state
            self._after_governance_batch_interpretation = previous_batch_hook
            self._before_governance_application = previous_intent_hook
            self._after_governance_application = previous_progress_hook
            elapsed_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
            metrics = (
                metrics_reader(cursor)
                if cursor is not None and callable(metrics_reader)
                else _empty_governance_metrics()
            )
            metrics = dict(metrics)
            metrics["governance_wall_ms"] = elapsed_ms
            if failure is not None and int(metrics["failure_count"]) == 0:
                categories = dict(metrics["failure_categories"])
                categories["governance"] = categories.get("governance", 0) + 1
                metrics["failure_categories"] = categories
                metrics["failure_count"] = int(metrics["failure_count"]) + 1
            latest = self.run_store.status(run_id)
            latest_items = latest.get("items")
            if not isinstance(latest_items, dict):
                raise WechatDigestError("微信运行状态 items 损坏。")
            latest_item = self._item(latest_items, item_id)
            changes: dict[str, object] = {
                "governance_metrics": _merge_governance_metrics(
                    latest_item.get("governance_metrics"), metrics
                )
            }
            if outcome is not None:
                pending, object_ids = outcome
                if batch_persisted:
                    if latest_batch_receipt is None:
                        raise WechatDigestError(
                            "微信 Governance batch receipt 丢失。"
                        )
                    changes["governance_receipt"] = {
                        **latest_batch_receipt,
                        "phase": "completed",
                        "pending_human": pending,
                        "context_object_ids": sorted(set(object_ids)),
                    }
                    _validated_governance_receipt(
                        changes["governance_receipt"]
                    )
                else:
                    changes["governance_receipt"] = {
                        "schema_version": LEGACY_GOVERNANCE_RECEIPT_SCHEMA_VERSION,
                        "phase": "completed",
                        "atomic_information_fingerprint": atomic_fingerprint,
                        "pending_human": pending,
                        "context_object_ids": sorted(set(object_ids)),
                    }
            self._update_item(
                run_id,
                status,
                item_id,
                **changes,
            )

    @staticmethod
    def _aggregate_results(
        results: Sequence[WechatDigestResult],
    ) -> WechatDigestResult:
        if not results:
            raise WechatDigestError("微信运行没有可汇总的结果。")
        if len(results) == 1:
            return results[0]
        context_object_ids = tuple(
            sorted(
                {
                    object_id
                    for result in results
                    for object_id in result.context_object_ids
                }
            )
        )
        return WechatDigestResult(
            run_id=results[-1].run_id,
            new_messages=sum(result.new_messages for result in results),
            new_attachments=sum(result.new_attachments for result in results),
            durable_information=sum(result.durable_information for result in results),
            local_only=sum(result.local_only for result in results),
            unsupported=sum(result.unsupported for result in results),
            pending_human=sum(result.pending_human for result in results),
            failed_closed=sum(result.failed_closed for result in results),
            context_objects=len(context_object_ids),
            checkpoint_published=all(result.checkpoint_published for result in results),
            replayed=any(result.replayed for result in results),
            context_object_ids=context_object_ids,
            governance_app_server_starts=sum(
                result.governance_app_server_starts for result in results
            ),
            governance_threads=sum(result.governance_threads for result in results),
            governance_turns=sum(result.governance_turns for result in results),
            governance_startup_wall_ms=sum(
                result.governance_startup_wall_ms for result in results
            ),
            governance_turn_wall_ms_sum=sum(
                result.governance_turn_wall_ms_sum for result in results
            ),
            governance_turn_wall_ms_max=max(
                (result.governance_turn_wall_ms_max for result in results),
                default=0,
            ),
            governance_wall_ms=sum(result.governance_wall_ms for result in results),
            governance_timeouts=sum(result.governance_timeouts for result in results),
            governance_failures=sum(result.governance_failures for result in results),
            semantic_preserved_but_unabsorbed=sum(
                result.semantic_preserved_but_unabsorbed for result in results
            ),
            governance_preserved_but_incomplete=sum(
                result.governance_preserved_but_incomplete for result in results
            ),
            segment_safe_stopped=any(
                result.segment_safe_stopped for result in results
            ),
            segment_items_completed=sum(
                result.segment_items_completed for result in results
            ),
            segment_remaining_items=results[-1].segment_remaining_items,
            segment_stop_reason=results[-1].segment_stop_reason,
            segment_receipt_fingerprint=results[-1].segment_receipt_fingerprint,
            upper_bound_probe_calls=sum(
                result.upper_bound_probe_calls for result in results
            ),
            capture_attempts=sum(result.capture_attempts for result in results),
            capture_successes=sum(result.capture_successes for result in results),
            capture_reasons=tuple(
                reason
                for result in results
                for reason in result.capture_reasons
            ),
            capture_provider_calls=sum(
                result.capture_provider_calls for result in results
            ),
            completed_window_connector_replays=sum(
                result.completed_window_connector_replays for result in results
            ),
            materialized_cursor_rows=sum(
                result.materialized_cursor_rows for result in results
            ),
            cursor_discovery_ms=sum(
                result.cursor_discovery_ms for result in results
            ),
            snapshot_bytes=max(result.snapshot_bytes for result in results),
            capture_ms=sum(result.capture_ms for result in results),
            snapshot_publish_ms=sum(
                result.snapshot_publish_ms for result in results
            ),
            snapshot_readback_ms=sum(
                result.snapshot_readback_ms for result in results
            ),
            slice_build_ms=sum(result.slice_build_ms for result in results),
            semantic_parallelism=max(
                result.semantic_parallelism for result in results
            ),
            semantic_peak_concurrency=max(
                result.semantic_peak_concurrency for result in results
            ),
            semantic_wall_ms=sum(result.semantic_wall_ms for result in results),
            semantic_serial_estimate_ms=sum(
                result.semantic_serial_estimate_ms for result in results
            ),
            commit_wall_ms=sum(result.commit_wall_ms for result in results),
            checkpoint_wall_ms=sum(
                result.checkpoint_wall_ms for result in results
            ),
            governance_peak_concurrency=max(
                result.governance_peak_concurrency for result in results
            ),
            resume_provider_calls=sum(
                result.resume_provider_calls for result in results
            ),
            total_wall_ms=sum(result.total_wall_ms for result in results),
        )

    def _result(
        self,
        run_id: str,
        status: Mapping[str, object],
        *,
        replayed: bool,
        segment_safe_stopped: bool = False,
        segment_items_completed: int = 0,
        segment_remaining_items: int = 0,
        segment_stop_reason: str | None = None,
    ) -> WechatDigestResult:
        items = status.get("items")
        if not isinstance(items, dict):
            raise WechatDigestError("微信完成摘要不可读。")
        values = tuple(item for item in items.values() if isinstance(item, dict))
        atomic_ids = {
            atomic_id
            for item in values
            for atomic_id in item.get("atomic_information_ids", [])
            if isinstance(atomic_id, str)
        }
        object_ids = {
            object_id
            for item in values
            for object_id in item.get("context_object_ids", [])
            if isinstance(object_id, str)
        }
        metrics = [
            _validated_governance_metrics(item["governance_metrics"])
            for item in values
            if item.get("governance_metrics") is not None
        ]
        readback_started = time.monotonic()
        capture, _capture_receipt = self.run_store.load_capture_artifacts(run_id)
        snapshot_path = (
            self.run_store.runs_root / run_id / "capture" / "snapshot.json"
        )
        snapshot_bytes = snapshot_path.stat().st_size
        self._segment_performance["snapshot_readback_ms"] = (
            self._segment_performance.get("snapshot_readback_ms", 0)
            + round((time.monotonic() - readback_started) * 1000)
        )
        if _capture_fingerprint(capture) != _capture_receipt.get(
            "capture_fingerprint"
        ):
            raise WechatDigestError("微信完成摘要 capture binding 损坏。")
        return WechatDigestResult(
            run_id=run_id,
            new_messages=sum(
                int(item.get("message_count", 0))
                for item in values
                if item.get("kind") == "conversation"
            ),
            new_attachments=sum(item.get("kind") == "attachment" for item in values),
            durable_information=len(atomic_ids),
            local_only=sum(item.get("state") == "local_only" for item in values),
            unsupported=sum(item.get("state") == "unsupported" for item in values),
            pending_human=sum(item.get("state") == "pending_human" for item in values),
            failed_closed=sum(item.get("state") == "failed_closed" for item in values),
            context_objects=len(object_ids),
            checkpoint_published=bool(status.get("checkpoint_published")),
            replayed=replayed,
            context_object_ids=tuple(sorted(object_ids)),
            governance_app_server_starts=sum(
                int(item["app_server_start_count"]) for item in metrics
            ),
            governance_threads=sum(int(item["thread_count"]) for item in metrics),
            governance_turns=sum(int(item["turn_count"]) for item in metrics),
            governance_startup_wall_ms=sum(
                int(item["startup_wall_ms"]) for item in metrics
            ),
            governance_turn_wall_ms_sum=sum(
                int(item["turn_wall_ms_sum"]) for item in metrics
            ),
            governance_turn_wall_ms_max=max(
                (int(item["turn_wall_ms_max"]) for item in metrics),
                default=0,
            ),
            governance_wall_ms=self._segment_performance.get(
                "governance_wall_ms", 0
            ),
            governance_timeouts=sum(int(item["timeout_count"]) for item in metrics),
            governance_failures=sum(int(item["failure_count"]) for item in metrics),
            semantic_preserved_but_unabsorbed=sum(
                item.get("state") == "failed_closed"
                and item.get("semantic_failure") is not None
                for item in values
            ),
            governance_preserved_but_incomplete=sum(
                item.get("state") == "failed_closed"
                and item.get("governance_failure") is not None
                for item in values
            ),
            segment_safe_stopped=segment_safe_stopped,
            segment_items_completed=segment_items_completed,
            segment_remaining_items=segment_remaining_items,
            segment_stop_reason=segment_stop_reason,
            upper_bound_probe_calls=self._segment_performance.get(
                "upper_bound_probe_calls", 0
            ),
            capture_attempts=self._segment_performance.get(
                "capture_attempts", 0
            ),
            capture_successes=self._segment_performance.get(
                "capture_successes", 0
            ),
            capture_reasons=tuple(self._capture_reasons),
            capture_provider_calls=self._segment_performance.get(
                "capture_provider_calls", 0
            ),
            completed_window_connector_replays=self._segment_performance.get(
                "completed_window_connector_replays", 0
            ),
            materialized_cursor_rows=self._segment_performance.get(
                "materialized_cursor_rows", 0
            ),
            cursor_discovery_ms=self._segment_performance.get(
                "cursor_discovery_ms", 0
            ),
            snapshot_bytes=snapshot_bytes,
            capture_ms=self._segment_performance.get("capture_ms", 0),
            snapshot_publish_ms=self._segment_performance.get(
                "snapshot_publish_ms", 0
            ),
            snapshot_readback_ms=self._segment_performance.get(
                "snapshot_readback_ms", 0
            ),
            slice_build_ms=self._segment_performance.get("slice_build_ms", 0),
            semantic_parallelism=self.semantic_parallelism,
            semantic_peak_concurrency=self._segment_performance.get(
                "semantic_peak_concurrency", 0
            ),
            semantic_wall_ms=self._segment_performance.get(
                "semantic_wall_ms", 0
            ),
            semantic_serial_estimate_ms=self._segment_performance.get(
                "semantic_serial_estimate_ms", 0
            ),
            commit_wall_ms=self._segment_performance.get("commit_wall_ms", 0),
            checkpoint_wall_ms=self._segment_performance.get(
                "checkpoint_wall_ms", 0
            ),
            governance_peak_concurrency=self._segment_performance.get(
                "governance_peak_concurrency", 0
            ),
            resume_provider_calls=self._segment_performance.get(
                "resume_provider_calls", 0
            ),
            total_wall_ms=0,
        )
