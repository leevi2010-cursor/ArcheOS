from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..analysis import SEMANTIC_TYPES
from ..source.identity import require_managed_source_id
from ..representation_information import (
    PACKAGE_SCHEMA_VERSION,
    validate_representation_information_package,
)
from .models import (
    EvidenceRecord,
    IngestionResult,
    AtomicInformationRevision,
    evidence_from_dict,
    evidence_to_dict,
)
from .store import AtomicInformationStore


def _non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _manifest(package: Path) -> tuple[str, str]:
    manifest_path = package / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"processing package manifest not found: {manifest_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid processing manifest: {exc.msg}") from exc
    if not isinstance(manifest, dict):
        raise TypeError("processing manifest must be an object")
    schema_version = manifest.get("schema_version")
    if schema_version == PACKAGE_SCHEMA_VERSION:
        validated, _ = validate_representation_information_package(package)
        source = validated["source"]
        assert isinstance(source, dict)  # validated by the strict package reader
        return schema_version, require_managed_source_id(
            source["id"], field="manifest.source.id"
        )
    if schema_version not in {"1.0", "1.1", "1.2"}:
        raise ValueError("unsupported processing schema version")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise TypeError("processing manifest source must be an object")
    source_id = _non_empty(source.get("id"), "manifest.source.id")
    if schema_version == "1.2":
        source_id = require_managed_source_id(
            source_id, field="manifest.source.id"
        )
        content_hash = _non_empty(
            source.get("content_hash"), "manifest.source.content_hash"
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash):
            raise ValueError("manifest.source.content_hash must be a full sha256 hash")
        size_bytes = source.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("manifest.source.size_bytes must be a non-negative integer")
        _non_empty(source.get("media_type"), "manifest.source.media_type")
        _non_empty(source.get("filename_hint"), "manifest.source.filename_hint")
        if "path" in source or "ingested_from" in source:
            raise ValueError("Managed Source processing metadata must not contain paths")
    return schema_version, source_id


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    return tuple(_non_empty(item, field) for item in value)


def _evidence(value: object, field: str, source_id: str) -> tuple[EvidenceRecord, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    result = tuple(
        evidence_from_dict(item, f"{field}[{index}]")
        for index, item in enumerate(value, start=1)
    )
    if any(item.source_id != source_id for item in result):
        raise ValueError(f"{field} source_id does not match the processing package")
    return result


def _fingerprint(
    *,
    statement: str,
    semantic_type: str,
    concerns: tuple[str, ...],
    evidence: tuple[EvidenceRecord, ...],
    context: str,
    confidence: float,
) -> str:
    payload = {
        "statement": statement,
        "semantic_type": semantic_type,
        "concerns": list(concerns),
        "source_evidence": [evidence_to_dict(item) for item in evidence],
        "context": context,
        "confidence": confidence,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _atomic_information_id(source_id: str, candidate_id: str) -> str:
    origin = f"{source_id}\0{candidate_id}".encode()
    return f"atomic_info_{hashlib.sha256(origin).hexdigest()[:32]}"


def _parse_candidate(
    payload: object,
    *,
    source_id: str,
    artifact_name: str,
    expected_status: str,
    line_number: int,
) -> AtomicInformationRevision:
    field = f"{artifact_name} line {line_number}"
    if not isinstance(payload, dict):
        raise TypeError(f"{field} must be an object")
    expected = {
        "id",
        "statement",
        "semantic_type",
        "concerns",
        "source_evidence",
        "context",
        "confidence",
        "processing_time",
        "status",
    }
    if set(payload) != expected:
        raise ValueError(f"{field} does not match the M1 candidate contract")
    candidate_id = _non_empty(payload["id"], f"{field}.id")
    statement = _non_empty(payload["statement"], f"{field}.statement")
    semantic_type = _non_empty(payload["semantic_type"], f"{field}.semantic_type")
    if semantic_type not in SEMANTIC_TYPES:
        raise ValueError(f"{field}.semantic_type is not supported")
    concerns = _strings(payload["concerns"], f"{field}.concerns")
    evidence = _evidence(
        payload["source_evidence"], f"{field}.source_evidence", source_id
    )
    context = _non_empty(payload["context"], f"{field}.context")
    confidence = payload["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise ValueError(f"{field}.confidence must be between 0 and 1")
    status = payload["status"]
    if status != expected_status:
        raise ValueError(f"{field}.status must be {expected_status}")
    created_at = _non_empty(payload["processing_time"], f"{field}.processing_time")
    atomic_information_id = _atomic_information_id(source_id, candidate_id)
    return AtomicInformationRevision(
        atomic_information_id=atomic_information_id,
        revision_number=1,
        revision_id=f"{atomic_information_id}-r0001",
        origin_source_id=source_id,
        origin_candidate_id=candidate_id,
        origin_fingerprint=_fingerprint(
            statement=statement,
            semantic_type=semantic_type,
            concerns=concerns,
            evidence=evidence,
            context=context,
            confidence=float(confidence),
        ),
        statement=statement,
        semantic_type=semantic_type,
        raw_concerns=concerns,
        related_object_ids=(),
        source_evidence=evidence,
        context=context,
        confidence=float(confidence),
        created_at=created_at,
        revision_reason="automatic_ingestion",
    )


def _load_candidates(
    package: Path,
    schema_version: str,
    source_id: str,
) -> tuple[AtomicInformationRevision, ...]:
    if schema_version == "1.0":
        artifact_name = "atomic_notes.jsonl"
        expected_status = "proposed"
    else:
        artifact_name = "atomic_information_candidates.jsonl"
        expected_status = "candidate"
    path = package / artifact_name
    try:
        source = path.open(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(
            f"Atomic Information Candidate artifact not found: {path}"
        ) from exc
    revisions: list[AtomicInformationRevision] = []
    with source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError(f"{artifact_name} line {line_number} is blank")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid {artifact_name} line {line_number}: {exc.msg}"
                ) from exc
            revisions.append(
                _parse_candidate(
                    payload,
                    source_id=source_id,
                    artifact_name=artifact_name,
                    expected_status=expected_status,
                    line_number=line_number,
                )
            )
    return tuple(revisions)


def ingest_processing_package(
    package: Path,
    store: AtomicInformationStore,
) -> IngestionResult:
    package = Path(package).expanduser().resolve()
    if not package.is_dir():
        raise ValueError(f"processing package not found: {package}")
    schema_version, source_id = _manifest(package)
    revisions = _load_candidates(package, schema_version, source_id)
    return store.ingest_batch(revisions)
