from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..analysis import SEMANTIC_TYPES


@dataclass(frozen=True)
class EvidenceRecord:
    source_id: str
    artifact: str
    segment: int
    speaker: str | None
    start: str | None
    end: str | None
    excerpt: str
    representation_id: str | None = None
    representation_kind: str | None = None
    artifact_id: str | None = None
    unit_id: str | None = None
    locator: str | None = None


CLAIM_STANCES = frozenset({"assert", "deny", "uncertain"})


@dataclass(frozen=True)
class ClaimAttribution:
    claimant_object_id: str | None
    claimant_source_id: str
    claimant_label: str | None
    stance: str
    claimed_at: str | None
    attribution_confidence: float | None


@dataclass(frozen=True)
class AtomicInformationRevision:
    atomic_information_id: str
    revision_number: int
    revision_id: str
    origin_source_id: str
    origin_candidate_id: str
    origin_fingerprint: str
    statement: str
    semantic_type: str
    raw_concerns: tuple[str, ...]
    related_object_ids: tuple[str, ...]
    source_evidence: tuple[EvidenceRecord, ...]
    context: str
    confidence: float
    created_at: str
    revision_reason: str
    claim: ClaimAttribution | None = None


@dataclass(frozen=True)
class IngestionResult:
    created: int
    existing: int
    failed: int
    atomic_information_ids: tuple[str, ...]


def _non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _non_empty(value, field)


def _strings(value: object, field: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    result = tuple(_non_empty(item, field) for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{field} must not be empty")
    return result


def evidence_from_dict(value: object, field: str) -> EvidenceRecord:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    expected = {
        "source_id",
        "artifact",
        "segment",
        "speaker",
        "start",
        "end",
        "excerpt",
    }
    representation_expected = expected | {
        "representation_id",
        "representation_kind",
        "artifact_id",
        "unit_id",
        "locator",
    }
    if set(value) != expected and set(value) != representation_expected:
        raise ValueError(f"{field} does not match the Evidence schema")
    segment = value["segment"]
    if not isinstance(segment, int) or isinstance(segment, bool) or segment < 1:
        raise ValueError(f"{field}.segment must be a positive integer")
    representation_fields = set(value) == representation_expected
    if representation_fields and not all(
        isinstance(value[name], str) and value[name].strip()
        for name in ("representation_id", "representation_kind", "artifact_id", "unit_id", "locator")
    ):
        raise ValueError(f"{field} Representation Evidence fields must be non-empty strings")
    return EvidenceRecord(
        source_id=_non_empty(value["source_id"], f"{field}.source_id"),
        artifact=_non_empty(value["artifact"], f"{field}.artifact"),
        segment=segment,
        speaker=_optional_text(value["speaker"], f"{field}.speaker"),
        start=_optional_text(value["start"], f"{field}.start"),
        end=_optional_text(value["end"], f"{field}.end"),
        excerpt=_non_empty(value["excerpt"], f"{field}.excerpt"),
        representation_id=(
            str(value["representation_id"]) if representation_fields else None
        ),
        representation_kind=(
            str(value["representation_kind"]) if representation_fields else None
        ),
        artifact_id=str(value["artifact_id"]) if representation_fields else None,
        unit_id=str(value["unit_id"]) if representation_fields else None,
        locator=str(value["locator"]) if representation_fields else None,
    )


def evidence_to_dict(evidence: EvidenceRecord) -> dict[str, object]:
    _validate_evidence(evidence, "Evidence")
    payload: dict[str, object] = {
        "source_id": evidence.source_id,
        "artifact": evidence.artifact,
        "segment": evidence.segment,
        "speaker": evidence.speaker,
        "start": evidence.start,
        "end": evidence.end,
        "excerpt": evidence.excerpt,
    }
    if evidence.representation_id is not None:
        payload.update(
            {
                "representation_id": evidence.representation_id,
                "representation_kind": evidence.representation_kind,
                "artifact_id": evidence.artifact_id,
                "unit_id": evidence.unit_id,
                "locator": evidence.locator,
            }
        )
    return payload


def claim_from_dict(value: object, field: str) -> ClaimAttribution:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    expected = {
        "claimant_object_id",
        "claimant_source_id",
        "claimant_label",
        "stance",
        "claimed_at",
        "attribution_confidence",
    }
    if set(value) != expected:
        raise ValueError(f"{field} does not match the Claim attribution schema")
    attribution_confidence = value["attribution_confidence"]
    if attribution_confidence is not None and (
        not isinstance(attribution_confidence, (int, float))
        or isinstance(attribution_confidence, bool)
        or not 0 <= attribution_confidence <= 1
    ):
        raise ValueError(f"{field}.attribution_confidence must be between 0 and 1")
    claim = ClaimAttribution(
        claimant_object_id=_optional_text(
            value["claimant_object_id"], f"{field}.claimant_object_id"
        ),
        claimant_source_id=_non_empty(
            value["claimant_source_id"], f"{field}.claimant_source_id"
        ),
        claimant_label=_optional_text(
            value["claimant_label"], f"{field}.claimant_label"
        ),
        stance=_non_empty(value["stance"], f"{field}.stance"),
        claimed_at=_optional_text(value["claimed_at"], f"{field}.claimed_at"),
        attribution_confidence=(
            None if attribution_confidence is None else float(attribution_confidence)
        ),
    )
    validate_claim_attribution(claim, field)
    return claim


def claim_to_dict(claim: ClaimAttribution) -> dict[str, object]:
    validate_claim_attribution(claim)
    return {
        "claimant_object_id": claim.claimant_object_id,
        "claimant_source_id": claim.claimant_source_id,
        "claimant_label": claim.claimant_label,
        "stance": claim.stance,
        "claimed_at": claim.claimed_at,
        "attribution_confidence": claim.attribution_confidence,
    }


def atomic_information_revision_from_dict(
    value: object, field: str = "atomic information revision"
) -> AtomicInformationRevision:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    expected = {
        "atomic_information_id",
        "revision_number",
        "revision_id",
        "origin_source_id",
        "origin_candidate_id",
        "origin_fingerprint",
        "statement",
        "semantic_type",
        "raw_concerns",
        "related_object_ids",
        "source_evidence",
        "context",
        "confidence",
        "created_at",
        "revision_reason",
    }
    if set(value) not in {frozenset(expected), frozenset((*expected, "claim"))}:
        raise ValueError(
            f"{field} does not match the Atomic Information revision schema"
        )

    revision_number = value["revision_number"]
    if (
        not isinstance(revision_number, int)
        or isinstance(revision_number, bool)
        or revision_number < 1
    ):
        raise ValueError(f"{field}.revision_number must be a positive integer")
    confidence = value["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise ValueError(f"{field}.confidence must be between 0 and 1")
    semantic_type = _non_empty(value["semantic_type"], f"{field}.semantic_type")
    if semantic_type not in SEMANTIC_TYPES:
        raise ValueError(f"{field}.semantic_type is not supported")
    raw_evidence = value["source_evidence"]
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ValueError(f"{field}.source_evidence must not be empty")

    revision = AtomicInformationRevision(
        atomic_information_id=_non_empty(
            value["atomic_information_id"], f"{field}.atomic_information_id"
        ),
        revision_number=revision_number,
        revision_id=_non_empty(value["revision_id"], f"{field}.revision_id"),
        origin_source_id=_non_empty(
            value["origin_source_id"], f"{field}.origin_source_id"
        ),
        origin_candidate_id=_non_empty(
            value["origin_candidate_id"], f"{field}.origin_candidate_id"
        ),
        origin_fingerprint=_non_empty(
            value["origin_fingerprint"], f"{field}.origin_fingerprint"
        ),
        statement=_non_empty(value["statement"], f"{field}.statement"),
        semantic_type=semantic_type,
        raw_concerns=_strings(
            value["raw_concerns"], f"{field}.raw_concerns", allow_empty=False
        ),
        related_object_ids=_strings(
            value["related_object_ids"],
            f"{field}.related_object_ids",
            allow_empty=True,
        ),
        source_evidence=tuple(
            evidence_from_dict(item, f"{field}.source_evidence[{index}]")
            for index, item in enumerate(raw_evidence, start=1)
        ),
        context=_non_empty(value["context"], f"{field}.context"),
        confidence=float(confidence),
        created_at=_non_empty(value["created_at"], f"{field}.created_at"),
        revision_reason=_non_empty(
            value["revision_reason"], f"{field}.revision_reason"
        ),
        claim=(
            None
            if value.get("claim") is None
            else claim_from_dict(value["claim"], f"{field}.claim")
        ),
    )
    validate_atomic_information_revision(revision, field)
    return revision


def atomic_information_revision_to_dict(
    revision: AtomicInformationRevision,
) -> dict[str, object]:
    validate_atomic_information_revision(revision)
    return {
        "atomic_information_id": revision.atomic_information_id,
        "revision_number": revision.revision_number,
        "revision_id": revision.revision_id,
        "origin_source_id": revision.origin_source_id,
        "origin_candidate_id": revision.origin_candidate_id,
        "origin_fingerprint": revision.origin_fingerprint,
        "statement": revision.statement,
        "semantic_type": revision.semantic_type,
        "raw_concerns": list(revision.raw_concerns),
        "related_object_ids": list(revision.related_object_ids),
        "source_evidence": [
            evidence_to_dict(item) for item in revision.source_evidence
        ],
        "context": revision.context,
        "confidence": revision.confidence,
        "created_at": revision.created_at,
        "revision_reason": revision.revision_reason,
        "claim": None if revision.claim is None else claim_to_dict(revision.claim),
    }


def validate_atomic_information_revision(
    revision: AtomicInformationRevision,
    field: str = "atomic information revision",
) -> None:
    if not isinstance(revision, AtomicInformationRevision):
        raise TypeError(f"{field} must be an AtomicInformationRevision")
    if (
        not isinstance(revision.revision_number, int)
        or isinstance(revision.revision_number, bool)
        or revision.revision_number < 1
    ):
        raise ValueError(f"{field}.revision_number must be positive")
    expected_atomic_information_id = (
        "atomic_info_"
        + hashlib.sha256(
            f"{revision.origin_source_id}\0{revision.origin_candidate_id}".encode()
        ).hexdigest()[:32]
    )
    if revision.atomic_information_id != expected_atomic_information_id:
        raise ValueError(
            f"{field}.atomic_information_id does not match its immutable origin"
        )
    expected_revision_id = (
        f"{revision.atomic_information_id}-r{revision.revision_number:04d}"
    )
    if revision.revision_id != expected_revision_id:
        raise ValueError(
            f"{field}.revision_id does not match its Atomic Information revision"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", revision.origin_fingerprint):
        raise ValueError(f"{field}.origin_fingerprint must be a SHA-256 hex digest")
    for name, value in (
        ("origin_source_id", revision.origin_source_id),
        ("origin_candidate_id", revision.origin_candidate_id),
        ("statement", revision.statement),
        ("context", revision.context),
        ("created_at", revision.created_at),
        ("revision_reason", revision.revision_reason),
    ):
        _non_empty(value, f"{field}.{name}")
    if revision.semantic_type not in SEMANTIC_TYPES:
        raise ValueError(f"{field}.semantic_type is not supported")
    if not isinstance(revision.raw_concerns, tuple):
        raise TypeError(f"{field}.raw_concerns must be a tuple")
    if not revision.raw_concerns:
        raise ValueError(f"{field}.raw_concerns must not be empty")
    for concern in revision.raw_concerns:
        _non_empty(concern, f"{field}.raw_concerns")
    if not isinstance(revision.related_object_ids, tuple):
        raise TypeError(f"{field}.related_object_ids must be a tuple")
    for object_id in revision.related_object_ids:
        _non_empty(object_id, f"{field}.related_object_ids")
    if not isinstance(revision.source_evidence, tuple):
        raise TypeError(f"{field}.source_evidence must be a tuple")
    if not revision.source_evidence:
        raise ValueError(f"{field}.source_evidence must not be empty")
    for index, evidence in enumerate(revision.source_evidence, start=1):
        _validate_evidence(evidence, f"{field}.source_evidence[{index}]")
    if (
        not isinstance(revision.confidence, (int, float))
        or isinstance(revision.confidence, bool)
        or not 0 <= revision.confidence <= 1
    ):
        raise ValueError(f"{field}.confidence must be between 0 and 1")
    if revision.claim is not None:
        validate_claim_attribution(revision.claim, f"{field}.claim")
        if revision.claim.claimant_source_id not in {
            evidence.source_id for evidence in revision.source_evidence
        }:
            raise ValueError(
                f"{field}.claim.claimant_source_id must reference source Evidence"
            )


def validate_claim_attribution(
    claim: ClaimAttribution, field: str = "Claim attribution"
) -> None:
    if not isinstance(claim, ClaimAttribution):
        raise TypeError(f"{field} must be a ClaimAttribution")
    _optional_text(claim.claimant_object_id, f"{field}.claimant_object_id")
    _non_empty(claim.claimant_source_id, f"{field}.claimant_source_id")
    _optional_text(claim.claimant_label, f"{field}.claimant_label")
    if claim.stance not in CLAIM_STANCES:
        raise ValueError(f"{field}.stance is not supported")
    _optional_text(claim.claimed_at, f"{field}.claimed_at")
    if claim.attribution_confidence is not None and (
        not isinstance(claim.attribution_confidence, (int, float))
        or isinstance(claim.attribution_confidence, bool)
        or not 0 <= claim.attribution_confidence <= 1
    ):
        raise ValueError(f"{field}.attribution_confidence must be between 0 and 1")


def _validate_evidence(evidence: EvidenceRecord, field: str) -> None:
    if not isinstance(evidence, EvidenceRecord):
        raise TypeError(f"{field} must be an EvidenceRecord")
    _non_empty(evidence.source_id, f"{field}.source_id")
    _non_empty(evidence.artifact, f"{field}.artifact")
    if (
        not isinstance(evidence.segment, int)
        or isinstance(evidence.segment, bool)
        or evidence.segment < 1
    ):
        raise ValueError(f"{field}.segment must be a positive integer")
    _optional_text(evidence.speaker, f"{field}.speaker")
    _optional_text(evidence.start, f"{field}.start")
    _optional_text(evidence.end, f"{field}.end")
    _non_empty(evidence.excerpt, f"{field}.excerpt")
    representation_values = (
        evidence.representation_id,
        evidence.representation_kind,
        evidence.artifact_id,
        evidence.unit_id,
        evidence.locator,
    )
    if any(value is not None for value in representation_values):
        if any(value is None for value in representation_values):
            raise ValueError(f"{field} Representation Evidence fields must be complete")
        for name, value in zip(
            ("representation_id", "representation_kind", "artifact_id", "unit_id", "locator"),
            representation_values,
        ):
            _non_empty(value, f"{field}.{name}")
