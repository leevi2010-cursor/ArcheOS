"""Production orchestration for the approved External Agent semantic handoff."""

from __future__ import annotations

import hashlib
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
        if os.path.lexists(package):
            try:
                self._verify_replay_input(representation_id, package)
                validate_representation_information_package(package)
                audit_paths = self._matching_audit_paths(_package_fingerprint(package))
                if not audit_paths:
                    raise SemanticHandoffError(
                        "已存在的信息包缺少可读回的 Processing Run 审计。"
                    )
                self._validate_replay_audits(audit_paths)
                ingestion = ingest_processing_package(package, self.store)
                self._readback_store(ingestion)
                self._finalize_audits(audit_paths)
            except (
                OSError,
                ValueError,
                TypeError,
                RepresentationInformationError,
            ) as exc:
                raise SemanticHandoffError(
                    "已存在的信息包未能安全重放；未执行 External Agent。"
                ) from exc
            return SemanticHandoffResult(package, ingestion, audit_paths, True)

        try:
            package = self.representation_service.extract(representation_id, provider)
            validate_representation_information_package(package)
            package_fingerprint = _package_fingerprint(package)
            audit_paths = self._persist_audits(
                provider.execution_records,
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
            ingestion = ingest_processing_package(package, self.store)
            self._readback_store(ingestion)
        except (OSError, ValueError, TypeError, RepresentationInformationError) as exc:
            audit_paths = self._persist_audits(
                provider.execution_records,
                package_published=package.is_dir(),
                information_ingested=False,
                durable_ingestion_status="not_completed",
                package_fingerprint=(
                    _package_fingerprint(package) if package.is_dir() else None
                ),
                handoff_status="failed",
            )
            if not audit_paths and provider.execution_records:
                raise SemanticHandoffError("External Agent 审计无法安全保存。") from exc
            raise SemanticHandoffError(
                "External Agent 语义交接失败；未新增 Durable Atomic Information。"
            ) from exc
        try:
            self._finalize_audits(audit_paths)
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
        package_fingerprint: str | None,
        handoff_status: str,
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
                "execution_status": (record.execution_status),
                "failure_category": record.failure_category,
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

    def _verify_replay_input(self, representation_id: str, package: Path) -> None:
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

    def _validate_replay_audits(self, paths: tuple[Path, ...]) -> None:
        for path in paths:
            audit = _private_json_read(path)
            if (
                audit.get("execution_status") != "succeeded"
                or audit.get("strict_validation_status") != "passed"
                or audit.get("package_published") is not True
                or audit.get("audit_readback_status") != "verified"
                or audit.get("durable_ingestion_status") not in {"pending", "completed"}
            ):
                raise SemanticHandoffError("已存在的信息包审计状态不能安全重放。")

    def _readback_store(self, ingestion: IngestionResult) -> None:
        for atomic_information_id in ingestion.atomic_information_ids:
            observed = self.store.get_current(atomic_information_id)
            if observed.atomic_information_id != atomic_information_id:
                raise SemanticHandoffError("Durable Atomic Information 读回不一致。")

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
