"""Content-free technical Gate receipts for synthetic Semantic Handoff runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .filesystem import publish_file_no_replace
from .representation_information import (
    CONTRACT_FAILURE_DETAILS,
    CodexCliRepresentationAnalysisProvider,
    ExternalAgentExecutionRecord,
    ExternalAgentTechnicalObservation,
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationInformationError,
)

SYNTHETIC_GATE_RECEIPT_SCHEMA_VERSION = "synthetic-semantic-gate-receipt/1.0"
_RECEIPT_NAME = "technical-receipt.json"
_EMPTY_SHA256 = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_FAILURE_CATEGORIES = frozenset(
    {
        "runtime_start_failure",
        "timeout",
        "runtime_nonzero_exit",
        "runtime_execution_failure",
        "process_cleanup_failure",
        "no_result",
        "invalid_json",
        "result_binding_failure",
        "result_contract_failure",
    }
)
_CONTRACT_FAILURE_STAGES = frozenset(
    {
        "top_level",
        "candidate",
        "residue",
        "evidence_reference",
        "coverage",
        "accounting_cross_check",
        "record_grouping",
        "validation",
    }
)
_PROVIDER_ERROR_CATEGORIES = frozenset(
    {
        "auth_or_permission",
        "rate_limited",
        "network_or_transport",
        "service_unavailable",
        "structured_output_rejected",
        "provider_internal_error",
        "cancelled",
        "unknown",
    }
)
_HARNESS_FAILURE_CATEGORIES = frozenset(
    {
        "pre_provider_failure",
        "provider_outcome_unknown",
        "post_success_assertion_failure",
        "post_success_serialization_failure",
        "diagnostics_persistence_failure",
        "execution_observation_invalid",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "receipt_fingerprint",
        "provider_call_started",
        "provider_call_start_proven",
        "provider_call_counted",
        "provider_outcome_unknown",
        "provider_execution_status",
        "provider_failure_category",
        "provider_error_category",
        "contract_failure_detail",
        "contract_failure_stage",
        "strict_validation_status",
        "protocol_version",
        "provider_route",
        "provider_version",
        "model",
        "reasoning_effort",
        "fallback_policy",
        "eligible_units",
        "covered_units",
        "missing_anchor_count",
        "accounting_item_count",
        "candidate_item_count",
        "residue_item_count",
        "candidate_anchor_ref_count",
        "residue_anchor_ref_count",
        "duplicate_anchor_ref_count",
        "duplicate_accounting_count",
        "dual_assignment_count",
        "unknown_anchor_ref_count",
        "raw_record_count",
        "projected_record_count",
        "duplicate_exact_body_count",
        "grouping_collision_count",
        "grouping_observed",
        "exit_code",
        "termination_signal",
        "stdout_bytes",
        "stderr_bytes",
        "stdout_sha256",
        "stderr_sha256",
        "result_file_present",
        "result_size_bytes",
        "result_readback_status",
        "process_cleanup_status",
        "diagnostic_persistence_status",
        "diagnostics_privacy_status",
        "harness_status",
        "harness_failure_category",
        "technical_gate_status",
        "package_published",
        "atomic_information_written",
    }
)


class SyntheticSemanticGateError(RuntimeError):
    """The technical Gate could not publish and read back its safe receipt."""


@dataclass(frozen=True)
class SyntheticSemanticGateRun:
    receipt_path: Path
    receipt: dict[str, object]
    anonymous_projection: dict[str, object]


def execute_synthetic_semantic_gate(
    batch: RepresentationAnalysisBatch,
    provider: CodexCliRepresentationAnalysisProvider,
    *,
    receipt_root: Path,
    post_success_assertion: Callable[[Mapping[str, object]], None] | None = None,
    post_success_serializer: Callable[[Mapping[str, object]], object] | None = None,
) -> SyntheticSemanticGateRun:
    """Run one synthetic batch through the production adapter and persist truth first.

    Hooks exist only to verify harness failure boundaries. Their return values are
    discarded and can never enter the technical receipt.
    """

    if type(provider) is not CodexCliRepresentationAnalysisProvider:
        raise SyntheticSemanticGateError("technical Gate 必须使用 production Provider adapter。")
    root = Path(receipt_root)
    _prepare_receipt_root(root)
    before_records = len(provider.execution_records)
    before_observations = len(provider.technical_observations)
    before_starts = provider.provider_start_count
    result: RepresentationAnalysisResult | None = None
    provider_exception = False
    try:
        result = provider.analyze(batch)
    except RepresentationInformationError:
        provider_exception = True
    except Exception:  # noqa: BLE001 - receipt distinguishes unknown after Popen.
        provider_exception = True

    records = provider.execution_records[before_records:]
    observations = provider.technical_observations[before_observations:]
    start_delta = provider.provider_start_count - before_starts
    record = records[0] if len(records) == 1 else None
    observation = observations[0] if len(observations) == 1 else None
    harness_failure: str | None = None
    if (
        len(records) > 1
        or len(observations) > 1
        or start_delta not in {0, 1}
        or (record is None) != (observation is None)
        or (
            record is not None
            and observation is not None
            and record.processing_run_id != observation.processing_run_id
        )
    ):
        harness_failure = "execution_observation_invalid"
        record = None
        observation = None

    if (
        record is not None
        and record.execution_status == "succeeded"
        and result is not None
    ):
        safe_observation = _safe_success_observation(record)
        if post_success_assertion is not None:
            try:
                post_success_assertion(safe_observation)
            except Exception:  # noqa: BLE001 - no exception content is persisted.
                harness_failure = "post_success_assertion_failure"
        if harness_failure is None and post_success_serializer is not None:
            try:
                post_success_serializer(safe_observation)
            except Exception:  # noqa: BLE001 - no exception content is persisted.
                harness_failure = "post_success_serialization_failure"

    if (
        observation is not None
        and observation.diagnostic_persistence_status == "failed"
    ):
        harness_failure = "diagnostics_persistence_failure"
    if record is not None and (
        (record.execution_status == "succeeded") != (result is not None)
    ):
        harness_failure = "execution_observation_invalid"

    receipt = _receipt_payload(
        provider,
        record,
        observation,
        eligible_units=len(batch.anchor_units),
        result_present=result is not None,
        provider_exception=provider_exception,
        start_delta=start_delta,
        harness_failure=harness_failure,
    )
    path = root / _RECEIPT_NAME
    _write_receipt_no_replace(path, receipt)
    readback = read_synthetic_semantic_gate_receipt(path)
    if readback != receipt:
        raise SyntheticSemanticGateError("technical Gate receipt 读回不一致。")
    projection = dict(readback)
    return SyntheticSemanticGateRun(path, readback, projection)


def _safe_success_observation(
    record: ExternalAgentExecutionRecord,
) -> dict[str, object]:
    """Expose only non-identifying counts to optional harness checks."""

    return {
        "execution_status": record.execution_status,
        "strict_validation_status": record.strict_validation_status,
        "eligible_units": record.eligible_units,
        "covered_units": record.covered_units,
        "raw_record_count": record.raw_record_count,
        "projected_record_count": record.projected_record_count,
        "duplicate_exact_body_count": record.duplicate_exact_body_count,
        "grouping_collision_count": record.grouping_collision_count,
        "grouping_observed": record.raw_record_count > record.projected_record_count,
    }


def read_synthetic_semantic_gate_receipt(path: Path) -> dict[str, object]:
    """Strictly read one private, content-free technical receipt."""

    receipt_path = Path(path)
    if receipt_path.name != _RECEIPT_NAME:
        raise SyntheticSemanticGateError("technical Gate receipt 路径无效。")
    _require_private_root(receipt_path.parent, exact_inventory={_RECEIPT_NAME})
    try:
        metadata = receipt_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise SyntheticSemanticGateError("technical Gate receipt 权限或类型无效。")
        payload = json.loads(
            receipt_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyntheticSemanticGateError("technical Gate receipt 无法安全读回。") from exc
    _validate_receipt(payload)
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SyntheticSemanticGateError("technical Gate receipt 含重复字段。")
        value[key] = item
    return value


def _receipt_payload(
    provider: CodexCliRepresentationAnalysisProvider,
    record: ExternalAgentExecutionRecord | None,
    observation: ExternalAgentTechnicalObservation | None,
    *,
    eligible_units: int,
    result_present: bool,
    provider_exception: bool,
    start_delta: int,
    harness_failure: str | None,
) -> dict[str, object]:
    started: bool | None = start_delta == 1 if start_delta in {0, 1} else None
    start_proven = start_delta in {0, 1}
    outcome_unknown = record is None and start_delta != 0
    if record is None and start_delta == 0:
        harness_failure = harness_failure or "pre_provider_failure"
    elif outcome_unknown:
        harness_failure = harness_failure or "provider_outcome_unknown"

    execution_status = record.execution_status if record is not None else (
        "unknown" if outcome_unknown else "not_started"
    )
    strict_status = record.strict_validation_status if record is not None else "unknown"
    cleanup_status = record.process_cleanup_status if record is not None else (
        "unknown" if outcome_unknown else "not_started"
    )
    result_readback = observation.result_readback_status if observation is not None else (
        "unknown" if outcome_unknown else "not_applicable"
    )
    diagnostic_status = (
        observation.diagnostic_persistence_status
        if observation is not None
        else ("unknown" if outcome_unknown else "not_applicable")
    )
    strict_pass = (
        record is not None
        and not provider_exception
        and result_present
        and execution_status == "succeeded"
        and strict_status == "passed"
        and result_readback == "verified"
        and cleanup_status == "verified"
    )
    gate_status = (
        "unknown"
        if outcome_unknown
        else "passed"
        if strict_pass and harness_failure is None
        else "failed"
    )
    raw_count = record.raw_record_count if record is not None else 0
    projected_count = record.projected_record_count if record is not None else 0
    payload: dict[str, object] = {
        "schema_version": SYNTHETIC_GATE_RECEIPT_SCHEMA_VERSION,
        "receipt_fingerprint": None,
        "provider_call_started": started,
        "provider_call_start_proven": start_proven,
        "provider_call_counted": 1 if start_delta != 0 else 0,
        "provider_outcome_unknown": outcome_unknown,
        "provider_execution_status": execution_status,
        "provider_failure_category": record.failure_category if record else None,
        "provider_error_category": record.provider_error_category if record else None,
        "contract_failure_detail": record.contract_failure_detail if record else None,
        "contract_failure_stage": record.contract_failure_stage if record else None,
        "strict_validation_status": strict_status,
        "protocol_version": record.protocol_version if record else None,
        "provider_route": record.provider_route if record else None,
        "provider_version": record.provider_version if record else provider.provider_version,
        "model": record.model if record else provider.model,
        "reasoning_effort": record.reasoning_effort if record else provider.reasoning_effort,
        "fallback_policy": record.fallback_policy if record else provider.fallback_policy,
        "eligible_units": record.eligible_units if record else eligible_units,
        "covered_units": record.covered_units if record else 0,
        "missing_anchor_count": record.missing_anchor_count if record else 0,
        "accounting_item_count": record.accounting_item_count if record else 0,
        "candidate_item_count": record.candidate_item_count if record else 0,
        "residue_item_count": record.residue_item_count if record else 0,
        "candidate_anchor_ref_count": record.candidate_anchor_ref_count if record else 0,
        "residue_anchor_ref_count": record.residue_anchor_ref_count if record else 0,
        "duplicate_anchor_ref_count": record.duplicate_anchor_ref_count if record else 0,
        "duplicate_accounting_count": record.duplicate_accounting_count if record else 0,
        "dual_assignment_count": record.dual_assignment_count if record else 0,
        "unknown_anchor_ref_count": record.unknown_anchor_ref_count if record else 0,
        "raw_record_count": raw_count,
        "projected_record_count": projected_count,
        "duplicate_exact_body_count": record.duplicate_exact_body_count if record else 0,
        "grouping_collision_count": record.grouping_collision_count if record else 0,
        "grouping_observed": bool(strict_pass and raw_count > projected_count),
        "exit_code": record.exit_code if record else None,
        "termination_signal": record.termination_signal if record else None,
        "stdout_bytes": record.stdout_bytes if record else (None if outcome_unknown else 0),
        "stderr_bytes": record.stderr_bytes if record else (None if outcome_unknown else 0),
        "stdout_sha256": observation.stdout_sha256 if observation else (None if outcome_unknown else _EMPTY_SHA256),
        "stderr_sha256": observation.stderr_sha256 if observation else (None if outcome_unknown else _EMPTY_SHA256),
        "result_file_present": record.result_file_present if record else (None if outcome_unknown else False),
        "result_size_bytes": record.result_size_bytes if record else (None if outcome_unknown else 0),
        "result_readback_status": result_readback,
        "process_cleanup_status": cleanup_status,
        "diagnostic_persistence_status": diagnostic_status,
        "diagnostics_privacy_status": (
            "unknown" if outcome_unknown else "passed"
        ),
        "harness_status": "failed" if harness_failure else "completed",
        "harness_failure_category": harness_failure,
        "technical_gate_status": gate_status,
        "package_published": False,
        "atomic_information_written": False,
    }
    payload["receipt_fingerprint"] = _receipt_fingerprint(payload)
    return payload


def _prepare_receipt_root(root: Path) -> None:
    if not root.is_absolute():
        raise SyntheticSemanticGateError("technical Gate receipt root 必须是绝对路径。")
    if os.path.lexists(root):
        _require_private_root(root, exact_inventory=set())
        return
    _require_safe_ancestors(root.parent)
    try:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
    except OSError as exc:
        raise SyntheticSemanticGateError("technical Gate receipt root 创建失败。") from exc
    _require_private_root(root, exact_inventory=set())


def _write_receipt_no_replace(path: Path, payload: Mapping[str, object]) -> None:
    _validate_receipt(dict(payload))
    root = path.parent
    _require_private_root(root, exact_inventory=set())
    temporary = root / f".{_RECEIPT_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            target.flush()
            os.fsync(target.fileno())
        publish_file_no_replace(temporary, path)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            if os.path.lexists(temporary):
                temporary.unlink()
        except OSError:
            pass
        raise SyntheticSemanticGateError("technical Gate receipt 原子写入失败。") from exc


def _require_private_root(root: Path, *, exact_inventory: set[str]) -> None:
    _require_safe_ancestors(root)
    try:
        metadata = root.lstat()
        names = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise SyntheticSemanticGateError("technical Gate receipt root 无法验证。") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or names != exact_inventory
    ):
        raise SyntheticSemanticGateError("technical Gate receipt root 不安全。")


def _require_safe_ancestors(path: Path) -> None:
    current = path
    while True:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise SyntheticSemanticGateError("technical Gate receipt 路径不可验证。") from exc
        mode = metadata.st_mode
        permissions = stat.S_IMODE(mode)
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SyntheticSemanticGateError("technical Gate receipt 路径包含非目录或链接。")
        if permissions & 0o022 and not permissions & stat.S_ISVTX:
            raise SyntheticSemanticGateError("technical Gate receipt 路径权限不安全。")
        if current == current.parent:
            break
        current = current.parent


def _validate_receipt(payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS:
        raise SyntheticSemanticGateError("technical Gate receipt schema 无效。")
    if payload["schema_version"] != SYNTHETIC_GATE_RECEIPT_SCHEMA_VERSION:
        raise SyntheticSemanticGateError("technical Gate receipt 版本无效。")
    fingerprint = payload["receipt_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or _SHA256.fullmatch(fingerprint) is None
        or fingerprint != _receipt_fingerprint(payload)
    ):
        raise SyntheticSemanticGateError("technical Gate receipt fingerprint 无效。")
    started = payload["provider_call_started"]
    proven = payload["provider_call_start_proven"]
    counted = payload["provider_call_counted"]
    unknown = payload["provider_outcome_unknown"]
    if (
        (started is not None and not isinstance(started, bool))
        or not isinstance(proven, bool)
        or isinstance(counted, bool)
        or counted not in {0, 1}
        or not isinstance(unknown, bool)
        or proven != (started is not None)
        or (counted == 0) != (started is False)
        or unknown
        != (
            started in {True, None}
            and payload["provider_execution_status"] == "unknown"
        )
    ):
        raise SyntheticSemanticGateError("technical Gate Provider call accounting 无效。")
    if payload["provider_execution_status"] not in {
        "succeeded",
        "failed",
        "not_started",
        "unknown",
    }:
        raise SyntheticSemanticGateError("technical Gate execution status 无效。")
    if payload["provider_failure_category"] not in _FAILURE_CATEGORIES | {None}:
        raise SyntheticSemanticGateError("technical Gate failure category 无效。")
    if payload["provider_error_category"] not in _PROVIDER_ERROR_CATEGORIES | {None}:
        raise SyntheticSemanticGateError("technical Gate provider error 无效。")
    if payload["contract_failure_detail"] not in CONTRACT_FAILURE_DETAILS | {None}:
        raise SyntheticSemanticGateError("technical Gate contract detail 无效。")
    if payload["contract_failure_stage"] not in _CONTRACT_FAILURE_STAGES | {None}:
        raise SyntheticSemanticGateError("technical Gate contract stage 无效。")
    if payload["strict_validation_status"] not in {"passed", "failed", "unknown"}:
        raise SyntheticSemanticGateError("technical Gate strict status 无效。")
    if payload["process_cleanup_status"] not in {"verified", "failed", "not_started", "unknown"}:
        raise SyntheticSemanticGateError("technical Gate cleanup status 无效。")
    if payload["result_readback_status"] not in {"verified", "not_applicable", "unknown"}:
        raise SyntheticSemanticGateError("technical Gate result readback 无效。")
    if payload["diagnostic_persistence_status"] not in {
        "verified",
        "failed",
        "not_applicable",
        "preflight_failed",
        "unknown",
    }:
        raise SyntheticSemanticGateError("technical Gate diagnostics status 无效。")
    if payload["diagnostics_privacy_status"] not in {"passed", "unknown"}:
        raise SyntheticSemanticGateError("technical Gate diagnostics privacy 无效。")
    if payload["harness_status"] not in {"completed", "failed"}:
        raise SyntheticSemanticGateError("technical Gate harness status 无效。")
    if payload["harness_failure_category"] not in _HARNESS_FAILURE_CATEGORIES | {None}:
        raise SyntheticSemanticGateError("technical Gate harness failure 无效。")
    if payload["technical_gate_status"] not in {"passed", "failed", "unknown"}:
        raise SyntheticSemanticGateError("technical Gate status 无效。")
    if (payload["technical_gate_status"] == "unknown") != unknown:
        raise SyntheticSemanticGateError("technical Gate unknown status 不一致。")
    execution_status = payload["provider_execution_status"]
    failure_category = payload["provider_failure_category"]
    strict_status = payload["strict_validation_status"]
    if (
        execution_status == "succeeded"
        and (failure_category is not None or strict_status != "passed")
    ) or (
        execution_status == "failed"
        and (failure_category is None or strict_status != "failed")
    ) or (
        execution_status in {"not_started", "unknown"}
        and (failure_category is not None or strict_status != "unknown")
    ):
        raise SyntheticSemanticGateError("technical Gate execution contract 不一致。")
    contract_failure = failure_category == "result_contract_failure"
    if contract_failure != (
        payload["contract_failure_detail"] is not None
        and payload["contract_failure_stage"] is not None
    ):
        raise SyntheticSemanticGateError("technical Gate contract failure 不一致。")
    count_fields = (
        "eligible_units",
        "covered_units",
        "missing_anchor_count",
        "accounting_item_count",
        "candidate_item_count",
        "residue_item_count",
        "candidate_anchor_ref_count",
        "residue_anchor_ref_count",
        "duplicate_anchor_ref_count",
        "duplicate_accounting_count",
        "dual_assignment_count",
        "unknown_anchor_ref_count",
        "raw_record_count",
        "projected_record_count",
        "duplicate_exact_body_count",
        "grouping_collision_count",
    )
    if any(
        isinstance(payload[name], bool)
        or not isinstance(payload[name], int)
        or payload[name] < 0
        for name in count_fields
    ) or payload["covered_units"] > payload["eligible_units"]:
        raise SyntheticSemanticGateError("technical Gate counts 无效。")
    for byte_name, hash_name in (
        ("stdout_bytes", "stdout_sha256"),
        ("stderr_bytes", "stderr_sha256"),
    ):
        byte_count = payload[byte_name]
        digest = payload[hash_name]
        if byte_count is None or digest is None:
            if not unknown or byte_count is not None or digest is not None:
                raise SyntheticSemanticGateError("technical Gate stream metadata 无效。")
        elif (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise SyntheticSemanticGateError("technical Gate stream metadata 无效。")
    if (
        not isinstance(payload["grouping_observed"], bool)
        or not isinstance(payload["package_published"], bool)
        or not isinstance(payload["atomic_information_written"], bool)
        or payload["package_published"]
        or payload["atomic_information_written"]
    ):
        raise SyntheticSemanticGateError("technical Gate safety boundary 无效。")
    if payload["grouping_observed"] and not (
        payload["provider_execution_status"] == "succeeded"
        and payload["strict_validation_status"] == "passed"
        and payload["raw_record_count"] > payload["projected_record_count"]
    ):
        raise SyntheticSemanticGateError("technical Gate grouping observation 无效。")
    result_present = payload["result_file_present"]
    result_size = payload["result_size_bytes"]
    if unknown:
        if result_present is not None or result_size is not None:
            raise SyntheticSemanticGateError("technical Gate unknown result metadata 无效。")
    elif (
        not isinstance(result_present, bool)
        or isinstance(result_size, bool)
        or not isinstance(result_size, int)
        or result_size < 0
    ):
        raise SyntheticSemanticGateError("technical Gate result metadata 无效。")
    for name in (
        "protocol_version",
        "provider_route",
        "provider_version",
        "model",
        "reasoning_effort",
        "fallback_policy",
    ):
        value = payload[name]
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", value) is None
        ):
            raise SyntheticSemanticGateError("technical Gate profile metadata 无效。")
    if payload["harness_status"] == "failed" and payload["harness_failure_category"] is None:
        raise SyntheticSemanticGateError("technical Gate harness failure detail 缺失。")
    if payload["harness_status"] == "completed" and payload["harness_failure_category"] is not None:
        raise SyntheticSemanticGateError("technical Gate harness state 不一致。")
    passed = payload["technical_gate_status"] == "passed"
    if passed and not (
        payload["provider_execution_status"] == "succeeded"
        and payload["strict_validation_status"] == "passed"
        and payload["result_readback_status"] == "verified"
        and payload["process_cleanup_status"] == "verified"
        and payload["diagnostics_privacy_status"] == "passed"
        and payload["harness_status"] == "completed"
    ):
        raise SyntheticSemanticGateError("technical Gate PASS 条件不完整。")


def _receipt_fingerprint(payload: Mapping[str, object]) -> str:
    value = dict(payload)
    value.pop("receipt_fingerprint", None)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
