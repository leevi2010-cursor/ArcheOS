from __future__ import annotations

from dataclasses import dataclass

ALLOWED_ROLES = frozenset(
    {
        "person",
        "company",
        "brand",
        "project",
        "business_line",
        "event",
        "goal",
        "decision",
    }
)

ALLOWED_RELATIONSHIPS = frozenset(
    {"part_of", "member_of", "responsible_for", "depends_on", "related_to"}
)


@dataclass(frozen=True)
class ApplyReceiptRecord:
    apply_id: str
    payload: str
    created_at: str


@dataclass(frozen=True)
class ObjectRecord:
    object_id: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class NameAssignment:
    name_assignment_id: str
    object_id: str
    name: str
    valid_from: str
    valid_to: str | None
    is_primary: bool


@dataclass(frozen=True)
class RoleAssignment:
    role_assignment_id: str
    object_id: str
    role: str
    valid_from: str
    valid_to: str | None
    source_atomic_information_id: str | None
    confidence: float | None


@dataclass(frozen=True)
class LifecycleRecord:
    lifecycle_record_id: str
    object_id: str
    start_at: str | None
    actual_end_at: str | None
    target_end_at: str | None
    completion_condition: str | None
    state: str
    valid_from: str
    valid_to: str | None


@dataclass(frozen=True)
class RelationshipRecord:
    relationship_id: str
    from_object_id: str
    relation: str
    to_object_id: str
    valid_from: str
    valid_to: str | None
    source_atomic_information_id: str | None
    confidence: float | None


@dataclass(frozen=True)
class ObjectReadModel:
    object_id: str
    current_name: str
    roles: tuple[str, ...]
    status: str
    lifecycle: LifecycleRecord | None
