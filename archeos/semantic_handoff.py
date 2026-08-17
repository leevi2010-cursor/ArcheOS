"""Production orchestration for the approved External Agent semantic handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .atomic_information import IngestionResult, ingest_processing_package
from .atomic_information.store import AtomicInformationStore
from .representation_information import (
    CONTRACT_FAILURE_DETAILS,
    EXTERNAL_AGENT_PROTOCOL_VERSION,
    EXTERNAL_AGENT_ROUTE,
    CodexCliRepresentationAnalysisProvider,
    ExternalAgentExecutionRecord,
    RepresentationInformationError,
    RepresentationInformationService,
    _analysis_batches_for_anchor_unit_ids,
    _external_agent_request,
    _provider_manifest,
    _units_from_representation,
    validate_representation_information_package,
)


class SemanticHandoffError(RuntimeError):
    """The External Agent handoff did not safely reach durable Information."""


@dataclass(frozen=True)
class SemanticHandoffResult:
    package: Path
    ingestion: IngestionResult
    audit_paths: tuple[Path, ...]
    replayed_existing_package: bool


class ExternalAgentSemanticHandoffService:
    """Coordinates package, audit, Readback and the one Atomic Information Store.

    The provider receives only canonical Analysis Units.  This orchestrator owns
    all package and durable Information writes; it imports no World Model code.
    """

    def __init__(
        self,
        representation_service: RepresentationInformationService,
        store: AtomicInformationStore,
        audit_root: Path,
    ) -> None:
        self.representation_service = representation_service
        self.store = store
        self.audit_root = Path(audit_root).expanduser()

    def execute(
        self,
        representation_id: str,
        provider: CodexCliRepresentationAnalysisProvider,
    ) -> SemanticHandoffResult:
        record_offset = len(provider.execution_records)
        package = self.representation_service.output_root / representation_id
        if os.path.lexists(package):
            try:
                manifest = self._verify_replay_input(representation_id, package)
                package_fingerprint = _package_fingerprint(package)
                expected_batches = self._expected_batch_contracts(
                    representation_id, manifest, provider
                )
                audit_paths = self._matching_audit_paths(package_fingerprint)
                self._validate_replay_audits(
                    audit_paths,
                    expected_batches,
                    provider,
                    package_fingerprint,
                )
                self._mark_durable_attempt(audit_paths)
                ingestion = ingest_processing_package(package, self.store)
                self._mark_durable_write(audit_paths)
                self._readback_store(ingestion)
                self._finalize_audits(audit_paths)
            except (
                OSError,
                ValueError,
                TypeError,
                RepresentationInformationError,
                SemanticHandoffError,
            ) as exc:
                raise SemanticHandoffError(
                    "已存在的信息包未能安全重放；未执行 External Agent。"
                ) from exc
            return SemanticHandoffResult(package, ingestion, audit_paths, True)

        audit_paths: tuple[Path, ...] = ()
        ingestion: IngestionResult | None = None
        try:
            package = self.representation_service.extract(representation_id, provider)
            manifest = self._verify_replay_input(representation_id, package)
            package_fingerprint = _package_fingerprint(package)
            records = provider.execution_records[record_offset:]
            expected_batches = self._expected_batch_contracts(
                representation_id, manifest, provider
            )
            self._validate_execution_records(records, expected_batches, provider)
            audit_paths = self._persist_audits(
                records,
                package_published=True,
                information_ingested=False,
                durable_ingestion_status="pending",
                package_fingerprint=package_fingerprint,
                handoff_status="pending",
            )
            if not audit_paths:
                raise SemanticHandoffError(
                    "External Agent 未生成可读回的 Processing Run 审计。"
                )
            self._mark_durable_attempt(audit_paths)
            ingestion = ingest_processing_package(package, self.store)
            self._mark_durable_write(audit_paths)
            self._readback_store(ingestion)
        except (
            OSError,
            ValueError,
            TypeError,
            RepresentationInformationError,
            SemanticHandoffError,
        ) as exc:
            if ingestion is not None:
                raise SemanticHandoffError(
                    "Durable Atomic Information 已写入或正在读回；Processing Run 审计需要 exact replay 恢复。"
                ) from exc
            if audit_paths:
                raise SemanticHandoffError(
                    "Durable Atomic Information 写入结果待核验；Processing Run 审计需要 exact replay 恢复。"
                ) from exc
            audit_paths = self._persist_audits(
                provider.execution_records[record_offset:],
                package_published=package.is_dir(),
                information_ingested=False,
                durable_ingestion_status="ingestion_not_completed",
                package_fingerprint=(
                    _package_fingerprint(package) if package.is_dir() else None
                ),
                handoff_status="failed",
            )
            if not audit_paths and provider.execution_records[record_offset:]:
                raise SemanticHandoffError("External Agent 审计无法安全保存。") from exc
            raise SemanticHandoffError(
                "External Agent 语义交接失败；未确认新增 Durable Atomic Information。"
            ) from exc
        try:
            self._finalize_audits(audit_paths)
        except SemanticHandoffError as exc:
            raise SemanticHandoffError(
                "Durable Atomic Information 已写入，但 Processing Run 审计仍为待完成；需要人工恢复审计读回。"
            ) from exc
        assert ingestion is not None
        return SemanticHandoffResult(package, ingestion, audit_paths, False)

    def _persist_audits(
        self,
        records: list[ExternalAgentExecutionRecord],
        *,
        package_published: bool,
        information_ingested: bool,
        durable_ingestion_status: str,
        package_fingerprint: str | None,
        handoff_status: str,
    ) -> tuple[Path, ...]:
        paths: list[Path] = []
        for record in records:
            if (
                record.failure_category == "result_contract_failure"
                and record.contract_failure_detail not in CONTRACT_FAILURE_DETAILS
            ) or (
                record.failure_category != "result_contract_failure"
                and record.contract_failure_detail is not None
            ):
                raise SemanticHandoffError("Processing Run 合同失败诊断字段无效。")
            payload = {
                "schema_version": "processing-run-audit/1.0",
                "artifact_kind": "processing_run_audit",
                "processing_run_id": record.processing_run_id,
                "protocol_version": record.protocol_version,
                "input_fingerprint": record.input_fingerprint,
                "anchor_unit_ids": list(record.anchor_unit_ids),
                "provider_route": record.provider_route,
                "provider_version": record.provider_version,
                "model": record.model,
                "reasoning_effort": record.reasoning_effort,
                "fallback_policy": record.fallback_policy,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "execution_status": (record.execution_status),
                "failure_category": record.failure_category,
                "contract_failure_detail": record.contract_failure_detail,
                "strict_validation_status": record.strict_validation_status,
                "result_fingerprint": record.result_fingerprint,
                "eligible_units": record.eligible_units,
                "covered_units": record.covered_units,
                "unaccounted_units": record.eligible_units - record.covered_units,
                "diagnostic_schema_version": record.diagnostic_schema_version,
                "elapsed_ms": record.elapsed_ms,
                "deadline_ms": record.deadline_ms,
                "exit_code": record.exit_code,
                "termination_signal": record.termination_signal,
                "timeout_phase": record.timeout_phase,
                "provider_error_category": record.provider_error_category,
                "result_file_present": record.result_file_present,
                "result_size_bytes": record.result_size_bytes,
                "stdout_bytes": record.stdout_bytes,
                "stderr_bytes": record.stderr_bytes,
                "process_cleanup_status": record.process_cleanup_status,
                "result_readback_status": (
                    "verified"
                    if record.execution_status == "succeeded"
                    else "not_applicable"
                ),
                "package_published": package_published,
                "package_fingerprint": package_fingerprint,
                "information_ingested": information_ingested,
                "durable_ingestion_status": durable_ingestion_status,
                "handoff_status": handoff_status,
                "audit_readback_status": "pending",
            }
            path = (
                self.audit_root / record.processing_run_id / "processing-run-audit.json"
            )
            _private_json_write(path, payload)
            readback = _private_json_read(path)
            if readback != payload:
                raise SemanticHandoffError("Processing Run 审计读回不一致。")
            payload["audit_readback_status"] = "verified"
            _private_json_write(path, payload)
            if _private_json_read(path) != payload:
                raise SemanticHandoffError("Processing Run 审计最终读回失败。")
            paths.append(path)
        return tuple(paths)

    def _verify_replay_input(
        self, representation_id: str, package: Path
    ) -> dict[str, object]:
        representation = self.representation_service.representation_repository.get(
            representation_id
        )
        verification = self.representation_service.representation_repository.verify(
            representation_id
        )
        if not verification.verified:
            raise SemanticHandoffError("已存在的信息包对应 Representation 校验失败。")
        self.representation_service._verify_source(representation)
        manifest, _ = validate_representation_information_package(package)
        source = manifest.get("source")
        package_representation = manifest.get("representation")
        if (
            not isinstance(source, dict)
            or not isinstance(package_representation, dict)
            or source.get("id") != representation.source_id
            or package_representation.get("representation_id")
            != representation.representation_id
            or source.get("content_hash") != representation.source_content_hash
        ):
            raise SemanticHandoffError("已存在的信息包不再匹配当前受管输入。")
        return manifest

    def _matching_audit_paths(self, package_fingerprint: str) -> tuple[Path, ...]:
        if not self.audit_root.is_dir():
            return ()
        matches: list[Path] = []
        for path in sorted(self.audit_root.glob("*/processing-run-audit.json")):
            try:
                audit = _private_json_read(path)
            except (OSError, json.JSONDecodeError, SemanticHandoffError):
                continue
            if audit.get("package_fingerprint") == package_fingerprint:
                matches.append(path)
        return tuple(matches)

    def _expected_batch_contracts(
        self,
        representation_id: str,
        manifest: dict[str, object],
        provider: CodexCliRepresentationAnalysisProvider,
    ) -> tuple[tuple[tuple[str, ...], str], ...]:
        package_provider = manifest.get("provider")
        if (
            not isinstance(package_provider, dict)
            or package_provider != _provider_manifest(provider)
        ):
            raise SemanticHandoffError("已存在的信息包 Provider 不匹配当前交接合同。")
        manifest_batches = manifest.get("batches")
        if not isinstance(manifest_batches, list):
            raise SemanticHandoffError("已存在的信息包批次清单不可读。")
        representation = self.representation_service.representation_repository.get(
            representation_id
        )
        batch_unit_ids: list[tuple[str, ...]] = []
        for package_batch in manifest_batches:
            if not isinstance(package_batch, dict):
                raise SemanticHandoffError("已存在的信息包批次清单不可读。")
            unit_ids = package_batch.get("unit_ids")
            if (
                not isinstance(unit_ids, list)
                or not unit_ids
                or any(not isinstance(item, str) for item in unit_ids)
            ):
                raise SemanticHandoffError("已存在的信息包批次清单不可读。")
            batch_unit_ids.append(tuple(unit_ids))
        try:
            batches = _analysis_batches_for_anchor_unit_ids(
                _units_from_representation(
                    representation, self.representation_service.representation_repository
                ),
                batch_unit_ids,
            )
        except RepresentationInformationError as exc:
            raise SemanticHandoffError("当前 canonical Analysis Unit 批次不再匹配信息包。") from exc
        if len(batches) != len(manifest_batches):
            raise SemanticHandoffError("当前 canonical Analysis Unit 批次不再匹配信息包。")
        contracts: list[tuple[tuple[str, ...], str]] = []
        for package_batch, batch in zip(manifest_batches, batches, strict=True):
            if not isinstance(package_batch, dict):
                raise SemanticHandoffError("已存在的信息包批次清单不可读。")
            anchor_unit_ids = tuple(unit.unit_id for unit in batch.anchor_units)
            if package_batch.get("unit_ids") != list(anchor_unit_ids):
                raise SemanticHandoffError("当前 canonical Analysis Unit 批次不再匹配信息包。")
            _, fingerprint = _external_agent_request(batch)
            contracts.append((anchor_unit_ids, fingerprint))
        return tuple(contracts)

    def _validate_execution_records(
        self,
        records: list[ExternalAgentExecutionRecord],
        expected_batches: tuple[tuple[tuple[str, ...], str], ...],
        provider: CodexCliRepresentationAnalysisProvider,
    ) -> None:
        if len(records) != len(expected_batches):
            raise SemanticHandoffError("当前信息包 Processing Run 审计数量不匹配。")
        for record, (anchor_unit_ids, input_fingerprint) in zip(
            records, expected_batches, strict=True
        ):
            if (
                record.protocol_version != EXTERNAL_AGENT_PROTOCOL_VERSION
                or record.input_fingerprint != input_fingerprint
                or record.anchor_unit_ids != anchor_unit_ids
                or record.provider_route != EXTERNAL_AGENT_ROUTE
                or record.provider_version != provider.provider_version
                or record.model != provider.model
                or record.reasoning_effort != provider.reasoning_effort
                or record.fallback_policy != provider.fallback_policy
                or record.execution_status != "succeeded"
                or record.failure_category is not None
                or record.contract_failure_detail is not None
                or record.strict_validation_status != "passed"
                or not _sha256_fingerprint(record.result_fingerprint)
                or record.eligible_units != len(anchor_unit_ids)
                or record.covered_units != len(anchor_unit_ids)
            ):
                raise SemanticHandoffError("当前信息包 Processing Run 审计不匹配。")

    def _validate_replay_audits(
        self,
        paths: tuple[Path, ...],
        expected_batches: tuple[tuple[tuple[str, ...], str], ...],
        provider: CodexCliRepresentationAnalysisProvider,
        package_fingerprint: str,
    ) -> None:
        if len(paths) != len(expected_batches):
            raise SemanticHandoffError("已存在的信息包缺少完整的 Processing Run 审计集合。")
        required_fields = {
            "schema_version",
            "artifact_kind",
            "processing_run_id",
            "protocol_version",
            "input_fingerprint",
            "anchor_unit_ids",
            "provider_route",
            "provider_version",
            "model",
            "reasoning_effort",
            "fallback_policy",
            "started_at",
            "finished_at",
            "execution_status",
            "failure_category",
            "strict_validation_status",
            "result_fingerprint",
            "eligible_units",
            "covered_units",
            "unaccounted_units",
            "diagnostic_schema_version",
            "elapsed_ms",
            "deadline_ms",
            "exit_code",
            "termination_signal",
            "timeout_phase",
            "provider_error_category",
            "result_file_present",
            "result_size_bytes",
            "stdout_bytes",
            "stderr_bytes",
            "process_cleanup_status",
            "result_readback_status",
            "package_published",
            "package_fingerprint",
            "information_ingested",
            "durable_ingestion_status",
            "handoff_status",
            "audit_readback_status",
        }
        diagnostic_fields = {
            "diagnostic_schema_version",
            "elapsed_ms",
            "deadline_ms",
            "exit_code",
            "termination_signal",
            "timeout_phase",
            "provider_error_category",
            "result_file_present",
            "result_size_bytes",
            "stdout_bytes",
            "stderr_bytes",
            "process_cleanup_status",
        }
        optional_fields = {"contract_failure_detail"}
        legacy_required_fields = required_fields - diagnostic_fields
        observed_batches: set[tuple[str, ...]] = set()
        expected_by_anchor = dict(expected_batches)
        for path in paths:
            audit = _private_json_read(path)
            if set(audit) not in {
                frozenset(required_fields),
                frozenset(legacy_required_fields),
                frozenset(required_fields | optional_fields),
                frozenset(legacy_required_fields | optional_fields),
            }:
                raise SemanticHandoffError("已存在的信息包审计不完整。")
            has_diagnostics = diagnostic_fields.issubset(audit)
            anchor_unit_ids = audit.get("anchor_unit_ids")
            if (
                not isinstance(anchor_unit_ids, list)
                or not anchor_unit_ids
                or any(not isinstance(item, str) for item in anchor_unit_ids)
            ):
                raise SemanticHandoffError("已存在的信息包审计缺少批次锚点。")
            batch = tuple(anchor_unit_ids)
            expected_fingerprint = expected_by_anchor.get(batch)
            if expected_fingerprint is None or batch in observed_batches:
                raise SemanticHandoffError("已存在的信息包审计批次集合不完整或冲突。")
            observed_batches.add(batch)
            durable_status = audit.get("durable_ingestion_status")
            audit_readback_status = audit.get("audit_readback_status")
            valid_completion_state = (
                durable_status == "pending"
                and audit.get("information_ingested") is False
                and audit_readback_status == "verified"
                and audit.get("handoff_status") == "pending"
            ) or (
                durable_status == "write_attempt_started"
                and audit.get("information_ingested") is False
                and audit_readback_status == "verified"
                and audit.get("handoff_status") == "pending_durable_write"
            ) or (
                durable_status == "written_readback_pending"
                and audit.get("information_ingested") is True
                and audit_readback_status in {"pending", "verified"}
                and audit.get("handoff_status") == "pending_readback"
            ) or (
                durable_status == "completed"
                and audit.get("information_ingested") is True
                and audit_readback_status in {"pending", "verified"}
                and audit.get("handoff_status") == "completed"
            )
            if (
                audit.get("schema_version") != "processing-run-audit/1.0"
                or audit.get("artifact_kind") != "processing_run_audit"
                or not _processing_run_id(audit.get("processing_run_id"))
                or audit.get("protocol_version") != EXTERNAL_AGENT_PROTOCOL_VERSION
                or audit.get("input_fingerprint") != expected_fingerprint
                or audit.get("provider_route") != EXTERNAL_AGENT_ROUTE
                or audit.get("provider_version") != provider.provider_version
                or audit.get("model") != provider.model
                or audit.get("reasoning_effort") != provider.reasoning_effort
                or audit.get("fallback_policy") != provider.fallback_policy
                or not _timestamp(audit.get("started_at"))
                or not _timestamp(audit.get("finished_at"))
                or audit.get("execution_status") != "succeeded"
                or audit.get("failure_category") is not None
                or audit.get("contract_failure_detail") is not None
                or audit.get("strict_validation_status") != "passed"
                or not _sha256_fingerprint(audit.get("result_fingerprint"))
                or audit.get("eligible_units") != len(batch)
                or audit.get("covered_units") != len(batch)
                or audit.get("unaccounted_units") != 0
                or has_diagnostics
                and (
                    audit.get("diagnostic_schema_version")
                    != "external-agent-diagnostics/1.0"
                    or not isinstance(audit.get("elapsed_ms"), int)
                    or audit.get("elapsed_ms") < 0
                    or not isinstance(audit.get("deadline_ms"), int)
                    or audit.get("deadline_ms") <= 0
                    or audit.get("exit_code") is not None
                    and not isinstance(audit.get("exit_code"), int)
                    or audit.get("termination_signal") is not None
                    and not isinstance(audit.get("termination_signal"), int)
                    or audit.get("timeout_phase") is not None
                    or audit.get("provider_error_category") is not None
                    or audit.get("result_file_present") is not True
                    or not isinstance(audit.get("result_size_bytes"), int)
                    or audit.get("result_size_bytes") < 0
                    or not isinstance(audit.get("stdout_bytes"), int)
                    or audit.get("stdout_bytes") < 0
                    or not isinstance(audit.get("stderr_bytes"), int)
                    or audit.get("stderr_bytes") < 0
                    or audit.get("process_cleanup_status") != "verified"
                )
                or audit.get("result_readback_status") != "verified"
                or audit.get("package_published") is not True
                or audit.get("package_fingerprint") != package_fingerprint
                or not valid_completion_state
            ):
                raise SemanticHandoffError("已存在的信息包审计状态不能安全重放。")
        if observed_batches != set(expected_by_anchor):
            raise SemanticHandoffError("已存在的信息包审计集合不完整。")

    def _readback_store(self, ingestion: IngestionResult) -> None:
        for atomic_information_id in ingestion.atomic_information_ids:
            observed = self.store.get_current(atomic_information_id)
            if observed.atomic_information_id != atomic_information_id:
                raise SemanticHandoffError("Durable Atomic Information 读回不一致。")

    def _mark_durable_attempt(self, paths: tuple[Path, ...]) -> None:
        """Record an auditable Store-write boundary before the unique Store call."""
        for path in paths:
            try:
                payload = _private_json_read(path)
                if payload.get("information_ingested") is True:
                    continue
                payload["durable_ingestion_status"] = "write_attempt_started"
                payload["handoff_status"] = "pending_durable_write"
                payload["audit_readback_status"] = "pending"
                _private_json_write(path, payload)
                if _private_json_read(path) != payload:
                    raise SemanticHandoffError("Processing Run 审计写入边界读回失败。")
                payload["audit_readback_status"] = "verified"
                _private_json_write(path, payload)
                if _private_json_read(path) != payload:
                    raise SemanticHandoffError("Processing Run 审计写入边界读回失败。")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raise SemanticHandoffError("Processing Run 审计写入边界读回失败。")

    def _mark_durable_write(self, paths: tuple[Path, ...]) -> None:
        """Persist the truthful recovery state before durable Store readback."""
        for path in paths:
            try:
                payload = _private_json_read(path)
                payload["information_ingested"] = True
                payload["durable_ingestion_status"] = "written_readback_pending"
                payload["handoff_status"] = "pending_readback"
                payload["audit_readback_status"] = "pending"
                _private_json_write(path, payload)
                if _private_json_read(path) != payload:
                    raise SemanticHandoffError("Processing Run 审计写入状态读回失败。")
                payload["audit_readback_status"] = "verified"
                _private_json_write(path, payload)
                if _private_json_read(path) != payload:
                    raise SemanticHandoffError("Processing Run 审计写入状态读回失败。")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raise SemanticHandoffError("Processing Run 审计写入状态读回失败。")

    def _finalize_audits(self, paths: tuple[Path, ...]) -> None:
        for path in paths:
            try:
                payload = _private_json_read(path)
                payload["information_ingested"] = True
                payload["durable_ingestion_status"] = "completed"
                payload["handoff_status"] = "completed"
                payload["audit_readback_status"] = "pending"
                _private_json_write(path, payload)
                if _private_json_read(path) != payload:
                    raise SemanticHandoffError("Processing Run 审计最终读回失败。")
                payload["audit_readback_status"] = "verified"
                _private_json_write(path, payload)
                if _private_json_read(path) != payload:
                    raise SemanticHandoffError("Processing Run 审计最终读回失败。")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raise SemanticHandoffError("Processing Run 审计最终读回失败。")


def _private_json_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            os.chmod(temporary, 0o600)
            target.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _private_json_read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SemanticHandoffError("Processing Run 审计不是对象。")
    return value


def _sha256_fingerprint(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _processing_run_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"run_[0-9a-f]{32}", value) is not None


def _timestamp(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value
    ) is not None


def _package_fingerprint(package: Path) -> str:
    """Stable private linkage between a strict package and its Processing Runs."""
    package = Path(package)
    if package.is_symlink() or not package.is_dir():
        raise SemanticHandoffError("Processing package 目录不安全。")
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(package).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()
