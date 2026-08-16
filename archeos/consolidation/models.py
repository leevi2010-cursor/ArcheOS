from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ..atomic_information import AtomicInformationRevision
from ..atomic_information.models import validate_atomic_information_revision

RELATION_ORDER = (
    "equivalent",
    "derived",
    "complementary",
    "temporal_update",
    "conflict",
    "uncertain",
)
ACTIVE_INFORMATION_RELATIONS = frozenset(RELATION_ORDER)
EVIDENCE_INDEPENDENCE_VALUES = frozenset(
    {"same_source_family", "independent", "unknown"}
)


def _non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _non_empty(value, field)


def _projection_fingerprint(
    atomic_information_id: str,
    revision_id: str,
    start_offset: int,
    end_offset: int,
    projection_text: str,
) -> str:
    payload = "\0".join(
        (
            atomic_information_id,
            revision_id,
            str(start_offset),
            str(end_offset),
            projection_text,
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def time_sort_key(value: str) -> float:
    text = _non_empty(value, "time basis")
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            return datetime(
                parsed_date.year,
                parsed_date.month,
                parsed_date.day,
                tzinfo=UTC,
            ).timestamp()
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "time basis must be an ISO date or timezone-aware datetime"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError("time basis datetime must include a timezone")
    return parsed.astimezone(UTC).timestamp()


@dataclass(frozen=True)
class InformationComparisonProjection:
    atomic_information_id: str
    revision_id: str
    start_offset: int
    end_offset: int
    projection_text: str
    projection_fingerprint: str
    statement_length: int

    def __post_init__(self) -> None:
        _non_empty(self.atomic_information_id, "atomic_information_id")
        _non_empty(self.revision_id, "revision_id")
        for field in ("start_offset", "end_offset", "statement_length"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field} must be an integer")
        if self.start_offset < 0:
            raise ValueError("start_offset must not be negative")
        if self.end_offset <= self.start_offset:
            raise ValueError("projection span must not be empty")
        if self.statement_length < self.end_offset:
            raise ValueError("projection span exceeds the recorded statement length")
        _non_empty(self.projection_text, "projection_text")
        if len(self.projection_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.projection_fingerprint
        ):
            raise ValueError("projection_fingerprint must be a SHA-256 hex digest")

    @classmethod
    def from_revision(
        cls,
        revision: AtomicInformationRevision,
        *,
        start_offset: int = 0,
        end_offset: int | None = None,
    ) -> InformationComparisonProjection:
        validate_atomic_information_revision(revision)
        if isinstance(start_offset, bool) or not isinstance(start_offset, int):
            raise TypeError("start_offset must be an integer")
        if end_offset is None:
            end_offset = len(revision.statement)
        if isinstance(end_offset, bool) or not isinstance(end_offset, int):
            raise TypeError("end_offset must be an integer")
        if start_offset < 0 or end_offset > len(revision.statement):
            raise ValueError("projection span is outside the revision statement")
        if end_offset <= start_offset:
            raise ValueError("projection span must not be empty")
        projection_text = revision.statement[start_offset:end_offset]
        if not projection_text.strip():
            raise ValueError("projection span must include non-whitespace text")
        return cls(
            atomic_information_id=revision.atomic_information_id,
            revision_id=revision.revision_id,
            start_offset=start_offset,
            end_offset=end_offset,
            projection_text=projection_text,
            projection_fingerprint=_projection_fingerprint(
                revision.atomic_information_id,
                revision.revision_id,
                start_offset,
                end_offset,
                projection_text,
            ),
            statement_length=len(revision.statement),
        )

    @property
    def is_full_statement(self) -> bool:
        return self.start_offset == 0 and self.end_offset == self.statement_length

    @property
    def sort_key(self) -> tuple[object, ...]:
        return (
            self.atomic_information_id,
            self.revision_id,
            self.start_offset,
            self.end_offset,
            self.projection_fingerprint,
        )

    def validate_against(self, revision: AtomicInformationRevision) -> None:
        validate_atomic_information_revision(revision)
        if (
            revision.atomic_information_id != self.atomic_information_id
            or revision.revision_id != self.revision_id
        ):
            raise ValueError("projection is stale for the supplied revision")
        if len(revision.statement) != self.statement_length:
            raise ValueError("projection is stale for the supplied statement length")
        if self.end_offset > len(revision.statement):
            raise ValueError("projection span is outside the supplied revision")
        actual_text = revision.statement[self.start_offset : self.end_offset]
        if actual_text != self.projection_text:
            raise ValueError("projection text does not match the supplied revision")
        actual_fingerprint = _projection_fingerprint(
            self.atomic_information_id,
            self.revision_id,
            self.start_offset,
            self.end_offset,
            self.projection_text,
        )
        if actual_fingerprint != self.projection_fingerprint:
            raise ValueError("projection fingerprint does not match its revision span")


@dataclass(frozen=True)
class InformationRelationJudgment:
    left_projection: InformationComparisonProjection
    right_projection: InformationComparisonProjection
    relation: str
    provenance_basis: tuple[str, ...]
    evidence_independence: str
    uncertainty: str | None = None
    rationale: str | None = None
    left_time: str | None = None
    right_time: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.left_projection, InformationComparisonProjection):
            raise TypeError(
                "left_projection must be an InformationComparisonProjection"
            )
        if not isinstance(self.right_projection, InformationComparisonProjection):
            raise TypeError(
                "right_projection must be an InformationComparisonProjection"
            )
        if self.left_projection.sort_key >= self.right_projection.sort_key:
            raise ValueError(
                "judgment projections must use deterministic canonical order"
            )
        if self.relation not in ACTIVE_INFORMATION_RELATIONS:
            raise ValueError("relation must be an active relation")
        if not isinstance(self.provenance_basis, tuple) or not self.provenance_basis:
            raise ValueError("provenance_basis must be a non-empty tuple")
        for item in self.provenance_basis:
            _non_empty(item, "provenance_basis")
        if self.evidence_independence not in EVIDENCE_INDEPENDENCE_VALUES:
            raise ValueError("evidence_independence is not supported")
        _optional_text(self.uncertainty, "uncertainty")
        _optional_text(self.rationale, "rationale")
        if self.relation == "uncertain" and self.uncertainty is None:
            raise ValueError("uncertain judgment must describe its uncertainty")
        for value in (self.left_time, self.right_time):
            if value is not None:
                time_sort_key(value)

    @classmethod
    def create(
        cls,
        left_projection: InformationComparisonProjection,
        right_projection: InformationComparisonProjection,
        relation: str,
        *,
        left_revision: AtomicInformationRevision,
        right_revision: AtomicInformationRevision,
        provenance_basis: tuple[str, ...],
        evidence_independence: str,
        uncertainty: str | None = None,
        rationale: str | None = None,
        left_time: str | None = None,
        right_time: str | None = None,
    ) -> InformationRelationJudgment:
        left_projection.validate_against(left_revision)
        right_projection.validate_against(right_revision)
        if left_projection.sort_key == right_projection.sort_key:
            raise ValueError("a projection cannot be related to itself")
        if left_projection.sort_key > right_projection.sort_key:
            left_projection, right_projection = right_projection, left_projection
            left_time, right_time = right_time, left_time
        return cls(
            left_projection=left_projection,
            right_projection=right_projection,
            relation=relation,
            provenance_basis=provenance_basis,
            evidence_independence=evidence_independence,
            uncertainty=uncertainty,
            rationale=rationale,
            left_time=left_time,
            right_time=right_time,
        )

    @property
    def pair_key(self) -> tuple[tuple[object, ...], tuple[object, ...]]:
        return (self.left_projection.sort_key, self.right_projection.sort_key)

    def validate_against(
        self, revisions: Mapping[str, AtomicInformationRevision]
    ) -> None:
        for projection in (self.left_projection, self.right_projection):
            revision = revisions.get(projection.atomic_information_id)
            if revision is None:
                raise ValueError(
                    "judgment projection is stale or missing from current revisions"
                )
            projection.validate_against(revision)
