"""Production orchestration for the approved External Agent semantic handoff."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .atomic_information import IngestionResult, ingest_processing_package
from .atomic_information.store import AtomicInformationStore
from .filesystem import publish_directory_no_replace, publish_file_no_replace
from .representation_information import (
    CONTRACT_FAILURE_DETAILS,
    DIAGNOSTIC_SCHEMA_V1,
    DIAGNOSTIC_SCHEMA_V2,
    DIAGNOSTIC_SCHEMA_VERSION,
    EXTERNAL_AGENT_PROTOCOL_V1,
    EXTERNAL_AGENT_PROTOCOL_V2,
    EXTERNAL_AGENT_PROTOCOL_V3,
    EXTERNAL_AGENT_PROTOCOL_V3_1,
    EXTERNAL_AGENT_PROTOCOL_V3_2,
    EXTERNAL_AGENT_PROTOCOL_V3_3,
    EXTERNAL_AGENT_PROTOCOL_V3_4,
    EXTERNAL_AGENT_PROTOCOL_VERSION,
    EXTERNAL_AGENT_ROUTE,
    SUPPORTED_EXTERNAL_AGENT_PROTOCOL_VERSIONS,
    CodexCliRepresentationAnalysisProvider,
    ExternalAgentExecutionRecord,
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationInformationError,
    RepresentationInformationService,
    _analysis_batches,
    _analysis_batches_for_anchor_unit_ids,
    _external_agent_prompt,
    _external_agent_request,
    _ExternalAgentSuccessfulResult,
    _InternalAnalysisFinalization,
    _parse_external_agent_result,
    _provider_manifest,
    _units_from_representation,
    external_agent_representation_analysis_schema,
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


@dataclass(frozen=True)
class SemanticPrivacyBinding:
    """Caller-proved local privacy decision bound into a private run receipt."""

    policy: str
    policy_version: str
    route: str
    receipt_fingerprint: str


@dataclass(frozen=True)
class SemanticRecoveryPreflight:
    total_batches: int
    replayable_batches: int
    required_new_calls: int
    conservatively_counted_attempts: int
    historical_counted_attempts: int = 0


_RECOVERY_RUN_SCHEMA = "semantic-handoff-run-receipt/2.0"
_RECOVERY_ATTEMPT_SCHEMA = "semantic-handoff-attempt-receipt/2.0"
_RECOVERY_RESULT_SCHEMA = "semantic-handoff-batch-result-receipt/2.0"
_RECOVERY_RESULT_PHASE_SCHEMA = "semantic-handoff-batch-result-phase/1.0"
_V31_LOCAL_VALIDATOR_CONTRACT_VERSION = "external-agent-local-validator/3.1"
_V32_LOCAL_VALIDATOR_CONTRACT_VERSION = "external-agent-local-validator/3.2"
_V33_LOCAL_VALIDATOR_CONTRACT_VERSION = "external-agent-local-validator/3.3"
_LOCAL_VALIDATOR_CONTRACT_VERSION = "external-agent-local-validator/3.4"
_EXECUTION_RECORD_FIELDS = frozenset(
    ExternalAgentExecutionRecord.__dataclass_fields__
)
_V2_GROUPING_DIAGNOSTIC_FIELDS = frozenset(
    {
        "raw_record_count",
        "projected_record_count",
        "duplicate_exact_body_count",
        "grouping_collision_count",
    }
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bytes_fingerprint(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _payloads_exactly_equal(first: object, second: object) -> bool:
    return _canonical_bytes(first) == _canonical_bytes(second)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SemanticHandoffError("Semantic recovery 目录不可读。") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise SemanticHandoffError("Semantic recovery 目录权限不安全。")


def _require_private_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SemanticHandoffError("Semantic recovery 文件不可读。") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise SemanticHandoffError("Semantic recovery 文件权限不安全。")


def _private_bytes_read(path: Path) -> bytes:
    try:
        _require_private_file(path)
        return path.read_bytes()
    except SemanticHandoffError:
        raise
    except OSError as exc:
        raise SemanticHandoffError("Semantic recovery 文件不可读。") from exc


def _private_json_exact(path: Path) -> dict[str, object]:
    try:
        value = json.loads(_private_bytes_read(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticHandoffError("Semantic recovery receipt 不可读。") from exc
    if not isinstance(value, dict):
        raise SemanticHandoffError("Semantic recovery receipt 不是对象。")
    return value


def _write_staging_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if path.exists():
            path.unlink()
        raise


def _publish_private_json_marker(
    path: Path, payload: Mapping[str, object]
) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            os.chmod(temporary, 0o600)
            target.write(_canonical_bytes(payload) + b"\n")
            target.flush()
            os.fsync(target.fileno())
        publish_file_no_replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
        if not _payloads_exactly_equal(_private_json_exact(path), dict(payload)):
            raise SemanticHandoffError("Semantic recovery commit marker 读回失败。")
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _refsync_and_readback(
    *, files: tuple[Path, ...], directories: tuple[Path, ...]
) -> None:
    """Re-establish durability in this process; prior fsync return is irrelevant."""

    before = {path: _private_bytes_read(path) for path in files}
    for path in files:
        _fsync_file(path)
    for path in directories:
        _require_private_directory(path)
        _fsync_directory(path)
    after = {path: _private_bytes_read(path) for path in files}
    if after != before:
        raise SemanticHandoffError(
            "Semantic recovery post-strict byte readback 漂移。"
        )


def _ensure_private_directory(path: Path) -> None:
    if os.path.lexists(path):
        _require_private_directory(path)
        return
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _require_private_directory(path)
        return
    os.chmod(path, 0o700)
    _require_private_directory(path)
    _fsync_directory(path.parent)


def _require_safe_ancestor_traversal(path: Path) -> None:
    """Allow root/user-owned ancestors, but never symlink or unsafe traversal."""

    current = Path(os.path.abspath(path))
    while True:
        if os.path.lexists(current):
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise SemanticHandoffError(
                    "Semantic recovery ancestor 不可读。"
                ) from exc
            permissions = stat.S_IMODE(metadata.st_mode)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in {0, os.getuid()}
                or permissions & 0o022
                and not permissions & stat.S_ISVTX
            ):
                raise SemanticHandoffError(
                    "Semantic recovery ancestor traversal 不安全。"
                )
        if current == current.parent:
            return
        current = current.parent


def _validate_historical_audit_directory(path: Path) -> None:
    _require_private_directory(path)
    try:
        children = tuple(path.iterdir())
    except OSError as exc:
        raise SemanticHandoffError("Semantic recovery shared root 不可读。") from exc
    if {child.name for child in children} != {"processing-run-audit.json"}:
        raise SemanticHandoffError("Semantic recovery shared root inventory 损坏。")
    _require_private_file(children[0])


def _validate_recovery_result_directory(path: Path) -> None:
    _require_private_directory(path)
    try:
        children = {child.name: child for child in path.iterdir()}
    except OSError as exc:
        raise SemanticHandoffError("Semantic recovery result inventory 不可读。") from exc
    base = {"result.json", "result-receipt.json"}
    optional = {
        "result-commit.json",
        "result-commit-unknown.json",
        "phase-post-strict-pending.json",
        "phase-committed.json",
    }
    if not base.issubset(children) or not set(children) <= base | optional:
        raise SemanticHandoffError("Semantic recovery result inventory 损坏。")
    for child in children.values():
        _require_private_file(child)


def _validate_recovery_artifact_directory(path: Path) -> None:
    _require_private_directory(path)
    try:
        children = {child.name: child for child in path.iterdir()}
    except OSError as exc:
        raise SemanticHandoffError("Semantic recovery run inventory 不可读。") from exc
    allowed = {
        "run-receipt.json",
        "run-commit.json",
        "run-commit-unknown.json",
        "attempts",
        "results",
    }
    if "run-receipt.json" not in children or not set(children) <= allowed:
        raise SemanticHandoffError("Semantic recovery run inventory 损坏。")
    _require_private_file(children["run-receipt.json"])
    if "run-commit.json" in children:
        _require_private_file(children["run-commit.json"])
    if "run-commit-unknown.json" in children:
        _require_private_file(children["run-commit-unknown.json"])
    attempts = children.get("attempts")
    if attempts is not None:
        _require_private_directory(attempts)
        for child in attempts.iterdir():
            if re.fullmatch(r"batch_\d{4}\.json", child.name) is None:
                raise SemanticHandoffError(
                    "Semantic recovery attempt inventory 损坏。"
                )
            _require_private_file(child)
    results = children.get("results")
    if results is not None:
        _require_private_directory(results)
        for child in results.iterdir():
            if re.fullmatch(r"batch_\d{4}", child.name) is None:
                raise SemanticHandoffError(
                    "Semantic recovery result inventory 损坏。"
                )
            _validate_recovery_result_directory(child)


def _validate_shared_recovery_root(root: Path, *, create: bool) -> bool:
    """Validate the private shared audit/recovery root without repairing it."""

    root = Path(os.path.abspath(root))
    _require_safe_ancestor_traversal(root.parent)
    if not os.path.lexists(root):
        if not create:
            return False
        missing: list[Path] = []
        current = root
        while not os.path.lexists(current):
            missing.append(current)
            current = current.parent
        _require_safe_ancestor_traversal(current)
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            _require_private_directory(directory)
            _fsync_directory(directory.parent)
    _require_private_directory(root)
    try:
        entries = tuple(root.iterdir())
    except OSError as exc:
        raise SemanticHandoffError("Semantic recovery shared root 不可读。") from exc
    for entry in entries:
        if re.fullmatch(r"run_[0-9a-f]{32}", entry.name):
            _validate_historical_audit_directory(entry)
        elif re.fullmatch(r"semantic_run_[0-9a-f]{32}", entry.name):
            _validate_recovery_artifact_directory(entry)
        else:
            raise SemanticHandoffError(
                "Semantic recovery shared root inventory 损坏。"
            )
    return True


def _record_payload(record: ExternalAgentExecutionRecord) -> dict[str, object]:
    payload = asdict(record)
    payload["anchor_unit_ids"] = list(record.anchor_unit_ids)
    return payload


def _record_from_payload(value: object) -> ExternalAgentExecutionRecord:
    if not isinstance(value, dict):
        raise SemanticHandoffError("Semantic recovery execution receipt 损坏。")
    fields = set(value)
    current_v3 = (
        value.get("protocol_version") == EXTERNAL_AGENT_PROTOCOL_V3_4
        and fields == _EXECUTION_RECORD_FIELDS
    )
    historical_v2 = (
        value.get("protocol_version")
        in {
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            EXTERNAL_AGENT_PROTOCOL_V3_2,
            EXTERNAL_AGENT_PROTOCOL_V3_3,
        }
        and fields == _EXECUTION_RECORD_FIELDS - _V2_GROUPING_DIAGNOSTIC_FIELDS
    )
    if not current_v3 and not historical_v2:
        raise SemanticHandoffError("Semantic recovery execution receipt 损坏。")
    anchor_unit_ids = value.get("anchor_unit_ids")
    if not isinstance(anchor_unit_ids, list) or any(
        not isinstance(item, str) for item in anchor_unit_ids
    ):
        raise SemanticHandoffError("Semantic recovery execution receipt 损坏。")
    payload = dict(value)
    payload["anchor_unit_ids"] = tuple(anchor_unit_ids)
    if historical_v2:
        payload.update({field: 0 for field in _V2_GROUPING_DIAGNOSTIC_FIELDS})
    try:
        return ExternalAgentExecutionRecord(**payload)  # type: ignore[arg-type]
    except TypeError as exc:
        raise SemanticHandoffError(
            "Semantic recovery execution receipt 损坏。"
        ) from exc


class _SemanticRecoveryRun:
    """Private Processing Run artifacts; never a package or business Store."""

    def __init__(
        self,
        representation_service: RepresentationInformationService,
        audit_root: Path,
        representation_id: str,
        provider: CodexCliRepresentationAnalysisProvider,
        privacy: SemanticPrivacyBinding,
    ) -> None:
        if (
            not isinstance(privacy, SemanticPrivacyBinding)
            or privacy.route != "approved"
            or not all(
                isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", value)
                for value in (privacy.policy, privacy.policy_version, privacy.route)
            )
            or not _sha256_fingerprint(privacy.receipt_fingerprint)
        ):
            raise SemanticHandoffError(
                "Semantic recovery 缺少已批准的本地 privacy receipt。"
            )
        self.representation_service = representation_service
        self.audit_root = Path(
            os.path.abspath(Path(audit_root).expanduser())
        )
        self.provider = provider
        self.privacy = privacy
        representation = representation_service.representation_repository.get(
            representation_id
        )
        verification = representation_service.representation_repository.verify(
            representation_id
        )
        if not verification.verified:
            raise SemanticHandoffError("Semantic recovery Representation 校验失败。")
        representation_service._verify_source(representation)
        self.representation = representation
        self.units = _units_from_representation(
            representation,
            representation_service.representation_repository,
        )
        self.batches = _analysis_batches(
            self.units, representation_service.batch_size
        )
        if not self.batches:
            raise SemanticHandoffError("Semantic recovery 没有可执行的 canonical batch。")
        self.batch_contracts = tuple(
            self._batch_contract(
                batch,
                ordinal,
                protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_4,
            )
            for ordinal, batch in enumerate(self.batches, start=1)
        )
        execution_identity = self._execution_identity(
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_4,
            validator_version=_LOCAL_VALIDATOR_CONTRACT_VERSION,
            batch_contracts=self.batch_contracts,
        )
        self.execution_identity_fingerprint = _fingerprint(execution_identity)
        self.semantic_run_id = (
            "semantic_run_"
            + self.execution_identity_fingerprint.removeprefix("sha256:")[:32]
        )
        self.run_dir = self.audit_root / self.semantic_run_id
        self.attempts_dir = self.run_dir / "attempts"
        self.results_dir = self.run_dir / "results"
        self.contract_fingerprint, self.expected_run_receipt = (
            self._expected_run_receipt(
                self.semantic_run_id,
                execution_identity,
                execution_identity_fingerprint=(
                    self.execution_identity_fingerprint
                ),
            )
        )
        self.historical_v31_run_id = "semantic_run_" + hashlib.sha256(
            representation_id.encode("utf-8")
        ).hexdigest()[:32]
        self.historical_v31_batch_contracts = tuple(
            self._batch_contract(
                batch,
                ordinal,
                protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
            )
            for ordinal, batch in enumerate(self.batches, start=1)
        )
        historical_identity = self._execution_identity(
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
            validator_version=_V31_LOCAL_VALIDATOR_CONTRACT_VERSION,
            batch_contracts=self.historical_v31_batch_contracts,
        )
        (
            self.historical_v31_contract_fingerprint,
            self.expected_historical_v31_run_receipt,
        ) = self._expected_run_receipt(
            self.historical_v31_run_id,
            historical_identity,
        )
        self.historical_v32_batch_contracts = tuple(
            self._batch_contract(
                batch,
                ordinal,
                protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2,
            )
            for ordinal, batch in enumerate(self.batches, start=1)
        )
        historical_v32_identity = self._execution_identity(
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2,
            validator_version=_V32_LOCAL_VALIDATOR_CONTRACT_VERSION,
            batch_contracts=self.historical_v32_batch_contracts,
        )
        self.historical_v32_execution_identity_fingerprint = _fingerprint(
            historical_v32_identity
        )
        self.historical_v32_run_id = (
            "semantic_run_"
            + self.historical_v32_execution_identity_fingerprint.removeprefix(
                "sha256:"
            )[:32]
        )
        (
            self.historical_v32_contract_fingerprint,
            self.expected_historical_v32_run_receipt,
        ) = self._expected_run_receipt(
            self.historical_v32_run_id,
            historical_v32_identity,
            execution_identity_fingerprint=(
                self.historical_v32_execution_identity_fingerprint
            ),
        )
        self.historical_v33_batch_contracts = tuple(
            self._batch_contract(
                batch,
                ordinal,
                protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_3,
            )
            for ordinal, batch in enumerate(self.batches, start=1)
        )
        historical_v33_identity = self._execution_identity(
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_3,
            validator_version=_V33_LOCAL_VALIDATOR_CONTRACT_VERSION,
            batch_contracts=self.historical_v33_batch_contracts,
        )
        self.historical_v33_execution_identity_fingerprint = _fingerprint(
            historical_v33_identity
        )
        self.historical_v33_run_id = (
            "semantic_run_"
            + self.historical_v33_execution_identity_fingerprint.removeprefix(
                "sha256:"
            )[:32]
        )
        (
            self.historical_v33_contract_fingerprint,
            self.expected_historical_v33_run_receipt,
        ) = self._expected_run_receipt(
            self.historical_v33_run_id,
            historical_v33_identity,
            execution_identity_fingerprint=(
                self.historical_v33_execution_identity_fingerprint
            ),
        )

    def _execution_identity(
        self,
        *,
        protocol_version: str,
        validator_version: str,
        batch_contracts: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        return {
            "source": {
                "source_id": self.representation.source_id,
                "content_hash": self.representation.source_content_hash,
            },
            "representation": {
                "representation_id": self.representation.representation_id,
                "manifest_fingerprint": _fingerprint(
                    self.representation.to_manifest_dict()
                ),
                "artifacts": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "content_hash": artifact.content_hash,
                    }
                    for artifact in self.representation.artifacts
                ],
            },
            "privacy": asdict(self.privacy),
            "protocol_version": protocol_version,
            "provider_route": EXTERNAL_AGENT_ROUTE,
            "provider": _provider_manifest(self.provider),
            "execution_deadline_ms": round(
                self.provider.timeout_seconds * 1000
            ),
            "semantic_batch_size": self.representation_service.batch_size,
            "ordered_eligible_unit_ids": [
                unit.unit_id for unit in self.units if unit.analysis_eligible
            ],
            "prompt_template_fingerprint": _fingerprint(
                _external_agent_prompt(
                    {
                        "protocol_version": protocol_version,
                        "template_probe": True,
                    }
                )
            ),
            "local_validator_contract_version": validator_version,
            "batches": [contract["receipt"] for contract in batch_contracts],
        }

    @staticmethod
    def _expected_run_receipt(
        semantic_run_id: str,
        execution_identity: Mapping[str, object],
        *,
        execution_identity_fingerprint: str | None = None,
    ) -> tuple[str, dict[str, object]]:
        receipt_without_fingerprint: dict[str, object] = {
            "schema_version": _RECOVERY_RUN_SCHEMA,
            "artifact_kind": "semantic_handoff_recovery_run",
            "semantic_run_id": semantic_run_id,
            **(
                {
                    "execution_identity_fingerprint": (
                        execution_identity_fingerprint
                    )
                }
                if execution_identity_fingerprint is not None
                else {}
            ),
            **execution_identity,
        }
        contract_fingerprint = _fingerprint(receipt_without_fingerprint)
        run_receipt_without_fingerprint = {
            **receipt_without_fingerprint,
            "contract_fingerprint": contract_fingerprint,
        }
        return contract_fingerprint, {
            **run_receipt_without_fingerprint,
            "run_receipt_fingerprint": _fingerprint(
                run_receipt_without_fingerprint
            ),
        }

    def _batch_contract(
        self,
        batch: RepresentationAnalysisBatch,
        ordinal: int,
        *,
        protocol_version: str,
    ) -> dict[str, object]:
        schema = external_agent_representation_analysis_schema(
            protocol_version,
            batch=batch,
        )
        _, input_fingerprint = _external_agent_request(
            batch,
            protocol_version=protocol_version,
            result_schema=schema,
        )
        payload: dict[str, object] = {
            "ordinal": ordinal,
            "total": len(self.batches),
            "anchor_unit_ids": [unit.unit_id for unit in batch.anchor_units],
            "context_support_unit_ids": [
                unit.unit_id for unit in batch.context_support_units
            ],
            "input_fingerprint": input_fingerprint,
            "result_schema_fingerprint": _fingerprint(schema),
        }
        payload["batch_contract_fingerprint"] = _fingerprint(payload)
        return {"batch": batch, "receipt": payload}

    @property
    def exists(self) -> bool:
        return os.path.lexists(self.run_dir)

    def ensure_run_receipt(self) -> None:
        _validate_shared_recovery_root(self.audit_root, create=True)
        if self.exists:
            self._converge_run_receipt()
            return
        staging: Path | None = None
        try:
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{self.semantic_run_id}.", dir=self.audit_root
                )
            )
            os.chmod(staging, 0o700)
            _write_staging_file(
                staging / "run-receipt.json",
                _canonical_bytes(self.expected_run_receipt) + b"\n",
            )
            _fsync_directory(staging)
            publish_directory_no_replace(staging, self.run_dir)
            staging = None
        except FileExistsError:
            pass
        finally:
            if staging is not None and staging.exists():
                for child in staging.iterdir():
                    child.unlink()
                staging.rmdir()
        self._converge_run_receipt()

    def _validate_run_receipt_payload(self) -> None:
        _validate_shared_recovery_root(self.audit_root, create=False)
        _require_private_directory(self.run_dir)
        allowed = {"run-receipt.json", "attempts", "results"}
        try:
            inventory = {path.name for path in self.run_dir.iterdir()}
        except OSError as exc:
            raise SemanticHandoffError("Semantic recovery inventory 不可读。") from exc
        if (
            "run-receipt.json" not in inventory
            or not inventory <= allowed
        ):
            raise SemanticHandoffError("Semantic recovery inventory 损坏。")
        if not _payloads_exactly_equal(
            _private_json_exact(self.run_dir / "run-receipt.json"),
            self.expected_run_receipt,
        ):
            raise SemanticHandoffError("Semantic recovery run binding 漂移。")

    def _converge_run_receipt(self) -> None:
        self._validate_run_receipt_payload()
        _refsync_and_readback(
            files=(self.run_dir / "run-receipt.json",),
            directories=(self.run_dir, self.audit_root),
        )
        self._validate_run_receipt_payload()

    def _attempt_path(self, ordinal: int) -> Path:
        return self.attempts_dir / f"batch_{ordinal:04d}.json"

    def _result_path(self, ordinal: int) -> Path:
        return self.results_dir / f"batch_{ordinal:04d}"

    def _result_phase_payload(
        self,
        ordinal: int,
        result_receipt: Mapping[str, object],
        phase: str,
    ) -> dict[str, object]:
        if phase not in {"post_strict_pending", "committed"}:
            raise SemanticHandoffError("Semantic recovery phase 无效。")
        batch_receipt = self.batch_contracts[ordinal - 1]["receipt"]
        assert isinstance(batch_receipt, dict)
        return self._expected_result_phase_payload(
            semantic_run_id=self.semantic_run_id,
            contract_fingerprint=self.contract_fingerprint,
            batch_receipt=batch_receipt,
            ordinal=ordinal,
            result_receipt=result_receipt,
            phase=phase,
        )

    @staticmethod
    def _expected_result_phase_payload(
        *,
        semantic_run_id: str,
        contract_fingerprint: str,
        batch_receipt: Mapping[str, object],
        ordinal: int,
        result_receipt: Mapping[str, object],
        phase: str,
    ) -> dict[str, object]:
        if phase not in {"post_strict_pending", "committed"}:
            raise SemanticHandoffError("Semantic recovery phase 无效。")
        return {
            "schema_version": _RECOVERY_RESULT_PHASE_SCHEMA,
            "artifact_kind": "semantic_handoff_batch_result_phase",
            "semantic_run_id": semantic_run_id,
            "run_contract_fingerprint": contract_fingerprint,
            "batch_ordinal": ordinal,
            "batch_contract_fingerprint": batch_receipt[
                "batch_contract_fingerprint"
            ],
            "attempt_id": result_receipt["attempt_id"],
            "attempt_nonce": result_receipt["attempt_nonce"],
            "attempt_receipt_fingerprint": result_receipt[
                "attempt_receipt_fingerprint"
            ],
            "result_sha256": result_receipt["result_sha256"],
            "result_receipt_fingerprint": result_receipt[
                "result_receipt_fingerprint"
            ],
            "phase": phase,
        }

    def _attempt_payload(self, ordinal: int, attempt_nonce: str) -> dict[str, object]:
        batch_receipt = self.batch_contracts[ordinal - 1]["receipt"]
        assert isinstance(batch_receipt, dict)
        return self._expected_attempt_payload(
            semantic_run_id=self.semantic_run_id,
            contract_fingerprint=self.contract_fingerprint,
            batch_receipt=batch_receipt,
            ordinal=ordinal,
            attempt_nonce=attempt_nonce,
        )

    @staticmethod
    def _expected_attempt_payload(
        *,
        semantic_run_id: str,
        contract_fingerprint: str,
        batch_receipt: Mapping[str, object],
        ordinal: int,
        attempt_nonce: str,
    ) -> dict[str, object]:
        if re.fullmatch(r"[0-9a-f]{64}", attempt_nonce) is None:
            raise SemanticHandoffError("Semantic recovery attempt nonce 无效。")
        attempt_id = "attempt_" + hashlib.sha256(
            f"{semantic_run_id}:{ordinal}:{attempt_nonce}".encode()
        ).hexdigest()[:32]
        without_fingerprint: dict[str, object] = {
            "schema_version": _RECOVERY_ATTEMPT_SCHEMA,
            "artifact_kind": "semantic_handoff_attempt",
            "attempt_id": attempt_id,
            "attempt_nonce": attempt_nonce,
            "semantic_run_id": semantic_run_id,
            "run_contract_fingerprint": contract_fingerprint,
            "batch_ordinal": ordinal,
            "batch_contract_fingerprint": batch_receipt[
                "batch_contract_fingerprint"
            ],
            "input_fingerprint": batch_receipt["input_fingerprint"],
            "state": "started",
        }
        return {
            **without_fingerprint,
            "attempt_receipt_fingerprint": _fingerprint(without_fingerprint),
        }

    def _load_attempt(self, ordinal: int) -> dict[str, object]:
        payload = _private_json_exact(self._attempt_path(ordinal))
        nonce = payload.get("attempt_nonce")
        if not isinstance(nonce, str) or not _payloads_exactly_equal(
            payload, self._attempt_payload(ordinal, nonce)
        ):
            raise SemanticHandoffError("Semantic recovery attempt 损坏。")
        return payload

    def publish_attempt(self, ordinal: int) -> dict[str, object]:
        self._validate_inventory()
        _ensure_private_directory(self.attempts_dir)
        path = self._attempt_path(ordinal)
        expected = self._attempt_payload(ordinal, secrets.token_hex(32))
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.attempts_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as target:
                temporary = Path(target.name)
                os.chmod(temporary, 0o600)
                target.write(_canonical_bytes(expected) + b"\n")
                target.flush()
                os.fsync(target.fileno())
            publish_file_no_replace(temporary, path)
            temporary = None
            _fsync_directory(self.attempts_dir)
        except FileExistsError as exc:
            raise SemanticHandoffError(
                "Semantic recovery batch 已存在可能调用的 attempt；停止调用。"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        if not _payloads_exactly_equal(_private_json_exact(path), expected):
            raise SemanticHandoffError("Semantic recovery attempt 读回不一致。")
        return expected

    def _validate_inventory(self) -> None:
        self._converge_run_receipt()
        total = len(self.batches)
        expected_names = {f"batch_{ordinal:04d}" for ordinal in range(1, total + 1)}
        if os.path.lexists(self.attempts_dir):
            _require_private_directory(self.attempts_dir)
            attempt_names = {path.name for path in self.attempts_dir.iterdir()}
            expected_attempts = {f"{name}.json" for name in expected_names}
            if not attempt_names <= expected_attempts:
                raise SemanticHandoffError("Semantic recovery attempt inventory 损坏。")
        if os.path.lexists(self.results_dir):
            _require_private_directory(self.results_dir)
            result_names = {path.name for path in self.results_dir.iterdir()}
            if not result_names <= expected_names:
                raise SemanticHandoffError("Semantic recovery result inventory 损坏。")

    def inspect(
        self,
    ) -> tuple[
        tuple[
            tuple[RepresentationAnalysisResult, ExternalAgentExecutionRecord]
            | None,
            ...,
        ],
        int,
    ]:
        if not _validate_shared_recovery_root(
            self.audit_root, create=False
        ):
            return tuple(None for _ in self.batches), 0
        self._reject_conflicting_execution_runs()
        if not self.exists:
            return tuple(None for _ in self.batches), 0
        self._validate_inventory()
        loaded: list[
            tuple[RepresentationAnalysisResult, ExternalAgentExecutionRecord]
            | None
        ] = []
        unknown = 0
        missing_seen = False
        for ordinal, contract in enumerate(self.batch_contracts, start=1):
            attempt_path = self._attempt_path(ordinal)
            result_path = self._result_path(ordinal)
            attempt_exists = os.path.lexists(attempt_path)
            result_exists = os.path.lexists(result_path)
            if result_exists and not attempt_exists:
                raise SemanticHandoffError(
                    "Semantic recovery result 缺少 Provider attempt binding。"
                )
            if result_exists:
                if missing_seen:
                    raise SemanticHandoffError(
                        "Semantic recovery result 未按 canonical 顺序收敛。"
                    )
                attempt = self._load_attempt(ordinal)
                loaded.append(
                    self._converge_result(ordinal, contract, attempt)
                )
                continue
            missing_seen = True
            loaded.append(None)
            if attempt_exists:
                self._load_attempt(ordinal)
                unknown += 1
            if any(
                os.path.lexists(self._attempt_path(later))
                or os.path.lexists(self._result_path(later))
                for later in range(ordinal + 1, len(self.batches) + 1)
            ):
                raise SemanticHandoffError(
                    "Semantic recovery attempt 未按 canonical 顺序执行。"
                )
        return tuple(loaded), unknown

    def _reject_conflicting_execution_runs(self) -> None:
        for path in self.audit_root.glob("semantic_run_*"):
            if path == self.run_dir:
                continue
            receipt = _private_json_exact(path / "run-receipt.json")
            representation = receipt.get("representation")
            if (
                receipt.get("protocol_version")
                == EXTERNAL_AGENT_PROTOCOL_V3_4
                and isinstance(representation, dict)
                and representation.get("representation_id")
                == self.representation.representation_id
            ):
                raise SemanticHandoffError(
                    "Semantic recovery execution identity 已漂移。"
                )
            if (
                receipt.get("protocol_version")
                == EXTERNAL_AGENT_PROTOCOL_V3_3
                and isinstance(representation, dict)
                and representation.get("representation_id")
                == self.representation.representation_id
                and path.name != self.historical_v33_run_id
            ):
                raise SemanticHandoffError(
                    "历史 v3.3 recovery path binding 已漂移。"
                )
            if (
                receipt.get("protocol_version")
                == EXTERNAL_AGENT_PROTOCOL_V3_2
                and isinstance(representation, dict)
                and representation.get("representation_id")
                == self.representation.representation_id
                and path.name != self.historical_v32_run_id
            ):
                raise SemanticHandoffError(
                    "历史 v3.2 recovery path binding 已漂移。"
                )
            if (
                receipt.get("protocol_version")
                == EXTERNAL_AGENT_PROTOCOL_V3_1
                and isinstance(representation, dict)
                and representation.get("representation_id")
                == self.representation.representation_id
                and path.name != self.historical_v31_run_id
            ):
                raise SemanticHandoffError(
                    "历史 v3.1 recovery path binding 已漂移。"
                )

    def preflight(self) -> SemanticRecoveryPreflight:
        loaded, unknown = self.inspect()
        replayable = sum(item is not None for item in loaded)
        return SemanticRecoveryPreflight(
            total_batches=len(loaded),
            replayable_batches=replayable,
            required_new_calls=len(loaded) - replayable,
            conservatively_counted_attempts=unknown,
            historical_counted_attempts=(
                self._historical_v31_attempt_count()
                + self._historical_v32_attempt_count()
                + self._historical_v33_attempt_count()
            ),
        )

    def _historical_v31_attempt_count(self) -> int:
        return self._historical_attempt_count(
            run_id=self.historical_v31_run_id,
            contract_fingerprint=self.historical_v31_contract_fingerprint,
            expected_run_receipt=self.expected_historical_v31_run_receipt,
            batch_contracts=self.historical_v31_batch_contracts,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
        )

    def _historical_v32_attempt_count(self) -> int:
        return self._historical_attempt_count(
            run_id=self.historical_v32_run_id,
            contract_fingerprint=self.historical_v32_contract_fingerprint,
            expected_run_receipt=self.expected_historical_v32_run_receipt,
            batch_contracts=self.historical_v32_batch_contracts,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2,
        )

    def _historical_v33_attempt_count(self) -> int:
        return self._historical_attempt_count(
            run_id=self.historical_v33_run_id,
            contract_fingerprint=self.historical_v33_contract_fingerprint,
            expected_run_receipt=self.expected_historical_v33_run_receipt,
            batch_contracts=self.historical_v33_batch_contracts,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_3,
        )

    def _historical_attempt_count(
        self,
        *,
        run_id: str,
        contract_fingerprint: str,
        expected_run_receipt: Mapping[str, object],
        batch_contracts: tuple[dict[str, object], ...],
        protocol_version: str,
    ) -> int:
        if run_id == self.semantic_run_id:
            raise SemanticHandoffError("Semantic recovery protocol 路径未隔离。")
        legacy_run = self.audit_root / run_id
        if not os.path.lexists(legacy_run):
            return 0
        _validate_shared_recovery_root(self.audit_root, create=False)
        self._validate_historical_inventory(
            legacy_run,
            run_id=run_id,
            contract_fingerprint=contract_fingerprint,
            batch_contracts=batch_contracts,
            protocol_version=protocol_version,
        )
        if not _payloads_exactly_equal(
            _private_json_exact(legacy_run / "run-receipt.json"),
            expected_run_receipt,
        ):
            raise SemanticHandoffError("历史 Semantic recovery binding 损坏。")
        attempts_dir = legacy_run / "attempts"
        if not os.path.lexists(attempts_dir):
            return 0
        count = 0
        for path in sorted(attempts_dir.iterdir()):
            attempt = _private_json_exact(path)
            match = re.fullmatch(r"batch_(\d{4})\.json", path.name)
            ordinal = int(match.group(1)) if match else 0
            nonce = attempt.get("attempt_nonce")
            if (
                ordinal < 1
                or ordinal > len(batch_contracts)
                or not isinstance(nonce, str)
            ):
                raise SemanticHandoffError("历史 Semantic recovery attempt 损坏。")
            batch_receipt = batch_contracts[ordinal - 1]["receipt"]
            assert isinstance(batch_receipt, dict)
            if not _payloads_exactly_equal(
                attempt,
                self._expected_attempt_payload(
                    semantic_run_id=run_id,
                    contract_fingerprint=contract_fingerprint,
                    batch_receipt=batch_receipt,
                    ordinal=ordinal,
                    attempt_nonce=nonce,
                ),
            ):
                raise SemanticHandoffError("历史 Semantic recovery attempt 损坏。")
            count += 1
        return count

    def _validate_historical_inventory(
        self,
        legacy_run: Path,
        *,
        run_id: str,
        contract_fingerprint: str,
        batch_contracts: tuple[dict[str, object], ...],
        protocol_version: str,
    ) -> None:
        _require_private_directory(legacy_run)
        try:
            children = {path.name: path for path in legacy_run.iterdir()}
        except OSError as exc:
            raise SemanticHandoffError(
                "历史 v3.1 recovery inventory 不可读。"
            ) from exc
        if "run-receipt.json" not in children or not set(children) <= {
            "run-receipt.json",
            "attempts",
            "results",
        }:
            raise SemanticHandoffError("历史 v3.1 recovery inventory 损坏。")
        _require_private_file(children["run-receipt.json"])
        attempt_ordinals: set[int] = set()
        attempts = children.get("attempts")
        if attempts is not None:
            _require_private_directory(attempts)
            for path in attempts.iterdir():
                match = re.fullmatch(r"batch_(\d{4})\.json", path.name)
                ordinal = int(match.group(1)) if match else 0
                if (
                    ordinal < 1
                    or ordinal > len(batch_contracts)
                    or ordinal in attempt_ordinals
                ):
                    raise SemanticHandoffError(
                        "历史 v3.1 recovery attempt inventory 损坏。"
                    )
                _require_private_file(path)
                attempt_ordinals.add(ordinal)
        if attempt_ordinals and attempt_ordinals != set(
            range(1, max(attempt_ordinals) + 1)
        ):
            raise SemanticHandoffError(
                "历史 v3.1 recovery attempt inventory 顺序损坏。"
            )
        result_ordinals: set[int] = set()
        results = children.get("results")
        if results is not None:
            _require_private_directory(results)
            for path in results.iterdir():
                match = re.fullmatch(r"batch_(\d{4})", path.name)
                ordinal = int(match.group(1)) if match else 0
                if (
                    ordinal < 1
                    or ordinal > len(batch_contracts)
                    or ordinal in result_ordinals
                    or ordinal not in attempt_ordinals
                ):
                    raise SemanticHandoffError(
                        "历史 v3.1 recovery result inventory 损坏。"
                    )
                attempt = _private_json_exact(
                    attempts / f"batch_{ordinal:04d}.json"
                )
                self._validate_historical_result_inventory(
                    path,
                    ordinal,
                    attempt,
                    run_id=run_id,
                    contract_fingerprint=contract_fingerprint,
                    batch_contracts=batch_contracts,
                    protocol_version=protocol_version,
                )
                result_ordinals.add(ordinal)
        if result_ordinals and result_ordinals != set(
            range(1, max(result_ordinals) + 1)
        ):
            raise SemanticHandoffError(
                "历史 v3.1 recovery result inventory 顺序损坏。"
            )
        if attempt_ordinals:
            last_attempt = max(attempt_ordinals)
            completed = set(range(1, last_attempt + 1))
            final_attempt_unknown = set(range(1, last_attempt))
            if result_ordinals not in (completed, final_attempt_unknown):
                raise SemanticHandoffError(
                    "历史 v3.1 recovery batch 顺序因果损坏。"
                )

    def _validate_historical_result_inventory(
        self,
        path: Path,
        ordinal: int,
        attempt: Mapping[str, object],
        *,
        run_id: str,
        contract_fingerprint: str,
        batch_contracts: tuple[dict[str, object], ...],
        protocol_version: str,
    ) -> None:
        _require_private_directory(path)
        try:
            children = {child.name: child for child in path.iterdir()}
        except OSError as exc:
            raise SemanticHandoffError(
                "历史 v3.1 recovery result inventory 不可读。"
            ) from exc
        pending = {
            "result.json",
            "result-receipt.json",
            "phase-post-strict-pending.json",
        }
        if frozenset(children) not in {
            frozenset(pending),
            frozenset(pending | {"phase-committed.json"}),
        }:
            raise SemanticHandoffError(
                "历史 v3.1 recovery result inventory 损坏。"
            )
        for child in children.values():
            _require_private_file(child)
        raw = _private_bytes_read(path / "result.json")
        receipt = _private_json_exact(path / "result-receipt.json")
        batch_contract = batch_contracts[ordinal - 1]
        batch_receipt = batch_contract["receipt"]
        batch = batch_contract["batch"]
        assert isinstance(batch_receipt, dict)
        assert isinstance(batch, RepresentationAnalysisBatch)
        expected_keys = {
            "schema_version",
            "artifact_kind",
            "semantic_run_id",
            "run_contract_fingerprint",
            "batch_ordinal",
            "batch_contract_fingerprint",
            "attempt_id",
            "attempt_nonce",
            "attempt_receipt_fingerprint",
            "processing_run_id",
            "result_sha256",
            "result_size_bytes",
            "strict_validation_status",
            "result_readback_status",
            "process_cleanup_status",
            "execution_record",
            "result_receipt_fingerprint",
        }
        receipt_without_fingerprint = dict(receipt)
        receipt_fingerprint = receipt_without_fingerprint.pop(
            "result_receipt_fingerprint", None
        )
        if (
            set(receipt) != expected_keys
            or receipt.get("schema_version") != _RECOVERY_RESULT_SCHEMA
            or receipt.get("artifact_kind")
            != "semantic_handoff_batch_result"
            or receipt.get("semantic_run_id") != run_id
            or receipt.get("run_contract_fingerprint")
            != contract_fingerprint
            or isinstance(receipt.get("batch_ordinal"), bool)
            or receipt.get("batch_ordinal") != ordinal
            or receipt.get("batch_contract_fingerprint")
            != batch_receipt.get("batch_contract_fingerprint")
            or receipt.get("attempt_id") != attempt.get("attempt_id")
            or receipt.get("attempt_nonce") != attempt.get("attempt_nonce")
            or receipt.get("attempt_receipt_fingerprint")
            != attempt.get("attempt_receipt_fingerprint")
            or receipt.get("result_sha256") != _bytes_fingerprint(raw)
            or isinstance(receipt.get("result_size_bytes"), bool)
            or receipt.get("result_size_bytes") != len(raw)
            or receipt.get("strict_validation_status") != "passed"
            or receipt.get("result_readback_status") != "verified"
            or receipt.get("process_cleanup_status") != "verified"
            or receipt_fingerprint != _fingerprint(receipt_without_fingerprint)
        ):
            raise SemanticHandoffError(
                "历史 v3.1 recovery result binding 损坏。"
            )
        pending = self._expected_result_phase_payload(
            semantic_run_id=run_id,
            contract_fingerprint=contract_fingerprint,
            batch_receipt=batch_receipt,
            ordinal=ordinal,
            result_receipt=receipt,
            phase="post_strict_pending",
        )
        if not _payloads_exactly_equal(
            _private_json_exact(path / "phase-post-strict-pending.json"),
            pending,
        ):
            raise SemanticHandoffError(
                "历史 v3.1 recovery pending phase 损坏。"
            )
        if "phase-committed.json" in children and not _payloads_exactly_equal(
            _private_json_exact(path / "phase-committed.json"),
            self._expected_result_phase_payload(
                semantic_run_id=run_id,
                contract_fingerprint=contract_fingerprint,
                batch_receipt=batch_receipt,
                ordinal=ordinal,
                result_receipt=receipt,
                phase="committed",
            ),
        ):
            raise SemanticHandoffError(
                "历史 v3.1 recovery committed phase 损坏。"
            )
        record = _record_from_payload(receipt.get("execution_record"))
        if receipt.get("processing_run_id") != record.processing_run_id:
            raise SemanticHandoffError(
                "历史 v3.1 recovery Processing Run binding 损坏。"
            )
        self._reject_conflicting_failure_audit(record)
        self._validate_success_record_for_contract(
            record,
            ordinal,
            raw,
            protocol_version=protocol_version,
            batch_contracts=batch_contracts,
        )
        try:
            _parse_external_agent_result(
                raw.decode("utf-8"),
                batch,
                str(batch_receipt["input_fingerprint"]),
                protocol_version,
            )
        except (UnicodeDecodeError, RepresentationInformationError) as exc:
            raise SemanticHandoffError(
                "历史 v3.1 recovery result strict readback 失败。"
            ) from exc

    def _converge_result(
        self,
        ordinal: int,
        contract: Mapping[str, object],
        attempt: Mapping[str, object],
    ) -> tuple[RepresentationAnalysisResult, ExternalAgentExecutionRecord]:
        loaded = self._load_result(ordinal, contract, attempt)
        path = self._result_path(ordinal)
        committed_phase = path / "phase-committed.json"
        self._refsync_result_bundle(ordinal)
        loaded = self._load_result(ordinal, contract, attempt)
        if not os.path.lexists(committed_phase):
            receipt = _private_json_exact(path / "result-receipt.json")
            _publish_private_json_marker(
                committed_phase,
                self._result_phase_payload(ordinal, receipt, "committed"),
            )
        self._refsync_result_bundle(ordinal)
        final_loaded = self._load_result(ordinal, contract, attempt)
        if final_loaded != loaded:
            raise SemanticHandoffError(
                "Semantic recovery post-strict convergence 漂移。"
            )
        return final_loaded

    def _refsync_result_bundle(self, ordinal: int) -> None:
        path = self._result_path(ordinal)
        files = (
            self.run_dir / "run-receipt.json",
            self._attempt_path(ordinal),
            path / "result.json",
            path / "result-receipt.json",
            path / "phase-post-strict-pending.json",
            *(
                (path / "phase-committed.json",)
                if os.path.lexists(path / "phase-committed.json")
                else ()
            ),
        )
        _refsync_and_readback(
            files=files,
            directories=(
                path,
                self.results_dir,
                self.attempts_dir,
                self.run_dir,
                self.audit_root,
            ),
        )

    def _load_result(
        self,
        ordinal: int,
        contract: Mapping[str, object],
        attempt: Mapping[str, object],
    ) -> tuple[RepresentationAnalysisResult, ExternalAgentExecutionRecord]:
        path = self._result_path(ordinal)
        _require_private_directory(path)
        names = frozenset(child.name for child in path.iterdir())
        pending_names = {
            "result.json",
            "result-receipt.json",
            "phase-post-strict-pending.json",
        }
        if names not in {
            frozenset(pending_names),
            frozenset(pending_names | {"phase-committed.json"}),
        }:
            raise SemanticHandoffError("Semantic recovery batch result inventory 损坏。")
        raw = _private_bytes_read(path / "result.json")
        receipt = _private_json_exact(path / "result-receipt.json")
        batch_receipt = contract["receipt"]
        batch = contract["batch"]
        assert isinstance(batch_receipt, dict)
        assert isinstance(batch, RepresentationAnalysisBatch)
        expected_keys = {
            "schema_version",
            "artifact_kind",
            "semantic_run_id",
            "run_contract_fingerprint",
            "batch_ordinal",
            "batch_contract_fingerprint",
            "attempt_id",
            "attempt_nonce",
            "attempt_receipt_fingerprint",
            "processing_run_id",
            "result_sha256",
            "result_size_bytes",
            "strict_validation_status",
            "result_readback_status",
            "process_cleanup_status",
            "execution_record",
            "result_receipt_fingerprint",
        }
        receipt_without_fingerprint = dict(receipt)
        receipt_fingerprint = receipt_without_fingerprint.pop(
            "result_receipt_fingerprint", None
        )
        if (
            set(receipt) != expected_keys
            or receipt.get("schema_version") != _RECOVERY_RESULT_SCHEMA
            or receipt.get("artifact_kind") != "semantic_handoff_batch_result"
            or receipt.get("semantic_run_id") != self.semantic_run_id
            or receipt.get("run_contract_fingerprint")
            != self.contract_fingerprint
            or isinstance(receipt.get("batch_ordinal"), bool)
            or receipt.get("batch_ordinal") != ordinal
            or receipt.get("batch_contract_fingerprint")
            != batch_receipt.get("batch_contract_fingerprint")
            or receipt.get("attempt_id") != attempt["attempt_id"]
            or receipt.get("attempt_nonce") != attempt["attempt_nonce"]
            or receipt.get("attempt_receipt_fingerprint")
            != attempt["attempt_receipt_fingerprint"]
            or receipt.get("result_sha256") != _bytes_fingerprint(raw)
            or isinstance(receipt.get("result_size_bytes"), bool)
            or receipt.get("result_size_bytes") != len(raw)
            or receipt.get("strict_validation_status") != "passed"
            or receipt.get("result_readback_status") != "verified"
            or receipt.get("process_cleanup_status") != "verified"
            or receipt_fingerprint != _fingerprint(receipt_without_fingerprint)
        ):
            raise SemanticHandoffError("Semantic recovery batch result binding 损坏。")
        pending_phase = self._result_phase_payload(
            ordinal, receipt, "post_strict_pending"
        )
        if not _payloads_exactly_equal(
            _private_json_exact(path / "phase-post-strict-pending.json"),
            pending_phase,
        ):
            raise SemanticHandoffError("Semantic recovery pending phase 损坏。")
        if "phase-committed.json" in names and not _payloads_exactly_equal(
            _private_json_exact(path / "phase-committed.json"),
            self._result_phase_payload(ordinal, receipt, "committed"),
        ):
            raise SemanticHandoffError("Semantic recovery committed phase 损坏。")
        record = _record_from_payload(receipt.get("execution_record"))
        if receipt.get("processing_run_id") != record.processing_run_id:
            raise SemanticHandoffError("Semantic recovery Processing Run binding 损坏。")
        self._reject_conflicting_failure_audit(record)
        self._validate_success_record(record, ordinal, raw)
        try:
            parsed = _parse_external_agent_result(
                raw.decode("utf-8"),
                batch,
                str(batch_receipt["input_fingerprint"]),
            )
        except (UnicodeDecodeError, RepresentationInformationError) as exc:
            raise SemanticHandoffError(
                "Semantic recovery batch result strict readback 失败。"
            ) from exc
        return parsed, record

    def _reject_conflicting_failure_audit(
        self, record: ExternalAgentExecutionRecord
    ) -> None:
        path = (
            self.audit_root
            / record.processing_run_id
            / "processing-run-audit.json"
        )
        if not os.path.lexists(path):
            return
        audit = _private_json_exact(path)
        if (
            audit.get("processing_run_id") != record.processing_run_id
            or audit.get("execution_status") != "succeeded"
            or audit.get("failure_category") is not None
            or audit.get("strict_validation_status") != "passed"
            or audit.get("result_fingerprint") != record.result_fingerprint
        ):
            raise SemanticHandoffError(
                "Semantic recovery success bundle 与 failure audit 冲突。"
            )

    def _validate_success_record(
        self, record: ExternalAgentExecutionRecord, ordinal: int, raw: bytes
    ) -> None:
        self._validate_success_record_for_contract(
            record,
            ordinal,
            raw,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_VERSION,
            batch_contracts=self.batch_contracts,
        )

    def _validate_success_record_for_contract(
        self,
        record: ExternalAgentExecutionRecord,
        ordinal: int,
        raw: bytes,
        *,
        protocol_version: str,
        batch_contracts: tuple[dict[str, object], ...],
    ) -> None:
        receipt = batch_contracts[ordinal - 1]["receipt"]
        batch = batch_contracts[ordinal - 1]["batch"]
        assert isinstance(receipt, dict)
        assert isinstance(batch, RepresentationAnalysisBatch)
        zero_counts = (
            record.candidate_item_count,
            record.residue_item_count,
            record.accounting_item_count,
            record.candidate_anchor_ref_count,
            record.residue_anchor_ref_count,
            record.duplicate_anchor_ref_count,
            record.duplicate_accounting_count,
            record.dual_assignment_count,
            record.missing_anchor_count,
            record.unknown_anchor_ref_count,
        )
        expected_diagnostic_version = (
            DIAGNOSTIC_SCHEMA_VERSION
            if protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_4
            else DIAGNOSTIC_SCHEMA_V2
        )
        expected_grouping_counts = (0, 0, 0, 0)
        if protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_4:
            try:
                payload = json.loads(raw.decode("utf-8"))
                parsed = _parse_external_agent_result(
                    raw.decode("utf-8"),
                    batch,
                    str(receipt["input_fingerprint"]),
                    protocol_version,
                )
                anchor_results = payload["anchor_results"]
                assert isinstance(anchor_results, dict)
                raw_count = sum(
                    len(records)
                    for value in anchor_results.values()
                    if isinstance(value, dict)
                    and isinstance((records := value.get("records")), list)
                )
            except (
                AssertionError,
                KeyError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                RepresentationInformationError,
            ) as exc:
                raise SemanticHandoffError(
                    "Semantic recovery success diagnostics 不可重建。"
                ) from exc
            expected_grouping_counts = (
                raw_count,
                len(parsed.candidates) + len(parsed.residue),
                0,
                0,
            )
        if (
            not _processing_run_id(record.processing_run_id)
            or record.protocol_version != protocol_version
            or record.input_fingerprint != receipt["input_fingerprint"]
            or record.anchor_unit_ids
            != tuple(unit.unit_id for unit in batch.anchor_units)
            or record.provider_route != EXTERNAL_AGENT_ROUTE
            or record.provider_version != self.provider.provider_version
            or record.model != self.provider.model
            or record.reasoning_effort != self.provider.reasoning_effort
            or record.fallback_policy != self.provider.fallback_policy
            or record.execution_status != "succeeded"
            or record.failure_category is not None
            or record.contract_failure_detail is not None
            or record.strict_validation_status != "passed"
            or not _sha256_fingerprint(record.result_fingerprint)
            or record.result_fingerprint != _bytes_fingerprint(raw)
            or isinstance(record.eligible_units, bool)
            or not isinstance(record.eligible_units, int)
            or record.eligible_units != len(batch.anchor_units)
            or isinstance(record.covered_units, bool)
            or not isinstance(record.covered_units, int)
            or record.covered_units != len(batch.anchor_units)
            or record.contract_failure_stage is not None
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != 0
                for value in zero_counts
            )
            or (
                record.raw_record_count,
                record.projected_record_count,
                record.duplicate_exact_body_count,
                record.grouping_collision_count,
            )
            != expected_grouping_counts
            or record.diagnostic_schema_version != expected_diagnostic_version
            or not _timestamp(record.started_at)
            or not _timestamp(record.finished_at)
            or isinstance(record.elapsed_ms, bool)
            or not isinstance(record.elapsed_ms, int)
            or record.elapsed_ms < 0
            or isinstance(record.deadline_ms, bool)
            or not isinstance(record.deadline_ms, int)
            or record.deadline_ms != round(self.provider.timeout_seconds * 1000)
            or isinstance(record.exit_code, bool)
            or record.exit_code != 0
            or record.termination_signal is not None
            or record.timeout_phase is not None
            or record.provider_error_category is not None
            or record.result_file_present is not True
            or isinstance(record.result_size_bytes, bool)
            or not isinstance(record.result_size_bytes, int)
            or record.result_size_bytes != len(raw)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in (record.stdout_bytes, record.stderr_bytes)
            )
            or record.process_cleanup_status != "verified"
        ):
            raise SemanticHandoffError("Semantic recovery success receipt 不收敛。")

    def publish_result(
        self,
        ordinal: int,
        raw_result: _ExternalAgentSuccessfulResult,
        record: ExternalAgentExecutionRecord,
    ) -> tuple[RepresentationAnalysisResult, ExternalAgentExecutionRecord]:
        raw = raw_result.raw_bytes
        self._validate_success_record(record, ordinal, raw)
        if (
            raw_result.processing_run_id != record.processing_run_id
            or raw_result.input_fingerprint != record.input_fingerprint
        ):
            raise SemanticHandoffError("Semantic recovery raw result binding 损坏。")
        attempt = self._load_attempt(ordinal)
        receipt = self.batch_contracts[ordinal - 1]["receipt"]
        assert isinstance(receipt, dict)
        result_receipt_without_fingerprint: dict[str, object] = {
            "schema_version": _RECOVERY_RESULT_SCHEMA,
            "artifact_kind": "semantic_handoff_batch_result",
            "semantic_run_id": self.semantic_run_id,
            "run_contract_fingerprint": self.contract_fingerprint,
            "batch_ordinal": ordinal,
            "batch_contract_fingerprint": receipt[
                "batch_contract_fingerprint"
            ],
            "attempt_id": attempt["attempt_id"],
            "attempt_nonce": attempt["attempt_nonce"],
            "attempt_receipt_fingerprint": attempt[
                "attempt_receipt_fingerprint"
            ],
            "processing_run_id": record.processing_run_id,
            "result_sha256": _bytes_fingerprint(raw),
            "result_size_bytes": len(raw),
            "strict_validation_status": "passed",
            "result_readback_status": "verified",
            "process_cleanup_status": "verified",
            "execution_record": _record_payload(record),
        }
        result_receipt = {
            **result_receipt_without_fingerprint,
            "result_receipt_fingerprint": _fingerprint(
                result_receipt_without_fingerprint
            ),
        }
        pending_phase = self._result_phase_payload(
            ordinal, result_receipt, "post_strict_pending"
        )
        _ensure_private_directory(self.results_dir)
        final = self._result_path(ordinal)
        staging: Path | None = None
        published_here = False
        try:
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".batch_{ordinal:04d}.", dir=self.results_dir
                )
            )
            os.chmod(staging, 0o700)
            _write_staging_file(staging / "result.json", raw)
            _write_staging_file(
                staging / "result-receipt.json",
                _canonical_bytes(result_receipt) + b"\n",
            )
            _write_staging_file(
                staging / "phase-post-strict-pending.json",
                _canonical_bytes(pending_phase) + b"\n",
            )
            if (
                _private_bytes_read(staging / "result.json") != raw
                or not _payloads_exactly_equal(
                    _private_json_exact(staging / "result-receipt.json"),
                    result_receipt,
                )
                or not _payloads_exactly_equal(
                    _private_json_exact(
                        staging / "phase-post-strict-pending.json"
                    ),
                    pending_phase,
                )
            ):
                raise SemanticHandoffError(
                    "Semantic recovery batch staging readback 失败。"
                )
            _fsync_directory(staging)
            publish_directory_no_replace(staging, final)
            staging = None
            published_here = True
        except FileExistsError as exc:
            raise SemanticHandoffError(
                "Semantic recovery batch result collision；未覆盖。"
            ) from exc
        finally:
            if staging is not None and staging.exists():
                for child in staging.iterdir():
                    child.unlink()
                staging.rmdir()
        if not published_here:
            raise SemanticHandoffError(
                "Semantic recovery batch result 未发布。"
            )
        loaded = self._converge_result(
            ordinal,
            self.batch_contracts[ordinal - 1],
            attempt,
        )
        if loaded[1] != record:
            raise SemanticHandoffError("Semantic recovery batch result 读回不一致。")
        return loaded


def _analysis_results_fingerprint(
    outputs: tuple[RepresentationAnalysisResult, ...]
) -> str:
    return _fingerprint([asdict(output) for output in outputs])


class _RecoveryAwareProvider:
    """Feeds exact recovered batches into the unchanged complete package builder."""

    def __init__(
        self,
        provider: CodexCliRepresentationAnalysisProvider,
        recovery: _SemanticRecoveryRun,
        new_call_authority: int,
    ) -> None:
        if (
            isinstance(new_call_authority, bool)
            or not isinstance(new_call_authority, int)
            or new_call_authority < 0
        ):
            raise SemanticHandoffError("Semantic Provider call authority 无效。")
        self.provider = provider
        self.recovery = recovery
        self.new_call_authority = new_call_authority
        self.name = provider.name
        self.provider_version = provider.provider_version
        self.model = provider.model
        self.reasoning_effort = provider.reasoning_effort
        self.fallback_policy = provider.fallback_policy
        self.records: list[ExternalAgentExecutionRecord] = []
        self.new_calls = 0
        self._ordinal = 0

    @contextmanager
    def finalize_results(
        self, early_outputs: tuple[RepresentationAnalysisResult, ...]
    ):
        """Hold one exclusive run lock across final disk reload and package publish."""

        _require_private_directory(self.recovery.run_dir)
        descriptor = os.open(self.recovery.run_dir, os.O_RDONLY)
        try:
            observed = self.recovery.run_dir.lstat()
            opened = os.fstat(descriptor)
            if (
                observed.st_dev != opened.st_dev
                or observed.st_ino != opened.st_ino
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != os.getuid()
            ):
                raise SemanticHandoffError(
                    "Semantic recovery finalization lock binding 损坏。"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            outputs, records = self._final_disk_outputs(early_outputs)

            def verify_before_publish() -> None:
                verified_outputs, verified_records = self._final_disk_outputs(
                    outputs
                )
                if (
                    verified_outputs != outputs
                    or verified_records != records
                    or _analysis_results_fingerprint(verified_outputs)
                    != _analysis_results_fingerprint(outputs)
                ):
                    raise SemanticHandoffError(
                        "Semantic recovery package publish 前发生漂移。"
                    )

            finalization = _InternalAnalysisFinalization(
                outputs=outputs,
                verify_before_publish=verify_before_publish,
            )
            yield finalization
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _final_disk_outputs(
        self, expected_outputs: tuple[RepresentationAnalysisResult, ...]
    ) -> tuple[
        tuple[RepresentationAnalysisResult, ...],
        tuple[ExternalAgentExecutionRecord, ...],
    ]:
        if self._ordinal != len(self.recovery.batches):
            raise SemanticHandoffError(
                "Semantic recovery 未完成全部 canonical batches。"
            )
        loaded, unknown = self.recovery.inspect()
        if unknown or any(item is None for item in loaded):
            raise SemanticHandoffError(
                "Semantic recovery 最终全批次复核不完整。"
            )
        outputs = tuple(item[0] for item in loaded if item is not None)
        records = tuple(item[1] for item in loaded if item is not None)
        if (
            outputs != expected_outputs
            or _analysis_results_fingerprint(outputs)
            != _analysis_results_fingerprint(expected_outputs)
            or records != tuple(self.records)
        ):
            raise SemanticHandoffError(
                "Semantic recovery 最终磁盘结果与内存输出漂移。"
            )
        self.records = list(records)
        return outputs, records

    def analyze(
        self, batch: RepresentationAnalysisBatch
    ) -> RepresentationAnalysisResult:
        self._ordinal += 1
        if self._ordinal > len(self.recovery.batches):
            raise SemanticHandoffError("Semantic recovery batch 超出 canonical plan。")
        expected = self.recovery.batches[self._ordinal - 1]
        if batch != expected:
            raise SemanticHandoffError("Semantic recovery batch boundary 漂移。")
        loaded, unknown = self.recovery.inspect()
        if unknown:
            raise SemanticHandoffError(
                "Semantic recovery 存在 outcome 不确定的 attempt；LEAD_DECISION_REQUIRED。"
            )
        recovered = loaded[self._ordinal - 1]
        if recovered is not None:
            result, record = recovered
            self.records.append(record)
            return result
        if self.new_calls >= self.new_call_authority:
            raise SemanticHandoffError("Semantic Provider 剩余调用授权不足。")
        self.recovery.publish_attempt(self._ordinal)
        self.new_calls += 1
        record_offset = len(self.provider.execution_records)
        result_offset = len(self.provider._successful_results)
        self.provider._capture_successful_raw = True
        try:
            result = self.provider.analyze(batch)
        finally:
            self.provider._capture_successful_raw = False
        records = self.provider.execution_records[record_offset:]
        raw_results = self.provider._successful_results[result_offset:]
        if len(records) != 1 or len(raw_results) != 1:
            del self.provider._successful_results[result_offset:]
            raise SemanticHandoffError(
                "Semantic Provider success result 未能唯一绑定 Processing Run。"
            )
        raw_result = raw_results[0]
        del self.provider._successful_results[result_offset:]
        RepresentationInformationService._validate_batch_result(batch, result)
        loaded_result, record = self.recovery.publish_result(
            self._ordinal, raw_result, records[0]
        )
        if loaded_result != result:
            raise SemanticHandoffError("Semantic recovery strict result 读回漂移。")
        self.records.append(record)
        return loaded_result


_COMPLETED_AUDIT_BASE_FIELDS = {
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
    "contract_failure_detail",
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
_COMPLETED_AUDIT_CONTRACT_DIAGNOSTIC_FIELDS = {
    "contract_failure_stage",
    "candidate_item_count",
    "residue_item_count",
    "accounting_item_count",
    "candidate_anchor_ref_count",
    "residue_anchor_ref_count",
    "duplicate_anchor_ref_count",
    "duplicate_accounting_count",
    "dual_assignment_count",
    "missing_anchor_count",
    "unknown_anchor_ref_count",
}
_COMPLETED_AUDIT_GROUPING_DIAGNOSTIC_FIELDS = {
    "raw_record_count",
    "projected_record_count",
    "duplicate_exact_body_count",
    "grouping_collision_count",
}
_AUDIT_DIAGNOSTIC_FIELDS = {
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
_AUDIT_PROFILE_FIELDS = {
    "model",
    "reasoning_effort",
    "fallback_policy",
}
_COMPLETED_AUDIT_PROFILE_FIELDS = {
    "name",
    "provider_version",
    "model",
    "reasoning_effort",
    "fallback_policy",
}


def _unprofiled_v1_audit_shapes() -> frozenset[frozenset[str]]:
    current = _COMPLETED_AUDIT_BASE_FIELDS - _AUDIT_PROFILE_FIELDS
    return frozenset(
        {
            frozenset(current),
            frozenset(current - {"contract_failure_detail"}),
            frozenset(
                current
                - _AUDIT_DIAGNOSTIC_FIELDS
                - {"contract_failure_detail"}
            ),
        }
    )


def _versioned_audit_contract(
    protocol_version: str,
    package_provider: object,
    provider: CodexCliRepresentationAnalysisProvider,
) -> tuple[frozenset[frozenset[str]], str, bool, str]:
    if not isinstance(package_provider, dict) or frozenset(
        package_provider
    ) not in {
        frozenset({"name"}),
        frozenset(_COMPLETED_AUDIT_PROFILE_FIELDS),
    }:
        raise SemanticHandoffError(
            "已发布的信息包 Provider binding 不可读。"
        )
    profiled = set(package_provider) == _COMPLETED_AUDIT_PROFILE_FIELDS
    current_provider_version = getattr(provider, "provider_version", None)
    if (
        package_provider.get("name") != "external-agent-codex-cli"
        or getattr(provider, "name", None) != package_provider.get("name")
        or not isinstance(current_provider_version, str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            current_provider_version,
        )
        is None
    ):
        raise SemanticHandoffError(
            "已发布的信息包缺少唯一 Provider execution binding。"
        )
    if not profiled:
        if protocol_version != EXTERNAL_AGENT_PROTOCOL_V1:
            raise SemanticHandoffError(
                "已发布的信息包缺少该 protocol 要求的 Provider profile。"
            )
        return (
            _unprofiled_v1_audit_shapes(),
            DIAGNOSTIC_SCHEMA_V1,
            False,
            current_provider_version,
        )
    if (
        protocol_version
        in {
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            EXTERNAL_AGENT_PROTOCOL_V3_2,
            EXTERNAL_AGENT_PROTOCOL_V3_3,
            EXTERNAL_AGENT_PROTOCOL_V3_4,
        }
        and package_provider != _provider_manifest(provider)
    ):
        raise SemanticHandoffError(
            "已发布的信息包 Provider execution profile 已漂移。"
        )
    if (
        any(
            not isinstance(package_provider.get(field), str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                str(package_provider.get(field)),
            )
            is None
            for field in ("provider_version", "model")
        )
        or package_provider.get("reasoning_effort")
        not in {"low", "medium", "high", "xhigh"}
        or package_provider.get("fallback_policy") != "none"
    ):
        raise SemanticHandoffError(
            "已发布的信息包缺少唯一 Provider execution profile。"
        )
    package_provider_version = package_provider["provider_version"]
    assert isinstance(package_provider_version, str)
    if protocol_version in {
        EXTERNAL_AGENT_PROTOCOL_V1,
        EXTERNAL_AGENT_PROTOCOL_V2,
        EXTERNAL_AGENT_PROTOCOL_V3,
    }:
        shapes = frozenset({frozenset(_COMPLETED_AUDIT_BASE_FIELDS)})
        diagnostic_version = DIAGNOSTIC_SCHEMA_V1
    elif protocol_version in {
        EXTERNAL_AGENT_PROTOCOL_V3_1,
        EXTERNAL_AGENT_PROTOCOL_V3_2,
        EXTERNAL_AGENT_PROTOCOL_V3_3,
    }:
        shapes = frozenset(
            {
                frozenset(
                    _COMPLETED_AUDIT_BASE_FIELDS
                    | _COMPLETED_AUDIT_CONTRACT_DIAGNOSTIC_FIELDS
                )
            }
        )
        diagnostic_version = DIAGNOSTIC_SCHEMA_V2
    elif protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_4:
        shapes = frozenset(
            {
                frozenset(
                    _COMPLETED_AUDIT_BASE_FIELDS
                    | _COMPLETED_AUDIT_CONTRACT_DIAGNOSTIC_FIELDS
                    | _COMPLETED_AUDIT_GROUPING_DIAGNOSTIC_FIELDS
                )
            }
        )
        diagnostic_version = DIAGNOSTIC_SCHEMA_VERSION
    else:
        raise SemanticHandoffError(
            "已发布的信息包使用不受支持的 External Agent protocol。"
        )
    return shapes, diagnostic_version, True, package_provider_version


def _validate_versioned_published_audits(
    *,
    paths: tuple[Path, ...],
    expected: dict[tuple[str, ...], str],
    protocol_version: str,
    package_provider: object,
    provider: CodexCliRepresentationAnalysisProvider,
    package_fingerprint: str,
    completed_only: bool,
) -> None:
    if protocol_version not in SUPPORTED_EXTERNAL_AGENT_PROTOCOL_VERSIONS:
        raise SemanticHandoffError(
            "已发布的信息包使用不受支持的 External Agent protocol。"
        )
    (
        expected_shapes,
        expected_diagnostic_version,
        profiled,
        expected_provider_version,
    ) = _versioned_audit_contract(
        protocol_version, package_provider, provider
    )
    if len(paths) != len(expected):
        raise SemanticHandoffError("已发布的信息包审计集合不完整。")
    observed: set[tuple[str, ...]] = set()
    for path in paths:
        audit = _private_json_read(path)
        if frozenset(audit) not in expected_shapes:
            raise SemanticHandoffError("已发布的信息包审计字段不精确。")
        has_diagnostics = _AUDIT_DIAGNOSTIC_FIELDS.issubset(audit)
        has_contract_diagnostics = (
            _COMPLETED_AUDIT_CONTRACT_DIAGNOSTIC_FIELDS.issubset(audit)
        )
        processing_run_id = audit.get("processing_run_id")
        anchor_unit_ids = audit.get("anchor_unit_ids")
        if (
            not _processing_run_id(processing_run_id)
            or path.parent.name != processing_run_id
            or not isinstance(anchor_unit_ids, list)
            or not anchor_unit_ids
            or any(not isinstance(item, str) for item in anchor_unit_ids)
        ):
            raise SemanticHandoffError("已发布的信息包审计 identity 损坏。")
        batch = tuple(anchor_unit_ids)
        expected_fingerprint = expected.get(batch)
        if expected_fingerprint is None or batch in observed:
            raise SemanticHandoffError("已发布的信息包审计批次不收敛。")
        observed.add(batch)
        if completed_only:
            valid_completion_state = (
                audit.get("information_ingested") is True
                and audit.get("durable_ingestion_status") == "completed"
                and audit.get("handoff_status") == "completed"
                and audit.get("audit_readback_status") == "verified"
            )
        else:
            durable_status = audit.get("durable_ingestion_status")
            readback_status = audit.get("audit_readback_status")
            valid_completion_state = (
                durable_status == "pending"
                and audit.get("information_ingested") is False
                and readback_status == "verified"
                and audit.get("handoff_status") == "pending"
            ) or (
                durable_status == "write_attempt_started"
                and audit.get("information_ingested") is False
                and readback_status == "verified"
                and audit.get("handoff_status") == "pending_durable_write"
            ) or (
                durable_status == "written_readback_pending"
                and audit.get("information_ingested") is True
                and readback_status in {"pending", "verified"}
                and audit.get("handoff_status") == "pending_readback"
            ) or (
                durable_status == "completed"
                and audit.get("information_ingested") is True
                and readback_status in {"pending", "verified"}
                and audit.get("handoff_status") == "completed"
            )
        if (
            audit.get("schema_version") != "processing-run-audit/1.0"
            or audit.get("artifact_kind") != "processing_run_audit"
            or audit.get("protocol_version") != protocol_version
            or audit.get("input_fingerprint") != expected_fingerprint
            or audit.get("provider_route") != EXTERNAL_AGENT_ROUTE
            or audit.get("provider_version") != expected_provider_version
            or profiled
            and any(
                audit.get(field) != package_provider.get(field)
                for field in _AUDIT_PROFILE_FIELDS
            )
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
                != expected_diagnostic_version
                or isinstance(audit.get("elapsed_ms"), bool)
                or not isinstance(audit.get("elapsed_ms"), int)
                or int(audit["elapsed_ms"]) < 0
                or isinstance(audit.get("deadline_ms"), bool)
                or not isinstance(audit.get("deadline_ms"), int)
                or int(audit["deadline_ms"]) <= 0
                or audit.get("exit_code") != 0
                or audit.get("termination_signal") is not None
                or audit.get("timeout_phase") is not None
                or audit.get("provider_error_category") is not None
                or audit.get("result_file_present") is not True
                or isinstance(audit.get("result_size_bytes"), bool)
                or not isinstance(audit.get("result_size_bytes"), int)
                or int(audit["result_size_bytes"]) <= 0
                or any(
                    isinstance(audit.get(field), bool)
                    or not isinstance(audit.get(field), int)
                    or int(audit[field]) < 0
                    for field in ("stdout_bytes", "stderr_bytes")
                )
                or audit.get("process_cleanup_status") != "verified"
            )
            or audit.get("result_readback_status") != "verified"
            or audit.get("package_published") is not True
            or audit.get("package_fingerprint") != package_fingerprint
            or not valid_completion_state
        ):
            raise SemanticHandoffError(
                "已发布的信息包审计未严格完成或 execution binding 漂移。"
            )
        if has_contract_diagnostics and (
            audit.get("contract_failure_stage") is not None
            or any(
                audit.get(field) != 0
                for field in _COMPLETED_AUDIT_CONTRACT_DIAGNOSTIC_FIELDS
                if field != "contract_failure_stage"
            )
        ):
            raise SemanticHandoffError(
                "已发布的信息包 contract diagnostics 不收敛。"
            )
        if protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_4 and (
            isinstance(audit.get("raw_record_count"), bool)
            or not isinstance(audit.get("raw_record_count"), int)
            or isinstance(audit.get("projected_record_count"), bool)
            or not isinstance(audit.get("projected_record_count"), int)
            or int(audit["raw_record_count"])
            < int(audit["projected_record_count"])
            or int(audit["projected_record_count"]) < 1
            or audit.get("duplicate_exact_body_count") != 0
            or audit.get("grouping_collision_count") != 0
        ):
            raise SemanticHandoffError(
                "已发布的信息包 record grouping diagnostics 不收敛。"
            )
    if observed != set(expected):
        raise SemanticHandoffError("已发布的信息包审计批次集合不完整。")


def validate_completed_published_audits(
    *,
    representation_service: RepresentationInformationService,
    representation_id: str,
    manifest: dict[str, object],
    audit_root: Path,
    package_fingerprint: str,
    provider: CodexCliRepresentationAnalysisProvider,
) -> tuple[Path, ...]:
    """Read-only exact validation for completed historical External Agent runs."""
    paths: list[Path] = []
    payloads: list[dict[str, object]] = []
    if Path(audit_root).is_dir():
        for path in sorted(
            Path(audit_root).glob("*/processing-run-audit.json")
        ):
            try:
                payload = _private_json_read(path)
            except (OSError, json.JSONDecodeError, SemanticHandoffError):
                continue
            if payload.get("package_fingerprint") == package_fingerprint:
                paths.append(path)
                payloads.append(payload)
    protocols = {payload.get("protocol_version") for payload in payloads}
    if len(protocols) != 1:
        raise SemanticHandoffError(
            "已发布的信息包审计缺少唯一 External Agent protocol。"
        )
    protocol_version = protocols.pop()
    if not isinstance(protocol_version, str):
        raise SemanticHandoffError(
            "已发布的信息包审计缺少唯一 External Agent protocol。"
        )
    manifest_batches = manifest.get("batches")
    if not isinstance(manifest_batches, list) or not manifest_batches:
        raise SemanticHandoffError("已发布的信息包批次清单不可读。")
    batch_unit_ids: list[tuple[str, ...]] = []
    for package_batch in manifest_batches:
        if not isinstance(package_batch, dict):
            raise SemanticHandoffError("已发布的信息包批次清单不可读。")
        unit_ids = package_batch.get("unit_ids")
        if (
            not isinstance(unit_ids, list)
            or not unit_ids
            or any(not isinstance(item, str) for item in unit_ids)
        ):
            raise SemanticHandoffError("已发布的信息包批次清单不可读。")
        batch_unit_ids.append(tuple(unit_ids))
    representation = representation_service.representation_repository.get(
        representation_id
    )
    try:
        batches = _analysis_batches_for_anchor_unit_ids(
            _units_from_representation(
                representation,
                representation_service.representation_repository,
            ),
            batch_unit_ids,
        )
    except RepresentationInformationError as exc:
        raise SemanticHandoffError(
            "已发布的信息包批次不再匹配 canonical Analysis Units。"
        ) from exc
    expected: dict[tuple[str, ...], str] = {}
    for batch_unit_id, batch in zip(batch_unit_ids, batches, strict=True):
        _, fingerprint = _external_agent_request(
            batch, protocol_version=protocol_version
        )
        if batch_unit_id in expected:
            raise SemanticHandoffError("已发布的信息包批次 identity 冲突。")
        expected[batch_unit_id] = fingerprint
    _validate_versioned_published_audits(
        paths=tuple(paths),
        expected=expected,
        protocol_version=protocol_version,
        package_provider=manifest.get("provider"),
        provider=provider,
        package_fingerprint=package_fingerprint,
        completed_only=True,
    )
    return tuple(paths)


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
        self.audit_root = Path(
            os.path.abspath(Path(audit_root).expanduser())
        )

    def execute(
        self,
        representation_id: str,
        provider: CodexCliRepresentationAnalysisProvider,
        *,
        privacy_binding: SemanticPrivacyBinding | None = None,
        new_call_authority: int | None = None,
    ) -> SemanticHandoffResult:
        record_offset = len(provider.execution_records)
        package = self.representation_service.output_root / representation_id
        if os.path.lexists(package):
            if privacy_binding is not None:
                recovery = _SemanticRecoveryRun(
                    self.representation_service,
                    self.audit_root,
                    representation_id,
                    provider,
                    privacy_binding,
                )
                if recovery.exists:
                    return self._resume_recovery_package(
                        representation_id,
                        package,
                        provider,
                        recovery,
                    )
            try:
                manifest = self._verify_replay_input(representation_id, package)
                package_fingerprint = _package_fingerprint(package)
                audit_paths = self._matching_audit_paths(package_fingerprint)
                protocol_version = self._replay_protocol_version(audit_paths)
                expected_batches = self._expected_batch_contracts(
                    representation_id,
                    manifest,
                    provider,
                    protocol_version=protocol_version,
                )
                self._validate_replay_audits(
                    audit_paths,
                    expected_batches,
                    manifest,
                    provider,
                    package_fingerprint,
                    protocol_version=protocol_version,
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

        recovery: _SemanticRecoveryRun | None = None
        analysis_provider: object = provider
        if privacy_binding is not None:
            if (
                isinstance(new_call_authority, bool)
                or not isinstance(new_call_authority, int)
                or new_call_authority < 0
            ):
                raise SemanticHandoffError(
                    "Semantic recovery 缺少显式 Provider call authority。"
                )
            recovery = _SemanticRecoveryRun(
                self.representation_service,
                self.audit_root,
                representation_id,
                provider,
                privacy_binding,
            )
            preflight = recovery.preflight()
            if preflight.conservatively_counted_attempts:
                raise SemanticHandoffError(
                    "Semantic recovery 存在 outcome 不确定的 attempt；LEAD_DECISION_REQUIRED。"
                )
            if preflight.required_new_calls > new_call_authority:
                raise SemanticHandoffError("Semantic Provider 剩余调用授权不足。")
            recovery.ensure_run_receipt()
            analysis_provider = _RecoveryAwareProvider(
                provider, recovery, new_call_authority
            )

        audit_paths: tuple[Path, ...] = ()
        ingestion: IngestionResult | None = None
        try:
            if isinstance(analysis_provider, _RecoveryAwareProvider):
                package = self.representation_service._extract_with_internal_finalization(
                    representation_id,
                    analysis_provider,
                    analysis_provider.finalize_results,
                )
            else:
                package = self.representation_service.extract(
                    representation_id,
                    analysis_provider,  # type: ignore[arg-type]
                )
            manifest = self._verify_replay_input(representation_id, package)
            package_fingerprint = _package_fingerprint(package)
            records = (
                analysis_provider.records  # type: ignore[attr-defined]
                if recovery is not None
                else provider.execution_records[record_offset:]
            )
            expected_batches = self._expected_batch_contracts(
                representation_id,
                manifest,
                provider,
                protocol_version=EXTERNAL_AGENT_PROTOCOL_VERSION,
            )
            self._validate_execution_records(records, expected_batches, provider)
            audit_paths = self._persist_audits(
                records,
                package_published=True,
                information_ingested=False,
                durable_ingestion_status="pending",
                package_fingerprint=package_fingerprint,
                handoff_status="pending",
                no_replace=recovery is not None,
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
            failed_records = provider.execution_records[record_offset:]
            if recovery is not None:
                failed_records = [
                    record
                    for record in failed_records
                    if record.execution_status != "succeeded"
                ]
            audit_paths = self._persist_audits(
                failed_records,
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

    def recovery_preflight(
        self,
        representation_id: str,
        provider: CodexCliRepresentationAnalysisProvider,
        privacy_binding: SemanticPrivacyBinding,
    ) -> SemanticRecoveryPreflight:
        """Zero-Provider local convergence; historical audits are never results."""

        return _SemanticRecoveryRun(
            self.representation_service,
            self.audit_root,
            representation_id,
            provider,
            privacy_binding,
        ).preflight()

    def _resume_recovery_package(
        self,
        representation_id: str,
        package: Path,
        provider: CodexCliRepresentationAnalysisProvider,
        recovery: _SemanticRecoveryRun,
    ) -> SemanticHandoffResult:
        try:
            loaded, unknown = recovery.inspect()
            if unknown or any(item is None for item in loaded):
                raise SemanticHandoffError(
                    "Partial Semantic recovery 不得作为完整 package authority。"
                )
            records = [item[1] for item in loaded if item is not None]
            manifest = self._verify_replay_input(representation_id, package)
            package_fingerprint = _package_fingerprint(package)
            expected_batches = self._expected_batch_contracts(
                representation_id,
                manifest,
                provider,
                protocol_version=EXTERNAL_AGENT_PROTOCOL_VERSION,
            )
            self._validate_execution_records(records, expected_batches, provider)
            audit_paths = self._matching_audit_paths(package_fingerprint)
            if audit_paths:
                self._validate_replay_audits(
                    audit_paths,
                    expected_batches,
                    manifest,
                    provider,
                    package_fingerprint,
                    protocol_version=EXTERNAL_AGENT_PROTOCOL_VERSION,
                )
            else:
                audit_paths = self._persist_audits(
                    records,
                    package_published=True,
                    information_ingested=False,
                    durable_ingestion_status="pending",
                    package_fingerprint=package_fingerprint,
                    handoff_status="pending",
                    no_replace=True,
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
                "完整 Semantic recovery package 未能安全收敛；未执行 Provider。"
            ) from exc
        return SemanticHandoffResult(package, ingestion, audit_paths, True)

    def _persist_audits(
        self,
        records: list[ExternalAgentExecutionRecord],
        *,
        package_published: bool,
        information_ingested: bool,
        durable_ingestion_status: str,
        package_fingerprint: str | None,
        handoff_status: str,
        no_replace: bool = False,
    ) -> tuple[Path, ...]:
        if not os.path.lexists(self.audit_root):
            _validate_shared_recovery_root(self.audit_root, create=True)
        paths: list[Path] = []
        for record in records:
            contract_counts = (
                record.candidate_item_count,
                record.residue_item_count,
                record.accounting_item_count,
                record.candidate_anchor_ref_count,
                record.residue_anchor_ref_count,
                record.duplicate_anchor_ref_count,
                record.duplicate_accounting_count,
                record.dual_assignment_count,
                record.missing_anchor_count,
                record.unknown_anchor_ref_count,
            )
            grouping_counts = (
                record.raw_record_count,
                record.projected_record_count,
                record.duplicate_exact_body_count,
                record.grouping_collision_count,
            )
            if (
                record.failure_category == "result_contract_failure"
                and (
                    record.contract_failure_detail not in CONTRACT_FAILURE_DETAILS
                    or record.contract_failure_stage is None
                    or not _sha256_fingerprint(record.result_fingerprint)
                )
            ) or (
                record.failure_category != "result_contract_failure"
                and (
                    record.contract_failure_detail is not None
                    or record.contract_failure_stage is not None
                    or any(contract_counts)
                    or record.execution_status != "succeeded"
                    and any(grouping_counts)
                )
            ) or (
                any(isinstance(value, bool) or value < 0 for value in contract_counts)
                or any(
                    isinstance(value, bool) or value < 0
                    for value in grouping_counts
                )
                or record.covered_units < 0
                or record.covered_units > record.eligible_units
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
                "contract_failure_stage": record.contract_failure_stage,
                "candidate_item_count": record.candidate_item_count,
                "residue_item_count": record.residue_item_count,
                "accounting_item_count": record.accounting_item_count,
                "candidate_anchor_ref_count": record.candidate_anchor_ref_count,
                "residue_anchor_ref_count": record.residue_anchor_ref_count,
                "duplicate_anchor_ref_count": record.duplicate_anchor_ref_count,
                "duplicate_accounting_count": record.duplicate_accounting_count,
                "dual_assignment_count": record.dual_assignment_count,
                "missing_anchor_count": record.missing_anchor_count,
                "unknown_anchor_ref_count": record.unknown_anchor_ref_count,
                "raw_record_count": record.raw_record_count,
                "projected_record_count": record.projected_record_count,
                "duplicate_exact_body_count": record.duplicate_exact_body_count,
                "grouping_collision_count": record.grouping_collision_count,
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
            if no_replace:
                _private_json_write_no_replace(path, payload)
            else:
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

    @staticmethod
    def _replay_protocol_version(paths: tuple[Path, ...]) -> str:
        versions = {
            _private_json_read(path).get("protocol_version") for path in paths
        }
        if len(versions) != 1:
            raise SemanticHandoffError(
                "已存在的信息包审计缺少唯一 External Agent protocol。"
            )
        protocol_version = versions.pop()
        if protocol_version not in SUPPORTED_EXTERNAL_AGENT_PROTOCOL_VERSIONS:
            raise SemanticHandoffError(
                "已存在的信息包使用不受支持的 External Agent protocol。"
            )
        assert isinstance(protocol_version, str)
        return protocol_version

    def _expected_batch_contracts(
        self,
        representation_id: str,
        manifest: dict[str, object],
        provider: CodexCliRepresentationAnalysisProvider,
        *,
        protocol_version: str,
    ) -> tuple[tuple[tuple[str, ...], str], ...]:
        package_provider = manifest.get("provider")
        _versioned_audit_contract(
            protocol_version,
            package_provider,
            provider,
        )
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
            _, fingerprint = _external_agent_request(
                batch, protocol_version=protocol_version
            )
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
                or record.diagnostic_schema_version != DIAGNOSTIC_SCHEMA_VERSION
                or record.raw_record_count < record.projected_record_count
                or record.projected_record_count < 1
                or record.duplicate_exact_body_count != 0
                or record.grouping_collision_count != 0
                or record.contract_failure_stage is not None
                or any(
                    (
                        record.candidate_item_count,
                        record.residue_item_count,
                        record.accounting_item_count,
                        record.candidate_anchor_ref_count,
                        record.residue_anchor_ref_count,
                        record.duplicate_anchor_ref_count,
                        record.duplicate_accounting_count,
                        record.dual_assignment_count,
                        record.missing_anchor_count,
                        record.unknown_anchor_ref_count,
                    )
                )
            ):
                raise SemanticHandoffError("当前信息包 Processing Run 审计不匹配。")

    def _validate_replay_audits(
        self,
        paths: tuple[Path, ...],
        expected_batches: tuple[tuple[tuple[str, ...], str], ...],
        manifest: dict[str, object],
        provider: CodexCliRepresentationAnalysisProvider,
        package_fingerprint: str,
        *,
        protocol_version: str,
    ) -> None:
        _validate_versioned_published_audits(
            paths=paths,
            expected=dict(expected_batches),
            protocol_version=protocol_version,
            package_provider=manifest.get("provider"),
            provider=provider,
            package_fingerprint=package_fingerprint,
            completed_only=False,
        )

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


def _private_json_write_no_replace(
    path: Path, payload: dict[str, object]
) -> None:
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
            target.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            )
            target.flush()
            os.fsync(target.fileno())
        publish_file_no_replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
        if _private_json_read(path) != payload:
            raise SemanticHandoffError("Processing Run 审计 no-replace 读回失败。")
    except FileExistsError as exc:
        raise SemanticHandoffError("Processing Run 审计 collision；未覆盖。") from exc
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
