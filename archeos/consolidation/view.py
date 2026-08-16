from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations

from ..atomic_information import AtomicInformationRevision, EvidenceRecord
from ..atomic_information.models import validate_atomic_information_revision
from .models import (
    RELATION_ORDER,
    InformationComparisonProjection,
    InformationRelationJudgment,
    time_sort_key,
)


@dataclass(frozen=True)
class ConsolidatedInformationGroup:
    group_id: str
    projections: tuple[InformationComparisonProjection, ...]
    display_projections: tuple[InformationComparisonProjection, ...]
    judgments: tuple[InformationRelationJudgment, ...]
    information: tuple[AtomicInformationRevision, ...]
    relation_states: tuple[str, ...]
    temporal_order_complete: bool | None
    evidence_count: int
    distinct_source_count: int
    independent_source_count: int | None
    representation_count: int


@dataclass(frozen=True)
class ConsolidatedInformationViewMetadata:
    total_information: int
    included_information: int
    group_count: int
    relation_states: tuple[str, ...]
    distinct_source_count: int
    independent_source_count: int | None
    representation_count: int
    evidence_count: int
    pending_or_uncertain_count: int
    retrieval_completeness: str


@dataclass(frozen=True)
class ConsolidatedInformationView:
    raw_information: tuple[AtomicInformationRevision, ...]
    groups: tuple[ConsolidatedInformationGroup, ...]
    ungrouped_information: tuple[AtomicInformationRevision, ...]
    metadata: ConsolidatedInformationViewMetadata


def _evidence(
    revisions: Iterable[AtomicInformationRevision],
) -> tuple[EvidenceRecord, ...]:
    return tuple(
        evidence for revision in revisions for evidence in revision.source_evidence
    )


def _source_ids(revisions: Iterable[AtomicInformationRevision]) -> frozenset[str]:
    return frozenset(evidence.source_id for evidence in _evidence(revisions))


def _distinct_source_count(revisions: Iterable[AtomicInformationRevision]) -> int:
    return len(_source_ids(revisions))


def _independent_source_count(
    revisions: tuple[AtomicInformationRevision, ...],
    judgments: Iterable[InformationRelationJudgment],
) -> int | None:
    source_ids = _source_ids(revisions)
    if len(source_ids) <= 1:
        return len(source_ids)

    revisions_by_id = {
        revision.atomic_information_id: revision for revision in revisions
    }
    pair_labels: dict[frozenset[str], set[str]] = {}
    for judgment in judgments:
        left = revisions_by_id[judgment.left_projection.atomic_information_id]
        right = revisions_by_id[judgment.right_projection.atomic_information_id]
        left_source_ids = _source_ids((left,))
        right_source_ids = _source_ids((right,))
        if len(left_source_ids) != 1 or len(right_source_ids) != 1:
            return None
        left_source_id = next(iter(left_source_ids))
        right_source_id = next(iter(right_source_ids))
        if left_source_id == right_source_id:
            continue
        pair_labels.setdefault(frozenset((left_source_id, right_source_id)), set()).add(
            judgment.evidence_independence
        )

    if any(len(labels) != 1 for labels in pair_labels.values()):
        return None

    parent = {source_id: source_id for source_id in source_ids}

    def find(source_id: str) -> str:
        while parent[source_id] != source_id:
            parent[source_id] = parent[parent[source_id]]
            source_id = parent[source_id]
        return source_id

    def union(left_source_id: str, right_source_id: str) -> None:
        left_root = find(left_source_id)
        right_root = find(right_source_id)
        if left_root != right_root:
            parent[right_root] = left_root

    for pair, labels in pair_labels.items():
        if next(iter(labels)) == "same_source_family":
            left_source_id, right_source_id = sorted(pair)
            union(left_source_id, right_source_id)

    for pair, labels in pair_labels.items():
        label = next(iter(labels))
        left_source_id, right_source_id = sorted(pair)
        if label == "unknown":
            return None
        if label == "independent" and find(left_source_id) == find(right_source_id):
            return None

    for left_source_id, right_source_id in combinations(sorted(source_ids), 2):
        if find(left_source_id) == find(right_source_id):
            continue
        labels = pair_labels.get(frozenset((left_source_id, right_source_id)))
        if labels != {"independent"}:
            return None
    return len({find(source_id) for source_id in source_ids})


def _representation_count(revisions: Iterable[AtomicInformationRevision]) -> int:
    return len(
        {
            evidence.representation_id
            for evidence in _evidence(revisions)
            if evidence.representation_id is not None
        }
    )


def _relation_states(
    judgments: Iterable[InformationRelationJudgment],
) -> tuple[str, ...]:
    present = {judgment.relation for judgment in judgments}
    return tuple(relation for relation in RELATION_ORDER if relation in present)


class ConsolidatedInformationViewBuilder:
    def build(
        self,
        revisions: Iterable[AtomicInformationRevision],
        judgments: Iterable[InformationRelationJudgment] = (),
        *,
        retrieval_completeness: str = "not_supplied",
    ) -> ConsolidatedInformationView:
        if (
            not isinstance(retrieval_completeness, str)
            or not retrieval_completeness.strip()
        ):
            raise ValueError("retrieval_completeness must be a non-empty string")
        raw_information = tuple(revisions)
        by_atomic_id: dict[str, AtomicInformationRevision] = {}
        for index, revision in enumerate(raw_information, start=1):
            validate_atomic_information_revision(revision, f"revisions[{index}]")
            if revision.atomic_information_id in by_atomic_id:
                raise ValueError(
                    "revisions must contain one current revision per Atomic Information"
                )
            by_atomic_id[revision.atomic_information_id] = revision

        supplied_judgments = tuple(judgments)
        seen_pairs: dict[tuple[tuple[object, ...], tuple[object, ...]], str] = {}
        for judgment in supplied_judgments:
            if not isinstance(judgment, InformationRelationJudgment):
                raise TypeError(
                    "judgments must contain InformationRelationJudgment values"
                )
            judgment.validate_against(by_atomic_id)
            previous = seen_pairs.get(judgment.pair_key)
            if previous is not None:
                raise ValueError(
                    "projection pair has duplicate or conflicting judgments"
                )
            seen_pairs[judgment.pair_key] = judgment.relation

        groups = self._groups(raw_information, supplied_judgments)
        grouped_information_ids = {
            projection.atomic_information_id
            for group in groups
            for projection in group.projections
        }
        ungrouped = tuple(
            revision
            for revision in raw_information
            if revision.atomic_information_id not in grouped_information_ids
        )
        evidence = _evidence(raw_information)
        metadata = ConsolidatedInformationViewMetadata(
            total_information=len(raw_information),
            included_information=len(raw_information),
            group_count=len(groups),
            relation_states=_relation_states(supplied_judgments),
            distinct_source_count=_distinct_source_count(raw_information),
            independent_source_count=_independent_source_count(
                raw_information, supplied_judgments
            ),
            representation_count=_representation_count(raw_information),
            evidence_count=len(evidence),
            pending_or_uncertain_count=sum(
                judgment.relation == "uncertain" for judgment in supplied_judgments
            ),
            retrieval_completeness=retrieval_completeness,
        )
        return ConsolidatedInformationView(
            raw_information=raw_information,
            groups=groups,
            ungrouped_information=ungrouped,
            metadata=metadata,
        )

    @staticmethod
    def _groups(
        revisions: tuple[AtomicInformationRevision, ...],
        judgments: tuple[InformationRelationJudgment, ...],
    ) -> tuple[ConsolidatedInformationGroup, ...]:
        if not judgments:
            return ()
        projections: dict[str, InformationComparisonProjection] = {}
        adjacency: dict[str, set[str]] = {}
        judgments_by_projection: dict[str, list[InformationRelationJudgment]] = {}
        times: dict[str, str] = {}

        for judgment in judgments:
            pair = (
                (judgment.left_projection, judgment.left_time),
                (judgment.right_projection, judgment.right_time),
            )
            left_key = judgment.left_projection.projection_fingerprint
            right_key = judgment.right_projection.projection_fingerprint
            adjacency.setdefault(left_key, set()).add(right_key)
            adjacency.setdefault(right_key, set()).add(left_key)
            for projection, timestamp in pair:
                key = projection.projection_fingerprint
                existing = projections.get(key)
                if existing is not None and existing != projection:
                    raise ValueError("projection fingerprint collision")
                projections[key] = projection
                judgments_by_projection.setdefault(key, []).append(judgment)
                if timestamp is not None:
                    previous = times.get(key)
                    if previous is not None and previous != timestamp:
                        raise ValueError("projection has conflicting time basis")
                    times[key] = timestamp

        components: list[set[str]] = []
        remaining = set(adjacency)
        while remaining:
            start = min(remaining, key=lambda key: projections[key].sort_key)
            component: set[str] = set()
            stack = [start]
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                stack.extend(adjacency[current] - component)
            remaining -= component
            components.append(component)

        groups: list[ConsolidatedInformationGroup] = []
        for component in components:
            component_projections = tuple(
                sorted(
                    (projections[key] for key in component),
                    key=lambda item: item.sort_key,
                )
            )
            component_judgments = tuple(
                sorted(
                    {
                        judgment
                        for key in component
                        for judgment in judgments_by_projection[key]
                    },
                    key=lambda item: (
                        item.pair_key,
                        RELATION_ORDER.index(item.relation),
                    ),
                )
            )
            states = _relation_states(component_judgments)
            temporal_complete: bool | None = None
            display = component_projections
            if "temporal_update" in states:
                temporal_complete = all(
                    item.projection_fingerprint in times
                    for item in component_projections
                )
                if temporal_complete:
                    display = tuple(
                        sorted(
                            component_projections,
                            key=lambda item: (
                                time_sort_key(times[item.projection_fingerprint]),
                                item.sort_key,
                            ),
                        )
                    )
            information_ids = {
                projection.atomic_information_id for projection in component_projections
            }
            information = tuple(
                revision
                for revision in revisions
                if revision.atomic_information_id in information_ids
            )
            group_basis = "\0".join(
                projection.projection_fingerprint
                for projection in component_projections
            )
            group_id = "group_" + hashlib.sha256(group_basis.encode()).hexdigest()[:16]
            groups.append(
                ConsolidatedInformationGroup(
                    group_id=group_id,
                    projections=component_projections,
                    display_projections=display,
                    judgments=component_judgments,
                    information=information,
                    relation_states=states,
                    temporal_order_complete=temporal_complete,
                    evidence_count=len(_evidence(information)),
                    distinct_source_count=_distinct_source_count(information),
                    independent_source_count=_independent_source_count(
                        information, component_judgments
                    ),
                    representation_count=_representation_count(information),
                )
            )
        return tuple(sorted(groups, key=lambda item: item.projections[0].sort_key))
