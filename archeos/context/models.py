from __future__ import annotations

from dataclasses import dataclass

from ..atomic_information import ClaimAttribution, EvidenceRecord
from ..digestion.models import HumanReviewContent, WorldModelOperation
from ..world_model import LifecycleRecord

DEFAULT_MAX_RELATIONSHIPS = 50
DEFAULT_MAX_ATOMIC_INFORMATION = 50
DEFAULT_MAX_CHANGES = 50
DEFAULT_MAX_PENDING_JUDGMENTS = 20
DEFAULT_MAX_EVIDENCE_PER_INFORMATION = 5


def _positive_limit(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True)
class ContextRequest:
    scope: str
    object_id: str
    max_relationships: int = DEFAULT_MAX_RELATIONSHIPS
    max_atomic_information: int = DEFAULT_MAX_ATOMIC_INFORMATION
    max_changes: int = DEFAULT_MAX_CHANGES
    max_pending_judgments: int = DEFAULT_MAX_PENDING_JUDGMENTS
    max_evidence_per_information: int = DEFAULT_MAX_EVIDENCE_PER_INFORMATION

    def __post_init__(self) -> None:
        if self.scope != "object":
            raise ValueError("scope must be 'object'")
        if not isinstance(self.object_id, str) or not self.object_id.strip():
            raise ValueError("object_id must be a non-empty string")
        for field in (
            "max_relationships",
            "max_atomic_information",
            "max_changes",
            "max_pending_judgments",
            "max_evidence_per_information",
        ):
            _positive_limit(getattr(self, field), field)


@dataclass(frozen=True)
class ContextRoot:
    object_id: str
    current_name: str
    roles: tuple[str, ...]
    status: str
    lifecycle: LifecycleRecord | None


@dataclass(frozen=True)
class ContextNeighbor:
    object_id: str
    current_name: str
    roles: tuple[str, ...]
    status: str
    lifecycle: LifecycleRecord | None = None


@dataclass(frozen=True)
class ContextRelationshipItem:
    relationship_id: str
    relation: str
    direction: str
    neighbor: ContextNeighbor
    valid_from: str
    confidence: float | None
    source_atomic_information_id: str | None


@dataclass(frozen=True)
class ContextAtomicInformationItem:
    atomic_information_id: str
    revision_id: str
    revision_number: int
    revision_count: int
    statement: str
    semantic_type: str
    claim: ClaimAttribution | None
    context: str
    confidence: float
    raw_concerns: tuple[str, ...]
    related_object_ids: tuple[str, ...]
    source_evidence: tuple[EvidenceRecord, ...]
    created_at: str


@dataclass(frozen=True)
class ContextChangeItem:
    change_id: str
    atomic_information_id: str
    atomic_information_revision_id: str
    operation: str
    resolved_object_ids: tuple[str, ...]
    mode: str
    proposal_id: str | None
    status: str
    created_at: str
    applied_at: str | None


@dataclass(frozen=True)
class ContextPendingJudgmentItem:
    proposal_id: str
    status: str
    atomic_information_id: str
    atomic_information_revision_id: str
    rationale: str
    human_review: HumanReviewContent
    claim_summary: str | None
    proposed_claim: ClaimAttribution | None
    proposed_operations: tuple[WorldModelOperation, ...]
    created_at: str


@dataclass(frozen=True)
class ContextCoverage:
    total: int
    included: int
    truncated: bool


@dataclass(frozen=True)
class ContextMetadata:
    schema_version: str
    scope: str
    root_object_id: str
    relationships: ContextCoverage
    atomic_information: ContextCoverage
    recent_changes: ContextCoverage
    pending_judgments: ContextCoverage
    evidence_truncated_atomic_information_ids: tuple[str, ...]
    complete: bool
    incomplete_reasons: tuple[str, ...]
    generated_at: str


@dataclass(frozen=True)
class ContextBundle:
    root: ContextRoot
    relationships: tuple[ContextRelationshipItem, ...]
    atomic_information: tuple[ContextAtomicInformationItem, ...]
    recent_changes: tuple[ContextChangeItem, ...]
    pending_judgments: tuple[ContextPendingJudgmentItem, ...]
    metadata: ContextMetadata
