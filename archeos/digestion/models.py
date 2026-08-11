from __future__ import annotations

from dataclasses import dataclass

from ..atomic_information import AtomicInformationRevision
from ..world_model import ObjectReadModel

OPERATION_KINDS = frozenset(
    {
        "no_structural_change",
        "set_lifecycle",
        "add_role",
        "end_role",
        "rename",
        "create_relationship",
        "end_relationship",
        "new_object",
        "delete_object",
        "conflict",
        "unresolved",
    }
)

PROPOSAL_STATUSES = frozenset({"pending", "approved", "rejected", "deferred"})


@dataclass(frozen=True)
class WorldModelOperation:
    kind: str
    target_object_id: str | None = None
    secondary_object_id: str | None = None
    name: str | None = None
    role: str | None = None
    relation: str | None = None
    relationship_id: str | None = None
    lifecycle_state: str | None = None
    start_at: str | None = None
    actual_end_at: str | None = None
    target_end_at: str | None = None
    completion_condition: str | None = None


@dataclass(frozen=True)
class InterpretationResult:
    operations: tuple[WorldModelOperation, ...]
    rationale: str
    evidence_sufficient: bool
    conflict: bool
    ambiguous: bool


@dataclass(frozen=True)
class DigestionWorldState:
    resolved_objects: tuple[ObjectReadModel, ...]
    unmatched_concerns: tuple[str, ...]
    ambiguous_concerns: tuple[str, ...]


@dataclass(frozen=True)
class HumanReviewContent:
    finding: str
    importance: str
    recommendation: str
    evidence: str
    consequences: str


@dataclass(frozen=True)
class ChangeProposal:
    proposal_id: str
    atomic_information_id: str
    atomic_information_revision_id: str
    proposed_operations: tuple[WorldModelOperation, ...]
    resolved_object_ids: tuple[str, ...]
    rationale: str
    supporting_evidence_refs: tuple[str, ...]
    before_state_fingerprint: str
    interpretation_fingerprint: str
    human_review: HumanReviewContent
    status: str
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class ChangeJournalRecord:
    change_id: str
    atomic_information_id: str
    atomic_information_revision_id: str
    operation: str
    resolved_object_ids: tuple[str, ...]
    interpretation_fingerprint: str
    mode: str
    proposal_id: str | None
    status: str
    created_at: str
    applied_at: str | None
    error_code: str | None


@dataclass(frozen=True)
class DigestionResult:
    atomic_information: AtomicInformationRevision
    status: str
    change_ids: tuple[str, ...]
    proposal_id: str | None
