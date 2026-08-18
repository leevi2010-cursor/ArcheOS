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
    DIAGNOSTIC_SCHEMA_VERSION,
    EXTERNAL_AGENT_PROTOCOL_VERSION,
    EXTERNAL_AGENT_ROUTE,
    CodexCliRepresentationAnalysisProvider,
    ExternalAgentExecutionRecord,
    ExternalAgentTechnicalObservation,
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationInformationError,
    _external_agent_request,
    external_agent_representation_analysis_schema,
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
_CONTRACT_FAILURE_STAGES = {
    "top_level_schema": "top_level",
    "candidate_schema": "candidate",
    "residue_schema": "residue",
    "evidence_reference": "evidence_reference",
    "anchor_coverage": "coverage",
    "anchor_accounting": "accounting_cross_check",
    "record_grouping": "record_grouping",
    "unknown": "validation",
}
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
        "input_binding_status",
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
        or (
            record is not None
            and not _record_matches_execution_authority(record, provider, batch)
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
        record = None
        observation = None

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


def _record_matches_execution_authority(
    record: ExternalAgentExecutionRecord,
    provider: CodexCliRepresentationAnalysisProvider,
    batch: RepresentationAnalysisBatch,
) -> bool:
    try:
        schema = external_agent_representation_analysis_schema(batch=batch)
        _request, expected_input_fingerprint = _external_agent_request(
            batch,
            result_schema=schema,
        )
    except (RepresentationInformationError, TypeError, ValueError):
        return False
    return (
        record.protocol_version == EXTERNAL_AGENT_PROTOCOL_VERSION
        and record.provider_route == EXTERNAL_AGENT_ROUTE
        and record.provider_version == provider.provider_version
        and record.model == provider.model
        and record.reasoning_effort == provider.reasoning_effort
        and record.fallback_policy == provider.fallback_policy
        and record.eligible_units == len(batch.anchor_units)
        and record.anchor_unit_ids
        == tuple(unit.unit_id for unit in batch.anchor_units)
        and record.input_fingerprint == expected_input_fingerprint
        and record.diagnostic_schema_version == DIAGNOSTIC_SCHEMA_VERSION
        and record.deadline_ms == round(provider.timeout_seconds * 1000)
    )


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
    if start_delta == 0:
        if observation is not None and observation.diagnostic_persistence_status == "failed":
            harness_failure = "diagnostics_persistence_failure"
        else:
            harness_failure = "pre_provider_failure"
    elif outcome_unknown:
        harness_failure = harness_failure or "provider_outcome_unknown"

    effective_record = record if start_delta == 1 else None
    execution_status = effective_record.execution_status if effective_record is not None else (
        "unknown" if outcome_unknown else "not_started"
    )
    strict_status = (
        effective_record.strict_validation_status
        if effective_record is not None
        else "unknown"
    )
    cleanup_status = effective_record.process_cleanup_status if effective_record is not None else (
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
        effective_record is not None
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
    raw_count = effective_record.raw_record_count if effective_record is not None else 0
    projected_count = (
        effective_record.projected_record_count
        if effective_record is not None
        else 0
    )
    result_was_present = (
        effective_record.result_file_present
        if effective_record is not None
        else (None if outcome_unknown else False)
    )
    input_binding_status = (
        "verified"
        if effective_record is not None
        and (
            effective_record.execution_status == "succeeded"
            or effective_record.failure_category == "result_contract_failure"
        )
        else "failed"
        if effective_record is not None
        and effective_record.failure_category == "result_binding_failure"
        else "unknown"
        if outcome_unknown
        else "not_applicable"
    )
    payload: dict[str, object] = {
        "schema_version": SYNTHETIC_GATE_RECEIPT_SCHEMA_VERSION,
        "receipt_fingerprint": None,
        "provider_call_started": started,
        "provider_call_start_proven": start_proven,
        "provider_call_counted": 1 if start_delta != 0 else 0,
        "provider_outcome_unknown": outcome_unknown,
        "provider_execution_status": execution_status,
        "provider_failure_category": effective_record.failure_category if effective_record else None,
        "provider_error_category": effective_record.provider_error_category if effective_record else None,
        "contract_failure_detail": effective_record.contract_failure_detail if effective_record else None,
        "contract_failure_stage": effective_record.contract_failure_stage if effective_record else None,
        "strict_validation_status": strict_status,
        "input_binding_status": input_binding_status,
        "protocol_version": effective_record.protocol_version if effective_record else None,
        "provider_route": effective_record.provider_route if effective_record else None,
        "provider_version": effective_record.provider_version if effective_record else provider.provider_version,
        "model": effective_record.model if effective_record else provider.model,
        "reasoning_effort": effective_record.reasoning_effort if effective_record else provider.reasoning_effort,
        "fallback_policy": effective_record.fallback_policy if effective_record else provider.fallback_policy,
        "eligible_units": effective_record.eligible_units if effective_record else eligible_units,
        "covered_units": effective_record.covered_units if effective_record else 0,
        "missing_anchor_count": effective_record.missing_anchor_count if effective_record else 0,
        "accounting_item_count": effective_record.accounting_item_count if effective_record else 0,
        "candidate_item_count": effective_record.candidate_item_count if effective_record else 0,
        "residue_item_count": effective_record.residue_item_count if effective_record else 0,
        "candidate_anchor_ref_count": effective_record.candidate_anchor_ref_count if effective_record else 0,
        "residue_anchor_ref_count": effective_record.residue_anchor_ref_count if effective_record else 0,
        "duplicate_anchor_ref_count": effective_record.duplicate_anchor_ref_count if effective_record else 0,
        "duplicate_accounting_count": effective_record.duplicate_accounting_count if effective_record else 0,
        "dual_assignment_count": effective_record.dual_assignment_count if effective_record else 0,
        "unknown_anchor_ref_count": effective_record.unknown_anchor_ref_count if effective_record else 0,
        "raw_record_count": raw_count,
        "projected_record_count": projected_count,
        "duplicate_exact_body_count": effective_record.duplicate_exact_body_count if effective_record else 0,
        "grouping_collision_count": effective_record.grouping_collision_count if effective_record else 0,
        "grouping_observed": raw_count > projected_count,
        "exit_code": effective_record.exit_code if effective_record else None,
        "termination_signal": effective_record.termination_signal if effective_record else None,
        "stdout_bytes": effective_record.stdout_bytes if effective_record else (None if outcome_unknown else 0),
        "stderr_bytes": effective_record.stderr_bytes if effective_record else (None if outcome_unknown else 0),
        "stdout_sha256": observation.stdout_sha256 if effective_record and observation else (None if outcome_unknown else _EMPTY_SHA256),
        "stderr_sha256": observation.stderr_sha256 if effective_record and observation else (None if outcome_unknown else _EMPTY_SHA256),
        "result_file_present": result_was_present,
        "result_size_bytes": effective_record.result_size_bytes if effective_record else (None if outcome_unknown else 0),
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
    if payload["contract_failure_stage"] not in set(_CONTRACT_FAILURE_STAGES.values()) | {None}:
        raise SyntheticSemanticGateError("technical Gate contract stage 无效。")
    if payload["strict_validation_status"] not in {"passed", "failed", "unknown"}:
        raise SyntheticSemanticGateError("technical Gate strict status 无效。")
    if payload["input_binding_status"] not in {
        "verified",
        "failed",
        "not_applicable",
        "unknown",
    }:
        raise SyntheticSemanticGateError("technical Gate input binding 无效。")
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
    for name in ("exit_code", "termination_signal"):
        value = payload[name]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise SyntheticSemanticGateError("technical Gate child status 无效。")
    _validate_receipt_state(payload)


_COUNT_FIELDS = (
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
_CONTRACT_COUNT_FIELDS = (
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
)


def _validate_receipt_state(payload: dict[str, object]) -> None:
    """Validate one and only one complete technical receipt state."""

    if payload["eligible_units"] <= 0:
        raise SyntheticSemanticGateError("technical Gate eligible count 无效。")
    if (
        payload["provider_version"] is None
        or payload["model"] is None
        or payload["reasoning_effort"] not in {"low", "medium", "high", "xhigh"}
        or payload["fallback_policy"] != "none"
    ):
        raise SyntheticSemanticGateError("technical Gate execution profile 不完整。")
    if payload["grouping_observed"] != (
        payload["raw_record_count"] > payload["projected_record_count"]
    ):
        raise SyntheticSemanticGateError("technical Gate grouping observation 不一致。")
    _validate_result_state(payload)
    _validate_stream_state(payload)

    started = payload["provider_call_started"]
    unknown = payload["provider_outcome_unknown"]
    execution = payload["provider_execution_status"]
    if started is False:
        _validate_never_started_state(payload)
    elif unknown:
        _validate_outcome_unknown_state(payload)
    elif started is True and execution == "failed":
        _validate_provider_failed_state(payload)
    elif started is True and execution == "succeeded":
        _validate_strict_success_state(payload)
    else:
        raise SyntheticSemanticGateError("technical Gate state 无法归类。")


def _validate_result_state(payload: dict[str, object]) -> None:
    present = payload["result_file_present"]
    size = payload["result_size_bytes"]
    readback = payload["result_readback_status"]
    unknown = payload["provider_outcome_unknown"]
    if unknown:
        if present is not None or size is not None or readback != "unknown":
            raise SyntheticSemanticGateError("technical Gate unknown result state 无效。")
    elif present is True:
        if readback != "verified":
            raise SyntheticSemanticGateError("technical Gate result readback 不完整。")
    elif present is False:
        if size != 0 or readback != "not_applicable":
            raise SyntheticSemanticGateError("technical Gate absent result state 无效。")
    else:
        raise SyntheticSemanticGateError("technical Gate result presence 无效。")


def _validate_stream_state(payload: dict[str, object]) -> None:
    for count_name, hash_name in (
        ("stdout_bytes", "stdout_sha256"),
        ("stderr_bytes", "stderr_sha256"),
    ):
        count = payload[count_name]
        digest = payload[hash_name]
        if count == 0 and digest != _EMPTY_SHA256:
            raise SyntheticSemanticGateError("technical Gate empty stream hash 无效。")


def _validate_never_started_state(payload: dict[str, object]) -> None:
    diagnostics = payload["diagnostic_persistence_status"]
    expected_harness = (
        "diagnostics_persistence_failure"
        if diagnostics == "failed"
        else "pre_provider_failure"
    )
    if not (
        payload["provider_call_start_proven"] is True
        and payload["provider_call_counted"] == 0
        and payload["provider_outcome_unknown"] is False
        and payload["provider_execution_status"] == "not_started"
        and payload["provider_failure_category"] is None
        and payload["provider_error_category"] is None
        and payload["contract_failure_detail"] is None
        and payload["contract_failure_stage"] is None
        and payload["strict_validation_status"] == "unknown"
        and payload["input_binding_status"] == "not_applicable"
        and payload["protocol_version"] is None
        and payload["provider_route"] is None
        and all(payload[name] == 0 for name in _COUNT_FIELDS)
        and payload["exit_code"] is None
        and payload["termination_signal"] is None
        and payload["stdout_bytes"] == 0
        and payload["stderr_bytes"] == 0
        and payload["result_file_present"] is False
        and payload["process_cleanup_status"] == "not_started"
        and diagnostics
        in {"not_applicable", "preflight_failed", "verified", "failed"}
        and payload["diagnostics_privacy_status"] == "passed"
        and payload["harness_status"] == "failed"
        and payload["harness_failure_category"] == expected_harness
        and payload["technical_gate_status"] == "failed"
    ):
        raise SyntheticSemanticGateError("technical Gate never-started state 无效。")


def _validate_outcome_unknown_state(payload: dict[str, object]) -> None:
    if not (
        payload["provider_call_started"] in {True, None}
        and payload["provider_call_counted"] == 1
        and payload["provider_outcome_unknown"] is True
        and payload["provider_execution_status"] == "unknown"
        and payload["provider_failure_category"] is None
        and payload["provider_error_category"] is None
        and payload["contract_failure_detail"] is None
        and payload["contract_failure_stage"] is None
        and payload["strict_validation_status"] == "unknown"
        and payload["input_binding_status"] == "unknown"
        and payload["protocol_version"] is None
        and payload["provider_route"] is None
        and all(payload[name] == 0 for name in _COUNT_FIELDS)
        and payload["exit_code"] is None
        and payload["termination_signal"] is None
        and payload["stdout_bytes"] is None
        and payload["stderr_bytes"] is None
        and payload["result_file_present"] is None
        and payload["process_cleanup_status"] == "unknown"
        and payload["diagnostic_persistence_status"] == "unknown"
        and payload["diagnostics_privacy_status"] == "unknown"
        and payload["harness_status"] == "failed"
        and payload["harness_failure_category"]
        in {"provider_outcome_unknown", "execution_observation_invalid"}
        and payload["technical_gate_status"] == "unknown"
    ):
        raise SyntheticSemanticGateError("technical Gate outcome-unknown state 无效。")


def _validate_provider_failed_state(payload: dict[str, object]) -> None:
    failure = payload["provider_failure_category"]
    diagnostics = payload["diagnostic_persistence_status"]
    if not (
        payload["provider_call_start_proven"] is True
        and payload["provider_call_counted"] == 1
        and payload["provider_outcome_unknown"] is False
        and failure in _FAILURE_CATEGORIES - {"runtime_start_failure"}
        and payload["strict_validation_status"] == "failed"
        and payload["protocol_version"] == EXTERNAL_AGENT_PROTOCOL_VERSION
        and payload["provider_route"] == EXTERNAL_AGENT_ROUTE
        and payload["diagnostics_privacy_status"] == "passed"
        and diagnostics in {"verified", "failed"}
        and payload["technical_gate_status"] == "failed"
    ):
        raise SyntheticSemanticGateError("technical Gate provider-failed state 无效。")
    expected_harness = (
        "diagnostics_persistence_failure" if diagnostics == "failed" else None
    )
    expected_harness_status = "failed" if expected_harness else "completed"
    if (
        payload["harness_status"] != expected_harness_status
        or payload["harness_failure_category"] != expected_harness
    ):
        raise SyntheticSemanticGateError("technical Gate failed harness state 无效。")
    expected_cleanup = "failed" if failure == "process_cleanup_failure" else "verified"
    if payload["process_cleanup_status"] != expected_cleanup:
        raise SyntheticSemanticGateError("technical Gate failed cleanup state 无效。")
    result_bound = failure == "result_contract_failure"
    expected_binding = (
        "verified"
        if result_bound
        else "failed"
        if failure == "result_binding_failure"
        else "not_applicable"
    )
    if payload["input_binding_status"] != expected_binding:
        raise SyntheticSemanticGateError("technical Gate failed input binding 无效。")
    if failure in {"invalid_json", "result_binding_failure", "result_contract_failure"}:
        if payload["result_file_present"] is not True:
            raise SyntheticSemanticGateError("technical Gate strict failure 缺少result。")
    elif failure == "no_result" and payload["result_file_present"] is not False:
        raise SyntheticSemanticGateError("technical Gate no-result state 无效。")
    if failure in {
        "no_result",
        "invalid_json",
        "result_binding_failure",
        "result_contract_failure",
    } and (
        payload["exit_code"] != 0
        or payload["termination_signal"] is not None
        or payload["provider_error_category"] is not None
    ):
        raise SyntheticSemanticGateError("technical Gate strict failure child state 无效。")
    if failure == "runtime_nonzero_exit" and (
        payload["exit_code"] in {None, 0}
        or payload["provider_error_category"] is None
    ):
        raise SyntheticSemanticGateError("technical Gate nonzero exit state 无效。")
    if failure == "timeout" and payload["provider_error_category"] is None:
        raise SyntheticSemanticGateError("technical Gate timeout state 无效。")
    detail = payload["contract_failure_detail"]
    stage = payload["contract_failure_stage"]
    if result_bound:
        if detail not in CONTRACT_FAILURE_DETAILS or stage != _CONTRACT_FAILURE_STAGES[detail]:
            raise SyntheticSemanticGateError("technical Gate contract failure detail 无效。")
        _validate_contract_diagnostic_counts(payload)
    elif (
        detail is not None
        or stage is not None
        or payload["covered_units"] != 0
        or any(payload[name] != 0 for name in _CONTRACT_COUNT_FIELDS)
        or payload["raw_record_count"] != 0
        or payload["projected_record_count"] != 0
        or payload["duplicate_exact_body_count"] != 0
        or payload["grouping_collision_count"] != 0
    ):
        raise SyntheticSemanticGateError("technical Gate non-contract diagnostics 无效。")


def _validate_contract_diagnostic_counts(payload: dict[str, object]) -> None:
    """Validate identities that follow from the diagnostics projection itself."""

    eligible = payload["eligible_units"]
    covered = payload["covered_units"]
    missing = payload["missing_anchor_count"]
    raw = payload["raw_record_count"]
    projected = payload["projected_record_count"]
    accounting = payload["accounting_item_count"]
    unknown = payload["unknown_anchor_ref_count"]
    if missing != eligible - covered:
        raise SyntheticSemanticGateError("technical Gate contract coverage 无效。")
    if projected > raw:
        raise SyntheticSemanticGateError("technical Gate contract projection 无效。")
    if payload["duplicate_exact_body_count"] > raw:
        raise SyntheticSemanticGateError("technical Gate contract duplicate count 无效。")
    if payload["grouping_collision_count"] > projected:
        raise SyntheticSemanticGateError("technical Gate contract collision count 无效。")
    if raw > 0 and not (
        payload["candidate_item_count"]
        == payload["candidate_anchor_ref_count"]
        and payload["residue_item_count"]
        == payload["residue_anchor_ref_count"]
        and raw
        == payload["candidate_item_count"] + payload["residue_item_count"]
        and covered + unknown <= accounting <= eligible + unknown
    ):
        raise SyntheticSemanticGateError("technical Gate contract accounting 无效。")


def _validate_strict_success_state(payload: dict[str, object]) -> None:
    harness = payload["harness_status"]
    harness_failure = payload["harness_failure_category"]
    expected_gate = "passed" if harness == "completed" else "failed"
    if not (
        payload["provider_call_start_proven"] is True
        and payload["provider_call_counted"] == 1
        and payload["provider_outcome_unknown"] is False
        and payload["provider_failure_category"] is None
        and payload["provider_error_category"] is None
        and payload["contract_failure_detail"] is None
        and payload["contract_failure_stage"] is None
        and payload["strict_validation_status"] == "passed"
        and payload["input_binding_status"] == "verified"
        and payload["protocol_version"] == EXTERNAL_AGENT_PROTOCOL_VERSION
        and payload["provider_route"] == EXTERNAL_AGENT_ROUTE
        and payload["covered_units"] == payload["eligible_units"]
        and all(payload[name] == 0 for name in _CONTRACT_COUNT_FIELDS)
        and payload["raw_record_count"] >= payload["eligible_units"]
        and 0 < payload["projected_record_count"] <= payload["raw_record_count"]
        and payload["duplicate_exact_body_count"] == 0
        and payload["grouping_collision_count"] == 0
        and payload["exit_code"] == 0
        and payload["termination_signal"] is None
        and payload["result_file_present"] is True
        and payload["result_size_bytes"] > 0
        and payload["process_cleanup_status"] == "verified"
        and payload["diagnostic_persistence_status"] == "not_applicable"
        and payload["diagnostics_privacy_status"] == "passed"
        and payload["technical_gate_status"] == expected_gate
    ):
        raise SyntheticSemanticGateError("technical Gate strict-success state 无效。")
    if harness == "completed":
        if harness_failure is not None:
            raise SyntheticSemanticGateError("technical Gate successful harness state 无效。")
    elif harness == "failed":
        if harness_failure not in {
            "post_success_assertion_failure",
            "post_success_serialization_failure",
        }:
            raise SyntheticSemanticGateError("technical Gate post-success harness state 无效。")
    else:
        raise SyntheticSemanticGateError("technical Gate success harness status 无效。")


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
