from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

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
    independent_source_count: int
    representation_count: int


@dataclass(frozen=True)
class ConsolidatedInformationViewMetadata:
    total_information: int
    included_information: int
    group_count: int
    relation_states: tuple[str, ...]
    independent_source_count: int
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


def _source_count(revisions: Iterable[AtomicInformationRevision]) -> int:
    return len({evidence.source_id for evidence in _evidence(revisions)})


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
            independent_source_count=_source_count(raw_information),
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
                    independent_source_count=_source_count(information),
                    representation_count=_representation_count(information),
                )
            )
        return tuple(sorted(groups, key=lambda item: item.projections[0].sort_key))
