"""Incremental, replay-safe WeChat digestion orchestration.

The module is a Processing workflow.  Its run ledger and checkpoint are
technical ``Processing Run`` state; they do not introduce Conversation,
Message, Attachment, or checkpoint as Core business concepts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .atomic_information import JsonlAtomicInformationStore
from .consolidation import BoundedInformationCandidateRetriever
from .context import ContextBuilder, ContextRequest
from .digestion import (
    AtomicInformationDigestionService,
    AtomicInformationInterpretationProvider,
    BusinessLanguageHumanJudgmentPort,
    JsonlChangeJournal,
    JsonlChangeProposalStore,
)
from .emergence import IdentityEvidence, IdentityGateService
from .representation import (
    LocalRepresentationRepository,
    RepresentationService,
    WechatConversationV2RepresentationAdapter,
)
from .representation.registry import production_adapter
from .representation_information import (
    DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
    CodexCliRepresentationAnalysisProvider,
    RepresentationInformationService,
    _units_from_representation,
)
from .semantic_handoff import ExternalAgentSemanticHandoffService
from .source import LocalManagedSourceRepository, ManagedSourceService
from .source.local_repository import SourceNotFoundError
from .world_model import ObjectResolver, SQLiteWorldModelRepository

CHECKPOINT_SCHEMA_VERSION = "wechat-digest-checkpoint/1.0"
RUN_PLAN_SCHEMA_VERSION = "wechat-digest-run-plan/2.0"
LEGACY_RUN_PLAN_SCHEMA_VERSION = "wechat-digest-run-plan/1.0"
RUN_STATUS_SCHEMA_VERSION = "wechat-digest-run-status/1.0"
CAPTURE_SCHEMA_VERSION = "wechat-cli-capture/1.0"
SUPPORTED_WECHAT_CLI_VERSION = "0.5.0"
DEFAULT_CAPTURE_WINDOW_DAYS = 30
DEFAULT_CAPTURE_WINDOW_MESSAGES = 1000
TERMINAL_ITEM_STATES = frozenset(
    {"processed", "local_only", "unsupported", "pending_human"}
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
        observe_only: bool = False,
    ) -> WechatCapture: ...


class SemanticHandoffPort(Protocol):
    def execute(self, representation_id: str): ...


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


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
        observe_only: bool = False,
    ) -> WechatCapture:
        request = {
            "config_path": None if self.config_path is None else str(self.config_path),
            "after_cursor": after_cursor.to_dict(),
            "upper_bound": None if upper_bound is None else upper_bound.to_dict(),
            "observe_only": observe_only,
            "window_days": self.window_days,
            "window_message_limit": self.window_message_limit,
        }
        helper = Path(__file__).with_name("wechat_capture_helper.py")
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
        return capture

    def _parse_capture(
        self, payload: object, after_cursor: WechatCursor
    ) -> WechatCapture:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "observed_upper",
            "messages",
        }:
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
    ) -> None:
        self.root = Path(root)
        self.runs_root = self.root / "runs"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.active_path = self.root / "active.json"
        self.lock_path = self.root / ".digest.lock"
        self.clock = clock
        self.before_checkpoint_publish = before_checkpoint_publish

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
        if not isinstance(value, dict) or value.get("schema_version") not in {RUN_PLAN_SCHEMA_VERSION, LEGACY_RUN_PLAN_SCHEMA_VERSION}:
            raise WechatDigestError("微信运行计划损坏。")
        return value

    def plan_receipt(self, run_id: str) -> dict[str, object]:
        value = self._read_json(self.runs_root / run_id / "run-plan-receipt.json")
        if not isinstance(value, dict) or set(value) != {"schema_version", "run_id", "plan_fingerprint"}:
            raise WechatDigestError("微信运行计划 receipt 损坏。")
        return value

    def status(self, run_id: str) -> dict[str, object]:
        value = self._read_json(self.runs_root / run_id / "status.json")
        if not isinstance(value, dict) or value.get("schema_version") != RUN_STATUS_SCHEMA_VERSION:
            raise WechatDigestError("微信运行状态损坏。")
        return value

    def create(self, plan: dict[str, object], status: dict[str, object]) -> None:
        run_id = str(plan["run_id"])
        run_dir = self.runs_root / run_id
        if run_dir.exists():
            if self.plan(run_id) != plan:
                raise WechatDigestError("微信运行 identity collision。")
        else:
            run_dir.mkdir(parents=True)
            _atomic_write_json(run_dir / "plan.json", plan)
            _atomic_write_json(run_dir / "status.json", status)
            _atomic_write_json(run_dir / "run-plan-receipt.json", {
                "schema_version": "wechat-digest-run-plan-receipt/1.0",
                "run_id": run_id,
                "plan_fingerprint": _plan_fingerprint(plan),
            })
        _atomic_write_json(self.active_path, {"active_run_id": run_id})

    def update_status(self, run_id: str, status: dict[str, object]) -> None:
        _atomic_write_json(self.runs_root / run_id / "status.json", status)

    def upgrade_plan(self, run_id: str, plan: dict[str, object], status: dict[str, object]) -> None:
        if self.plan(run_id).get("schema_version") != LEGACY_RUN_PLAN_SCHEMA_VERSION:
            raise WechatDigestError("仅允许升级 active v1 微信运行。")
        fingerprint = _plan_fingerprint(plan)
        _atomic_write_json(self.runs_root / run_id / "plan.json", plan)
        _atomic_write_json(self.runs_root / run_id / "status.json", status)
        _atomic_write_json(self.runs_root / run_id / "run-plan-receipt.json", {"schema_version": "wechat-digest-run-plan-receipt/1.0", "run_id": run_id, "plan_fingerprint": fingerprint})

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
        result = subprocess.run(
            [codex_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WechatDigestError("无法验证 Semantic Provider 版本。") from exc
    if result.returncode != 0:
        raise WechatDigestError("无法验证 Semantic Provider 版本。")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", result.stdout.strip()).strip("-")
    if not normalized or len(normalized) > 128:
        raise WechatDigestError("无法验证 Semantic Provider 版本。")
    return normalized


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
        batch_size: int = DEFAULT_EXTERNAL_AGENT_BATCH_SIZE,
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
        self.provider = CodexCliRepresentationAnalysisProvider(
            codex_binary=codex_binary,
            provider_version=provider_version,
            timeout_seconds=timeout_seconds,
        )

    def execute(self, representation_id: str):
        return self.service.execute(representation_id, self.provider)


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


def _conversation_source_payload(
    capture: WechatCapture,
    conversation_key: str,
    attachment_sources: Mapping[str, str | None],
) -> bytes:
    selected = tuple(
        message
        for message in capture.messages
        if message.conversation_key == conversation_key
    )
    if not selected:
        raise WechatDigestError("微信运行计划缺少 Conversation 消息。")
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
    run_id: str | None = None,
    created_at: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
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
    for conversation_key in sorted(
        {message.conversation_key for message in capture.messages}
    ):
        source_id = _stable_id("src", run_id, conversation_key)
        payload = _conversation_source_payload(
            capture, conversation_key, attachment_sources
        )
        conversation_plans.append(
            {
                "conversation_key": conversation_key,
                "source_id": source_id,
                "content_hash": _sha256_bytes(payload),
                "size_bytes": len(payload),
                "filename_hint": f"wechat-{conversation_key[-12:]}.json",
                "message_keys": [
                    message.message_key
                    for message in capture.messages
                    if message.conversation_key == conversation_key
                ],
            }
        )
    plan: dict[str, object] = {
        "schema_version": RUN_PLAN_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "provider_version": capture.provider_version,
        "after_cursor": capture.after_cursor.to_dict(),
        "upper_bound": capture.upper_bound.to_dict(),
        "capture_fingerprint": fingerprint,
        "semantic_batch_size": semantic_batch_size,
        "message_keys": [message.message_key for message in capture.messages],
        "conversations": conversation_plans,
        "attachments": attachment_plans,
    }
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
    keys = ("schema_version", "run_id", "after_cursor", "upper_bound", "capture_fingerprint", "semantic_batch_size", "conversations")
    return _sha256_bytes(_canonical_json({key: plan.get(key) for key in keys}).encode("utf-8"))


def _plan_sequence(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise WechatDigestError(f"微信运行计划 {field} 损坏。")
    return value  # type: ignore[return-value]


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
        if isinstance(semantic_batch_size, bool) or not isinstance(semantic_batch_size, int) or semantic_batch_size < 1:
            raise ValueError("semantic batch size must be positive")
        self.semantic_batch_size = semantic_batch_size

    def run(
        self,
        *,
        since: str | None = None,
        from_now: bool = False,
        all_history: bool = False,
    ) -> WechatDigestResult:
        bootstrap_count = sum((since is not None, from_now, all_history))
        if bootstrap_count > 1:
            raise WechatDigestError(
                "首次起点只能选择 --since、--from-now 或 --all-history 之一。"
            )
        with self.run_store.lock():
            try:
                results: list[WechatDigestResult] = []
                while True:
                    result = self._run_locked(
                        since=since,
                        from_now=from_now,
                        all_history=all_history,
                    )
                    results.append(result)
                    if from_now or result.new_messages == 0:
                        return self._aggregate_results(results)
                    since = None
                    from_now = False
                    all_history = False
            except WechatDigestError:
                raise
            except Exception as exc:
                raise WechatDigestError(
                    "微信信息消化未安全完成；checkpoint 未推进。"
                ) from exc

    def prepare_next_semantic(
        self, *, batch_size: int = DEFAULT_EXTERNAL_AGENT_BATCH_SIZE
    ) -> WechatSemanticPreparation:
        """Recover an active run only up to its next single semantic batch."""
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise WechatDigestError("semantic batch size 必须是正整数。")
        with self.run_store.lock():
            run_id = self.run_store.active_run_id()
            if run_id is None:
                raise WechatDigestError("不存在可恢复的微信运行；未创建新 capture。")
            plan = self.run_store.plan(run_id)
            effective_batch_size = self._plan_batch_size(plan)
            if batch_size != effective_batch_size:
                raise WechatDigestError("semantic batch size 与 durable run 不一致。")
            capture = self.capture_provider.capture(
                WechatCursor.from_dict(plan["after_cursor"], "plan.after_cursor"),
                upper_bound=WechatCursor.from_dict(
                    plan["upper_bound"], "plan.upper_bound"
                ),
            )
            self._verify_capture_against_plan(capture, plan)
            self._verify_plan_and_status(capture, plan, self.run_store.status(run_id))
            return self._prepare_next_semantic_locked(
                run_id,
                capture,
                plan,
                self.run_store.status(run_id),
                batch_size=effective_batch_size,
            )

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
        captured_attachments = {
            attachment.attachment_key: attachment
            for message in capture.messages
            for attachment in message.attachments
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
                capture, str(item_plan["conversation_key"]), attachment_source_ids
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
        eligible = tuple(
            unit.unit_id
            for unit in _units_from_representation(
                representation, self.representation_repository
            )
            if unit.analysis_eligible
        )
        if not eligible:
            raise WechatDigestError("semantic item 缺少 eligible units。")
        if len(eligible) > batch_size:
            raise WechatDigestError("下一个 semantic item 需要多个 batch；未跳过该 item。")
        return WechatSemanticPreparation(run_id, representation_id, eligible)

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

    def _verify_plan_and_status(
        self, capture: WechatCapture, plan: dict[str, object], status: dict[str, object]
    ) -> None:
        run_id = plan.get("run_id")
        created_at = plan.get("created_at")
        if not isinstance(run_id, str) or not isinstance(created_at, str):
            raise WechatDigestError("微信运行计划损坏。")
        if plan.get("schema_version") != RUN_PLAN_SCHEMA_VERSION:
            raise WechatDigestError("active legacy 微信运行必须显式升级。")
        receipt = self.run_store.plan_receipt(run_id)
        fingerprint = _plan_fingerprint(plan)
        if receipt.get("run_id") != run_id or receipt.get("plan_fingerprint") != fingerprint or status.get("plan_fingerprint") != fingerprint:
            raise WechatDigestError("微信运行计划 receipt 不一致。")
        expected, _ = _build_plan(capture, clock=lambda: created_at, run_id=run_id,
            created_at=created_at, semantic_batch_size=self._plan_batch_size(plan))
        comparable = dict(plan)
        if comparable.get("schema_version") == LEGACY_RUN_PLAN_SCHEMA_VERSION:
            expected.pop("semantic_batch_size")
            expected["schema_version"] = LEGACY_RUN_PLAN_SCHEMA_VERSION
        if comparable != expected:
            raise WechatDigestError("微信 durable plan 与 fixed capture 不一致。")
        items = status.get("items")
        if status.get("run_id") != run_id or not isinstance(items, dict):
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
            if item.get("state") in {"represented", "local_only", "processed", "pending_human"}:
                representation_id = item.get("representation_id")
                if not isinstance(representation_id, str):
                    raise WechatDigestError("微信运行状态 receipt 损坏。")
                if self.representation_repository.get(representation_id).source_id != source_id:
                    raise WechatDigestError("微信运行状态 Representation binding 损坏。")

    def _run_locked(
        self, *, since: str | None, from_now: bool, all_history: bool
    ) -> WechatDigestResult:
        checkpoint = self.run_store.checkpoint()
        active_run_id = self.run_store.active_run_id()
        replayed = active_run_id is not None
        if active_run_id is not None:
            if any((since is not None, from_now, all_history)):
                raise WechatDigestError("存在未完成运行；请先原样恢复，不要更改首次起点。")
            plan = self.run_store.plan(active_run_id)
            after = WechatCursor.from_dict(plan["after_cursor"], "plan.after_cursor")
            upper = WechatCursor.from_dict(plan["upper_bound"], "plan.upper_bound")
            capture = self.capture_provider.capture(after, upper_bound=upper)
            self._verify_capture_against_plan(capture, plan)
            status = self.run_store.status(active_run_id)
            if self.semantic_batch_size != self._plan_batch_size(plan):
                raise WechatDigestError("semantic batch size 与 durable run 不一致。")
            self._verify_plan_and_status(capture, plan, status)
        else:
            if checkpoint is None:
                if not any((since is not None, from_now, all_history)):
                    raise WechatDigestError(
                        "首次使用必须明确选择 --since、--from-now 或 --all-history。"
                    )
                if from_now:
                    observed = self.capture_provider.capture(
                        ZERO_CURSOR, observe_only=True
                    )
                    capture = WechatCapture(
                        observed.provider_version,
                        observed.upper_bound,
                        observed.upper_bound,
                        (),
                    )
                else:
                    after = ZERO_CURSOR if all_history else parse_since(str(since))
                    capture = self.capture_provider.capture(after)
            else:
                if any((since is not None, from_now, all_history)):
                    raise WechatDigestError(
                        "checkpoint 已存在；日常运行不要再指定首次起点。"
                    )
                capture = self.capture_provider.capture(checkpoint)
            plan, status = _build_plan(capture, clock=self.clock,
                semantic_batch_size=self.semantic_batch_size)
            self.run_store.create(plan, status)
            active_run_id = str(plan["run_id"])

        assert active_run_id is not None
        try:
            result = self._process(
                capture, plan, status, replayed=replayed
            )
        except Exception as exc:
            failed = dict(status)
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
        by_attachment = {
            attachment.attachment_key: attachment
            for message in capture.messages
            for attachment in message.attachments
        }

        for attachment_plan in attachment_plans:
            item_id = f"attachment:{attachment_plan['attachment_key']}"
            item = self._item(items, item_id)
            if self._terminal_item_valid(item):
                continue
            if attachment_plan["status"] != "available":
                self._update_item(run_id, status, item_id, state="unsupported")
                continue
            captured = by_attachment.get(str(attachment_plan["attachment_key"]))
            if captured is None or captured.path is None:
                raise WechatDigestError("微信附件重放无法精确定位。")
            self._process_attachment(
                run_id, status, item_id, attachment_plan, captured
            )

        for conversation_plan in _plan_sequence(
            plan.get("conversations"), "conversations"
        ):
            item_id = f"conversation:{conversation_plan['conversation_key']}"
            item = self._item(items, item_id)
            if self._terminal_item_valid(item):
                continue
            payload = _conversation_source_payload(
                capture,
                str(conversation_plan["conversation_key"]),
                attachment_source_ids,
            )
            if (
                _sha256_bytes(payload) != conversation_plan["content_hash"]
                or len(payload) != conversation_plan["size_bytes"]
            ):
                raise WechatDigestError("微信 Conversation Source 重放不一致。")
            self._process_conversation(
                run_id, status, item_id, conversation_plan, payload
            )

        current_status = self.run_store.status(run_id)
        current_items = current_status.get("items")
        if not isinstance(current_items, dict) or any(
            not isinstance(item, dict)
            or item.get("state") not in TERMINAL_ITEM_STATES
            for item in current_items.values()
        ):
            raise WechatDigestError("微信运行尚未达到 terminal convergence。")
        converged = dict(current_status)
        converged["state"] = "converged"
        converged["failure_category"] = None
        converged["updated_at"] = self.clock()
        self.run_store.update_status(run_id, converged)
        upper = WechatCursor.from_dict(plan["upper_bound"], "plan.upper_bound")
        self.run_store.publish_checkpoint(run_id, upper)
        converged["checkpoint_published"] = True
        converged["state"] = "completed"
        converged["updated_at"] = self.clock()
        self.run_store.update_status(run_id, converged)
        self.run_store.clear_active()
        return self._result(run_id, converged, replayed=replayed)

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
        for atomic_id in atomic_ids:
            if not isinstance(atomic_id, str):
                raise WechatDigestError("微信 terminal item Information identity 损坏。")
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
        atomic_ids = self._semantic(representation.representation_id)
        pending, object_ids = self._govern(atomic_ids)
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
        atomic_ids = self._semantic(representation.representation_id)
        pending, object_ids = self._govern(atomic_ids)
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

    def _semantic(self, representation_id: str) -> tuple[str, ...]:
        if self._semantic_handoff is None:
            self._semantic_handoff = self.semantic_handoff_factory()
        result = self._semantic_handoff.execute(representation_id)
        atomic_ids = tuple(result.ingestion.atomic_information_ids)
        for atomic_id in atomic_ids:
            self.information_store.get_current(atomic_id)
        return atomic_ids

    def _has_semantic_units(self, representation_id: str) -> bool:
        representation = self.representation_repository.get(representation_id)
        units = _units_from_representation(
            representation, self.representation_repository
        )
        return any(unit.analysis_eligible for unit in units)

    def _govern(self, atomic_ids: Sequence[str]) -> tuple[bool, tuple[str, ...]]:
        if not atomic_ids:
            return False, ()
        pending = False
        affected: set[str] = set()
        with SQLiteWorldModelRepository(self.database) as repository:
            identity = IdentityGateService(
                self.information_store,
                repository,
                self.proposal_store,
                self.journal,
                BusinessLanguageHumanJudgmentPort(),
            )
            digestion = AtomicInformationDigestionService(
                self.information_store,
                repository,
                ObjectResolver(repository),
                self.interpretation_provider,
                self.proposal_store,
                self.journal,
                BusinessLanguageHumanJudgmentPort(),
            )
            retriever = BoundedInformationCandidateRetriever()
            for atomic_id in atomic_ids:
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
                bounded_pool = pool[-512:]
                retriever.retrieve(
                    current,
                    bounded_pool,
                    pool_complete=len(pool) <= 512,
                )
                if identity_pending:
                    affected.update(current.related_object_ids)
                    continue
                digest_result = digestion.digest(atomic_id)
                if digest_result.proposal_id is not None:
                    pending = True
                affected.update(digest_result.atomic_information.related_object_ids)

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
            durable_information=sum(
                result.durable_information for result in results
            ),
            local_only=sum(result.local_only for result in results),
            unsupported=sum(result.unsupported for result in results),
            pending_human=sum(result.pending_human for result in results),
            context_objects=len(context_object_ids),
            checkpoint_published=all(
                result.checkpoint_published for result in results
            ),
            replayed=any(result.replayed for result in results),
            context_object_ids=context_object_ids,
        )

    @staticmethod
    def _result(
        run_id: str, status: Mapping[str, object], *, replayed: bool
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
            context_objects=len(object_ids),
            checkpoint_published=bool(status.get("checkpoint_published")),
            replayed=replayed,
            context_object_ids=tuple(sorted(object_ids)),
        )
