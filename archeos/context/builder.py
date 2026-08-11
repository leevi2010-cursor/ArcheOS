from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from ..atomic_information import AtomicInformationRevision, AtomicInformationStore
from ..digestion.contracts import ChangeJournal, ChangeProposalStore
from ..world_model import (
    ALLOWED_RELATIONSHIPS,
    ObjectReadModel,
    ObjectResolver,
    WorldModelRepository,
)
from .models import (
    ContextAtomicInformationItem,
    ContextBundle,
    ContextChangeItem,
    ContextCoverage,
    ContextMetadata,
    ContextNeighbor,
    ContextPendingJudgmentItem,
    ContextRelationshipItem,
    ContextRequest,
    ContextRoot,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContextBuilder:
    def __init__(
        self,
        world_model_repository: WorldModelRepository,
        object_resolver: ObjectResolver,
        atomic_information_store: AtomicInformationStore,
        change_journal: ChangeJournal,
        change_proposal_store: ChangeProposalStore,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.world_model_repository = world_model_repository
        self.object_resolver = object_resolver
        self.atomic_information_store = atomic_information_store
        self.change_journal = change_journal
        self.change_proposal_store = change_proposal_store
        self.clock = clock

    def build(self, request: ContextRequest) -> ContextBundle:
        if not isinstance(request, ContextRequest):
            raise TypeError("request must be a ContextRequest")
        root_model = self.object_resolver.resolve(request.object_id)
        root = self._root(root_model)

        relationship_items = self._relationships(root_model)
        current_atomic, current_atomic_map = self._atomic_information(root_model)
        selected_atomic = current_atomic[: request.max_atomic_information]
        atomic_items, evidence_truncated_ids = self._atomic_items(
            selected_atomic, request
        )
        all_changes = self._changes(root_model.object_id)
        all_pending = self._pending(root_model.object_id, current_atomic_map)

        relationships = relationship_items[: request.max_relationships]
        atomic_information = atomic_items
        recent_changes = all_changes[: request.max_changes]
        pending_judgments = all_pending[: request.max_pending_judgments]

        coverage = {
            "relationships": self._coverage(
                len(relationship_items), len(relationships)
            ),
            "atomic_information": self._coverage(
                len(current_atomic), len(atomic_information)
            ),
            "recent_changes": self._coverage(len(all_changes), len(recent_changes)),
            "pending_judgments": self._coverage(
                len(all_pending), len(pending_judgments)
            ),
        }
        reasons = tuple(
            reason
            for reason, is_truncated in (
                ("relationships_limit", coverage["relationships"].truncated),
                (
                    "atomic_information_limit",
                    coverage["atomic_information"].truncated,
                ),
                ("recent_changes_limit", coverage["recent_changes"].truncated),
                (
                    "pending_judgments_limit",
                    coverage["pending_judgments"].truncated,
                ),
                ("evidence_limit", bool(evidence_truncated_ids)),
            )
            if is_truncated
        )
        metadata = ContextMetadata(
            schema_version="1.0",
            scope=request.scope,
            root_object_id=root.object_id,
            relationships=coverage["relationships"],
            atomic_information=coverage["atomic_information"],
            recent_changes=coverage["recent_changes"],
            pending_judgments=coverage["pending_judgments"],
            evidence_truncated_atomic_information_ids=evidence_truncated_ids,
            complete=not reasons,
            incomplete_reasons=reasons,
            generated_at=self.clock(),
        )
        return ContextBundle(
            root=root,
            relationships=relationships,
            atomic_information=atomic_information,
            recent_changes=recent_changes,
            pending_judgments=pending_judgments,
            metadata=metadata,
        )

    @staticmethod
    def _root(model: ObjectReadModel) -> ContextRoot:
        return ContextRoot(
            object_id=model.object_id,
            current_name=model.current_name,
            roles=model.roles,
            status=model.status,
            lifecycle=model.lifecycle,
        )

    def _relationships(
        self,
        root: ObjectReadModel,
    ) -> tuple[ContextRelationshipItem, ...]:
        items: list[ContextRelationshipItem] = []
        for relationship in self.world_model_repository.list_relationships(
            object_id=root.object_id, active_only=True
        ):
            if relationship.relation not in ALLOWED_RELATIONSHIPS:
                raise ValueError(
                    f"active relationship uses unsupported vocabulary: {relationship.relation}"
                )
            if relationship.from_object_id == root.object_id:
                if relationship.to_object_id == root.object_id:
                    raise ValueError("active relationship cannot point to itself")
                direction = "outgoing"
                neighbor_id = relationship.to_object_id
            elif relationship.to_object_id == root.object_id:
                direction = "incoming"
                neighbor_id = relationship.from_object_id
            else:
                raise ValueError(
                    "repository returned a relationship unrelated to the context root"
                )
            neighbor_model = self.object_resolver.resolve(neighbor_id)
            neighbor = ContextNeighbor(
                object_id=neighbor_model.object_id,
                current_name=neighbor_model.current_name,
                roles=neighbor_model.roles,
                status=neighbor_model.status,
                lifecycle=neighbor_model.lifecycle,
            )
            items.append(
                ContextRelationshipItem(
                    relationship_id=relationship.relationship_id,
                    relation=relationship.relation,
                    direction=direction,
                    neighbor=neighbor,
                    valid_from=relationship.valid_from,
                    confidence=relationship.confidence,
                    source_atomic_information_id=(
                        relationship.source_atomic_information_id
                    ),
                )
            )
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    item.relation,
                    item.direction,
                    item.neighbor.current_name.casefold(),
                    item.neighbor.object_id,
                    item.relationship_id,
                ),
            )
        )

    def _atomic_information(
        self,
        root: ObjectReadModel,
    ) -> tuple[tuple[AtomicInformationRevision, ...], dict[str, AtomicInformationRevision]]:
        current = tuple(
            item
            for item in self.atomic_information_store.list_atomic_information()
            if root.object_id in item.related_object_ids
        )
        current = tuple(
            sorted(
                current,
                key=lambda item: (item.created_at, item.atomic_information_id),
                reverse=True,
            )
        )
        return current, {item.atomic_information_id: item for item in current}

    def _atomic_items(
        self,
        current: tuple[AtomicInformationRevision, ...],
        request: ContextRequest,
    ) -> tuple[tuple[ContextAtomicInformationItem, ...], tuple[str, ...]]:
        items: list[ContextAtomicInformationItem] = []
        evidence_truncated: list[str] = []
        for item in current:
            revisions = self.atomic_information_store.list_revisions(
                item.atomic_information_id
            )
            if not revisions:
                raise ValueError(
                    f"Atomic Information has no revision history: {item.atomic_information_id}"
                )
            evidence = item.source_evidence[: request.max_evidence_per_information]
            if len(item.source_evidence) > len(evidence):
                evidence_truncated.append(item.atomic_information_id)
            items.append(
                ContextAtomicInformationItem(
                    atomic_information_id=item.atomic_information_id,
                    revision_id=item.revision_id,
                    revision_number=item.revision_number,
                    revision_count=len(revisions),
                    statement=item.statement,
                    semantic_type=item.semantic_type,
                    claim=item.claim,
                    context=item.context,
                    confidence=item.confidence,
                    raw_concerns=item.raw_concerns,
                    related_object_ids=item.related_object_ids,
                    source_evidence=evidence,
                    created_at=item.created_at,
                )
            )
        return tuple(items), tuple(evidence_truncated)

    def _changes(self, root_object_id: str) -> tuple[ContextChangeItem, ...]:
        records = self.change_journal.list_changes()
        items: list[ContextChangeItem] = []
        for record in records:
            if record.status not in {"applied", "failed"}:
                raise ValueError(f"unsupported Change Journal status: {record.status}")
            if record.mode not in {"automatic", "human_approved"}:
                raise ValueError(f"unsupported Change Journal mode: {record.mode}")
            if record.status != "applied" or root_object_id not in record.resolved_object_ids:
                continue
            items.append(
                ContextChangeItem(
                    change_id=record.change_id,
                    atomic_information_id=record.atomic_information_id,
                    atomic_information_revision_id=record.atomic_information_revision_id,
                    operation=record.operation,
                    resolved_object_ids=record.resolved_object_ids,
                    mode=record.mode,
                    proposal_id=record.proposal_id,
                    status=record.status,
                    created_at=record.created_at,
                    applied_at=record.applied_at,
                )
            )
        return tuple(
            sorted(items, key=lambda item: (item.created_at, item.change_id), reverse=True)
        )

    def _pending(
        self,
        root_object_id: str,
        current_atomic: dict[str, AtomicInformationRevision],
    ) -> tuple[ContextPendingJudgmentItem, ...]:
        proposals = self.change_proposal_store.list_unresolved()
        items: list[ContextPendingJudgmentItem] = []
        for proposal in proposals:
            if proposal.status in {"approved", "rejected"}:
                continue
            if proposal.status not in {"pending", "deferred"}:
                raise ValueError(
                    f"unresolved Proposal has unsupported status: {proposal.status}"
                )
            operation_matches = any(
                root_object_id in {
                    operation.target_object_id,
                    operation.secondary_object_id,
                }
                for operation in proposal.proposed_operations
            )
            claim_matches = (
                proposal.proposed_claim is not None
                and proposal.proposed_claim.claimant_object_id == root_object_id
            )
            information_matches = (
                proposal.atomic_information_id in current_atomic
                and root_object_id
                in current_atomic[proposal.atomic_information_id].related_object_ids
            )
            if not (
                root_object_id in proposal.resolved_object_ids
                or operation_matches
                or claim_matches
                or information_matches
            ):
                continue
            items.append(
                ContextPendingJudgmentItem(
                    proposal_id=proposal.proposal_id,
                    status=proposal.status,
                    atomic_information_id=proposal.atomic_information_id,
                    atomic_information_revision_id=proposal.atomic_information_revision_id,
                    rationale=proposal.rationale,
                    human_review=proposal.human_review,
                    claim_summary=proposal.claim_summary,
                    proposed_claim=proposal.proposed_claim,
                    proposed_operations=proposal.proposed_operations,
                    created_at=proposal.created_at,
                )
            )
        return tuple(
            sorted(items, key=lambda item: (item.created_at, item.proposal_id), reverse=True)
        )

    @staticmethod
    def _coverage(total: int, included: int) -> ContextCoverage:
        return ContextCoverage(total=total, included=included, truncated=total > included)
