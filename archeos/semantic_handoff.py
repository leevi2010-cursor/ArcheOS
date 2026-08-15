"""Production orchestration for the approved External Agent semantic handoff."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .atomic_information import IngestionResult, ingest_processing_package
from .atomic_information.store import AtomicInformationStore
from .representation_information import (
    CodexCliRepresentationAnalysisProvider,
    ExternalAgentExecutionRecord,
    RepresentationInformationError,
    RepresentationInformationService,
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
        package = self.representation_service.output_root / representation_id
        if package.exists():
            try:
                validate_representation_information_package(package)
                ingestion = ingest_processing_package(package, self.store)
            except (
                OSError,
                ValueError,
                TypeError,
                RepresentationInformationError,
            ) as exc:
                raise SemanticHandoffError(
                    "已存在的信息包未能安全重放；未执行 External Agent。"
                ) from exc
            return SemanticHandoffResult(package, ingestion, (), True)

        try:
            package = self.representation_service.extract(representation_id, provider)
            validate_representation_information_package(package)
            audit_paths = self._persist_audits(
                provider.execution_records,
                package_published=True,
                information_ingested=False,
                durable_ingestion_status="pending",
                failure_category=None,
            )
            if not audit_paths:
                raise SemanticHandoffError(
                    "External Agent 未生成可读回的 Processing Run 审计。"
                )
            ingestion = ingest_processing_package(package, self.store)
        except (OSError, ValueError, TypeError, RepresentationInformationError) as exc:
            audit_paths = self._persist_audits(
                provider.execution_records,
                package_published=package.is_dir(),
                information_ingested=False,
                durable_ingestion_status="not_completed",
                failure_category="handoff_failure",
            )
            if not audit_paths and provider.execution_records:
                raise SemanticHandoffError("External Agent 审计无法安全保存。") from exc
            raise SemanticHandoffError(
                "External Agent 语义交接失败；未新增 Durable Atomic Information。"
            ) from exc
        try:
            audit_paths = self._persist_audits(
                provider.execution_records,
                package_published=True,
                information_ingested=True,
                durable_ingestion_status="completed",
                failure_category=None,
            )
        except SemanticHandoffError as exc:
            raise SemanticHandoffError(
                "Durable Atomic Information 已写入，但 Processing Run 审计仍为待完成；需要人工恢复审计读回。"
            ) from exc
        return SemanticHandoffResult(package, ingestion, audit_paths, False)

    def _persist_audits(
        self,
        records: list[ExternalAgentExecutionRecord],
        *,
        package_published: bool,
        information_ingested: bool,
        durable_ingestion_status: str,
        failure_category: str | None,
    ) -> tuple[Path, ...]:
        paths: list[Path] = []
        for record in records:
            payload = {
                "schema_version": "processing-run-audit/1.0",
                "artifact_kind": "processing_run_audit",
                "processing_run_id": record.processing_run_id,
                "protocol_version": record.protocol_version,
                "input_fingerprint": record.input_fingerprint,
                "provider_route": record.provider_route,
                "provider_version": record.provider_version,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "execution_status": (
                    "succeeded"
                    if record.execution_status == "succeeded"
                    and failure_category is None
                    else "failed"
                ),
                "failure_category": failure_category or record.failure_category,
                "strict_validation_status": record.strict_validation_status,
                "result_fingerprint": record.result_fingerprint,
                "eligible_units": record.eligible_units,
                "covered_units": record.covered_units,
                "unaccounted_units": record.eligible_units - record.covered_units,
                "result_readback_status": (
                    "verified"
                    if record.execution_status == "succeeded"
                    else "not_applicable"
                ),
                "package_published": package_published,
                "information_ingested": information_ingested,
                "durable_ingestion_status": durable_ingestion_status,
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
