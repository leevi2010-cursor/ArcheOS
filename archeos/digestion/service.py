from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from ..atomic_information import (
    AtomicInformationRevision,
    AtomicInformationStore,
    ClaimAttribution,
)
from ..atomic_information.models import claim_to_dict, validate_claim_attribution
from ..world_model import (
    ALLOWED_RELATIONSHIPS,
    ALLOWED_ROLES,
    ObjectResolver,
    WorldModelRepository,
)
from .contracts import (
    AtomicInformationInterpretationProvider,
    ChangeJournal,
    ChangeProposalStore,
    HumanJudgmentPort,
)
from .models import (
    ChangeJournalRecord,
    ChangeProposal,
    DigestionResult,
    DigestionWorldState,
    HumanReviewContent,
    InterpretationResult,
    WorldModelOperation,
)
from .serialization import (
    journal_from_dict,
    journal_to_dict,
    operation_to_dict,
    validate_operation,
)

MAX_RELATED_INFORMATION = 20


@dataclass(frozen=True)
class _PreparedDigestion:
    atomic_information: AtomicInformationRevision
    recovered_change_ids: tuple[str, ...]
    resolved_ids: tuple[str, ...]
    unmatched: tuple[str, ...]
    ambiguous: tuple[str, ...]
    all_related_information: tuple[AtomicInformationRevision, ...]
    related_information: tuple[AtomicInformationRevision, ...]
    world_state: DigestionWorldState


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _digest_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]
    return f"{prefix}_{digest}"


class AtomicInformationDigestionService:
    def __init__(
        self,
        atomic_information_store: AtomicInformationStore,
        world_model_repository: WorldModelRepository,
        object_resolver: ObjectResolver,
        interpretation_provider: AtomicInformationInterpretationProvider | None,
        proposal_store: ChangeProposalStore,
        change_journal: ChangeJournal,
        human_judgment: HumanJudgmentPort,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.atomic_information_store = atomic_information_store
        self.world_model_repository = world_model_repository
        self.interpretation_provider = interpretation_provider
        self.proposal_store = proposal_store
        self.change_journal = change_journal
        self.human_judgment = human_judgment
        self.clock = clock
        self.resolver = object_resolver

    def digest(self, atomic_information_id: str) -> DigestionResult:
        if self.interpretation_provider is None:
            raise RuntimeError("an interpretation provider is required for digestion")
        prepared = self._prepare(atomic_information_id)
        interpretation = self.interpretation_provider.interpret(
            prepared.atomic_information, prepared.world_state
        )
        return self._apply_prepared(prepared, interpretation)

    def interpret_batch(
        self, atomic_information_ids: Sequence[str]
    ) -> tuple[InterpretationResult, ...]:
        if self.interpretation_provider is None:
            raise RuntimeError("an interpretation provider is required for digestion")
        identifiers = tuple(atomic_information_ids)
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("batch digestion IDs must be unique and non-empty")
        provider_method = getattr(self.interpretation_provider, "interpret_batch", None)
        if not callable(provider_method):
            if len(identifiers) == 1:
                prepared = self._prepare(
                    identifiers[0], recover_automatic_receipts=False
                )
                return (
                    self.interpretation_provider.interpret(
                        prepared.atomic_information, prepared.world_state
                    ),
                )
            raise RuntimeError(
                "batch interpretation provider is required for multi-item digestion"
            )
        prepared = tuple(
            self._prepare(identifier, recover_automatic_receipts=False)
            for identifier in identifiers
        )
        results = tuple(
            provider_method(
                tuple((item.atomic_information, item.world_state) for item in prepared)
            )
        )
        if len(results) != len(prepared):
            raise ValueError("batch interpretation result count does not match input")
        for item, interpretation in zip(prepared, results, strict=True):
            self._validate_interpretation(interpretation)
            self._validate_claim_enrichment(
                interpretation.claim, item.atomic_information, item.resolved_ids
            )
        return results

    def apply_interpretation(
        self,
        atomic_information_id: str,
        interpretation: InterpretationResult,
    ) -> DigestionResult:
        return self._apply_prepared(
            self._prepare(atomic_information_id), interpretation
        )

    def digest_batch(
        self, atomic_information_ids: Sequence[str]
    ) -> tuple[DigestionResult, ...]:
        identifiers = tuple(atomic_information_ids)
        interpretations = self.interpret_batch(identifiers)
        return tuple(
            self.apply_interpretation(identifier, interpretation)
            for identifier, interpretation in zip(
                identifiers, interpretations, strict=True
            )
        )

    def _prepare(
        self,
        atomic_information_id: str,
        *,
        recover_automatic_receipts: bool = True,
    ) -> _PreparedDigestion:
        recovered_change_ids = (
            self._recover_automatic_receipts(atomic_information_id)
            if recover_automatic_receipts
            else ()
        )
        atomic_information = self.atomic_information_store.get_current(
            atomic_information_id
        )
        resolved_ids, unmatched, ambiguous = self._match_concerns(
            atomic_information.raw_concerns
        )
        all_related_information = self._all_related_information(
            resolved_ids, atomic_information.atomic_information_id
        )
        related_information = all_related_information[:MAX_RELATED_INFORMATION]
        world_state = DigestionWorldState(
            resolved_objects=tuple(
                self.resolver.resolve(object_id) for object_id in resolved_ids
            ),
            unmatched_concerns=unmatched,
            ambiguous_concerns=ambiguous,
            related_atomic_information=related_information,
        )
        return _PreparedDigestion(
            atomic_information=atomic_information,
            recovered_change_ids=recovered_change_ids,
            resolved_ids=resolved_ids,
            unmatched=unmatched,
            ambiguous=ambiguous,
            all_related_information=all_related_information,
            related_information=related_information,
            world_state=world_state,
        )

    def _apply_prepared(
        self,
        prepared: _PreparedDigestion,
        interpretation: InterpretationResult,
    ) -> DigestionResult:
        atomic_information = prepared.atomic_information
        recovered_change_ids = prepared.recovered_change_ids
        resolved_ids = prepared.resolved_ids
        unmatched = prepared.unmatched
        ambiguous = prepared.ambiguous
        all_related_information = prepared.all_related_information
        related_information = prepared.related_information
        self._validate_interpretation(interpretation)
        self._validate_claim_enrichment(
            interpretation.claim, atomic_information, resolved_ids
        )
        interpretation_fingerprint = self._interpretation_fingerprint(interpretation)

        claim_update_conflict = (
            atomic_information.claim is not None
            and interpretation.claim is not None
            and atomic_information.claim != interpretation.claim
        )
        atomic_information, binding_change_ids, information_changed = (
            self._enrich_and_bind(
                atomic_information,
                resolved_ids,
                None if claim_update_conflict else interpretation.claim,
                interpretation_fingerprint,
            )
        )
        conflicting_information = self._claim_conflicts(
            atomic_information, all_related_information
        )
        claim_conflict = claim_update_conflict or bool(conflicting_information)
        before_state_fingerprint = self._before_state_fingerprint(
            self._relevant_existing_ids(resolved_ids, interpretation.operations)
        )

        if self._requires_human_judgment(
            interpretation,
            resolved_ids,
            unmatched,
            ambiguous,
            claim_conflict,
        ):
            proposal = self._create_proposal(
                atomic_information,
                interpretation,
                interpretation_fingerprint,
                resolved_ids,
                before_state_fingerprint,
                (
                    conflicting_information
                    if conflicting_information
                    else related_information
                ),
                claim_conflict,
            )
            return DigestionResult(
                atomic_information=atomic_information,
                status=proposal.status,
                change_ids=(*recovered_change_ids, *binding_change_ids),
                proposal_id=proposal.proposal_id,
            )

        operation_change_ids = self._apply_operations(
            atomic_information,
            interpretation.operations,
            interpretation_fingerprint,
            mode="automatic",
            proposal_id=None,
            expected_before_state=before_state_fingerprint,
            resolved_ids=resolved_ids,
            expected_atomic_revision_id=atomic_information.revision_id,
        )
        return DigestionResult(
            atomic_information=atomic_information,
            status=(
                "automatic"
                if recovered_change_ids
                or information_changed
                or binding_change_ids
                or operation_change_ids
                else "already_processed"
            ),
            change_ids=(
                *recovered_change_ids,
                *binding_change_ids,
                *operation_change_ids,
            ),
            proposal_id=None,
        )

    def list_pending(self) -> tuple[ChangeProposal, ...]:
        """Return every unresolved proposal, including deferred proposals."""
        return self.proposal_store.list_unresolved()

    def render_pending(self) -> tuple[str, ...]:
        return tuple(
            self.human_judgment.render(proposal)
            for proposal in self.proposal_store.list_unresolved()
        )

    def decide(self, proposal_id: str, decision: str) -> DigestionResult:
        normalized = self.human_judgment.normalize_decision(decision)
        if normalized not in {"approve", "reject", "defer"}:
            raise ValueError("decision is not supported for this Change Proposal")
        requested_status = {
            "approve": "approved",
            "reject": "rejected",
            "defer": "deferred",
        }[normalized]
        proposal = self.proposal_store.get(proposal_id)
        current_information = self.atomic_information_store.get_current(
            proposal.atomic_information_id
        )
        apply_id = self._apply_identity(
            proposal.atomic_information_revision_id,
            proposal.interpretation_fingerprint,
            "human_approved",
            proposal.proposal_id,
        )
        receipt = self.world_model_repository.get_apply_receipt(apply_id)
        claim_correction_applied = self._claim_correction_applied(
            proposal, current_information
        )

        if receipt is not None:
            if normalized != "approve":
                raise ValueError(
                    "The approved change was already committed and must be recovered."
                )
            if proposal.status == "rejected":
                raise ValueError(
                    "Change Proposal state conflicts with its committed approval."
                )

        if proposal.status in {"approved", "rejected"}:
            if proposal.status != requested_status:
                raise ValueError("Change Proposal already has a different decision")
            return DigestionResult(
                atomic_information=current_information,
                status=proposal.status,
                change_ids=tuple(
                    item.change_id
                    for item in self.change_journal.list_changes()
                    if item.proposal_id == proposal.proposal_id
                ),
                proposal_id=proposal.proposal_id,
            )

        if claim_correction_applied and normalized != "approve":
            raise ValueError(
                "The approved Claim correction was already applied and must be recovered."
            )

        if normalized in {"reject", "defer"}:
            if proposal.status == requested_status:
                return DigestionResult(
                    atomic_information=current_information,
                    status=proposal.status,
                    change_ids=(),
                    proposal_id=proposal.proposal_id,
                )
            decided = self.proposal_store.update(
                replace(
                    proposal,
                    status=requested_status,
                    decided_at=None if normalized == "defer" else self.clock(),
                )
            )
            return DigestionResult(
                atomic_information=current_information,
                status=decided.status,
                change_ids=(),
                proposal_id=decided.proposal_id,
            )

        if (
            current_information.revision_id != proposal.atomic_information_revision_id
            and receipt is None
            and not claim_correction_applied
        ):
            raise ValueError(
                "Atomic Information changed; run digestion and review again."
            )
        change_ids = self._apply_operations(
            current_information,
            proposal.proposed_operations,
            proposal.interpretation_fingerprint,
            mode="human_approved",
            proposal_id=proposal.proposal_id,
            expected_before_state=proposal.before_state_fingerprint,
            resolved_ids=proposal.resolved_object_ids,
            expected_atomic_revision_id=proposal.atomic_information_revision_id,
        )
        current_information = self.atomic_information_store.get_current(
            proposal.atomic_information_id
        )
        current_information = self._apply_claim_correction(
            proposal, current_information
        )
        self.proposal_store.update(
            replace(proposal, status="approved", decided_at=self.clock())
        )
        if not change_ids:
            change_ids = tuple(
                item.change_id
                for item in self.change_journal.list_changes()
                if item.proposal_id == proposal.proposal_id
            )
        return DigestionResult(
            atomic_information=current_information,
            status="approved",
            change_ids=change_ids,
            proposal_id=proposal.proposal_id,
        )

    def _match_concerns(
        self, concerns: tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        names: dict[str, set[str]] = {}
        for record in self.world_model_repository.list_objects():
            if record.status != "active":
                continue
            for assignment in self.world_model_repository.list_names(record.object_id):
                names.setdefault(_normalize_name(assignment.name), set()).add(
                    record.object_id
                )

        resolved: set[str] = set()
        unmatched: list[str] = []
        ambiguous: list[str] = []
        for concern in concerns:
            matches = names.get(_normalize_name(concern), set())
            if len(matches) == 1:
                resolved.update(matches)
            elif not matches:
                unmatched.append(concern)
            else:
                ambiguous.append(concern)
        return tuple(sorted(resolved)), tuple(unmatched), tuple(ambiguous)

    def _all_related_information(
        self,
        resolved_ids: tuple[str, ...],
        current_atomic_information_id: str,
    ) -> tuple[AtomicInformationRevision, ...]:
        if not resolved_ids:
            return ()
        targets = set(resolved_ids)
        related = tuple(
            item
            for item in self.atomic_information_store.list_atomic_information()
            if item.atomic_information_id != current_atomic_information_id
            and targets.intersection(item.related_object_ids)
        )
        return tuple(
            sorted(
                related,
                key=lambda item: (item.created_at, item.atomic_information_id),
                reverse=True,
            )
        )

    @staticmethod
    def _claim_conflicts(
        atomic_information: AtomicInformationRevision,
        related_information: tuple[AtomicInformationRevision, ...],
    ) -> tuple[AtomicInformationRevision, ...]:
        claim = atomic_information.claim
        if claim is None or claim.stance == "uncertain":
            return ()
        opposing = {"assert": "deny", "deny": "assert"}
        statement = _normalize_name(atomic_information.statement)
        return tuple(
            item
            for item in related_information
            if item.claim is not None
            and item.claim.stance == opposing[claim.stance]
            and _normalize_name(item.statement) == statement
        )

    def _validate_claim_enrichment(
        self,
        claim: ClaimAttribution | None,
        atomic_information: AtomicInformationRevision,
        resolved_ids: tuple[str, ...],
    ) -> None:
        if claim is None:
            return
        validate_claim_attribution(claim, "interpretation.claim")
        if claim.claimant_source_id not in {
            item.source_id for item in atomic_information.source_evidence
        }:
            raise ValueError(
                "interpretation.claim.claimant_source_id must reference Evidence"
            )
        if (
            claim.claimant_object_id is not None
            and claim.claimant_object_id not in resolved_ids
        ):
            raise ValueError(
                "interpretation.claim.claimant_object_id must be uniquely resolved"
            )

    def _claim_correction_applied(
        self,
        proposal: ChangeProposal,
        current_information: AtomicInformationRevision,
    ) -> bool:
        if proposal.proposed_claim is None:
            return False
        base_revision = next(
            (
                item
                for item in self.atomic_information_store.list_revisions(
                    proposal.atomic_information_id
                )
                if item.revision_id == proposal.atomic_information_revision_id
            ),
            None,
        )
        if base_revision is None or base_revision.claim == proposal.proposed_claim:
            return False
        return current_information.claim == proposal.proposed_claim

    def _apply_claim_correction(
        self,
        proposal: ChangeProposal,
        current_information: AtomicInformationRevision,
    ) -> AtomicInformationRevision:
        proposed_claim = proposal.proposed_claim
        if proposed_claim is None or current_information.claim == proposed_claim:
            return current_information
        corrected = replace(
            current_information,
            revision_number=current_information.revision_number + 1,
            revision_id=(
                f"{current_information.atomic_information_id}-"
                f"r{current_information.revision_number + 1:04d}"
            ),
            claim=proposed_claim,
            created_at=self.clock(),
            revision_reason="human_approved_claim_correction",
        )
        return self.atomic_information_store.append_revision(corrected)

    def _enrich_and_bind(
        self,
        atomic_information: AtomicInformationRevision,
        resolved_ids: tuple[str, ...],
        claim: ClaimAttribution | None,
        interpretation_fingerprint: str,
    ) -> tuple[AtomicInformationRevision, tuple[str, ...], bool]:
        related_ids = tuple(
            sorted(set(atomic_information.related_object_ids) | set(resolved_ids))
        )
        enriched_claim = atomic_information.claim or claim
        binding_changed = related_ids != atomic_information.related_object_ids
        claim_changed = enriched_claim != atomic_information.claim
        if not binding_changed and not claim_changed:
            return atomic_information, (), False
        revised = replace(
            atomic_information,
            revision_number=atomic_information.revision_number + 1,
            revision_id=(
                f"{atomic_information.atomic_information_id}-"
                f"r{atomic_information.revision_number + 1:04d}"
            ),
            related_object_ids=related_ids,
            claim=enriched_claim,
            created_at=self.clock(),
            revision_reason=(
                "claim_enrichment_and_object_binding"
                if binding_changed and claim_changed
                else "object_binding"
                if binding_changed
                else "claim_enrichment"
            ),
        )
        self.atomic_information_store.append_revision(revised)
        if not binding_changed:
            return revised, (), True
        change_id = _digest_id(
            "change",
            revised.revision_id,
            interpretation_fingerprint,
            "bind_atomic_information",
        )
        existing = self.change_journal.get(change_id)
        if existing is None:
            now = self.clock()
            self.change_journal.append(
                ChangeJournalRecord(
                    change_id=change_id,
                    atomic_information_id=revised.atomic_information_id,
                    atomic_information_revision_id=revised.revision_id,
                    operation="bind_atomic_information",
                    resolved_object_ids=related_ids,
                    interpretation_fingerprint=interpretation_fingerprint,
                    mode="automatic",
                    proposal_id=None,
                    status="applied",
                    created_at=now,
                    applied_at=now,
                    error_code=None,
                )
            )
        return revised, (change_id,), True

    def _requires_human_judgment(
        self,
        interpretation: InterpretationResult,
        resolved_ids: tuple[str, ...],
        unmatched: tuple[str, ...],
        ambiguous: tuple[str, ...],
        claim_conflict: bool,
    ) -> bool:
        if (
            interpretation.conflict
            or interpretation.ambiguous
            or not interpretation.evidence_sufficient
            or ambiguous
            or claim_conflict
        ):
            return True
        structural = tuple(
            operation
            for operation in interpretation.operations
            if operation.kind != "no_structural_change"
        )
        if unmatched and structural:
            return True
        return any(
            not self._operation_is_safe(operation, resolved_ids)
            for operation in interpretation.operations
        )

    def _operation_is_safe(
        self,
        operation: WorldModelOperation,
        resolved_ids: tuple[str, ...],
    ) -> bool:
        resolved = set(resolved_ids)
        if operation.kind == "no_structural_change":
            return True
        if operation.kind == "add_role":
            return (
                operation.target_object_id in resolved
                and operation.role in ALLOWED_ROLES
            )
        if operation.kind == "rename":
            if operation.target_object_id not in resolved or not operation.name:
                return False
            owners = self._active_name_owners(operation.name)
            return not owners or owners == {operation.target_object_id}
        if operation.kind == "set_lifecycle":
            return (
                operation.target_object_id in resolved
                and bool(operation.lifecycle_state)
                and not self._lifecycle_conflicts(operation)
            )
        if operation.kind == "create_relationship":
            return (
                operation.target_object_id in resolved
                and operation.secondary_object_id in resolved
                and operation.target_object_id != operation.secondary_object_id
                and operation.relation in ALLOWED_RELATIONSHIPS
            )
        return False

    def _active_name_owners(self, name: str) -> set[str]:
        normalized = _normalize_name(name)
        return {
            record.object_id
            for record in self.world_model_repository.list_objects()
            if record.status == "active"
            for assignment in self.world_model_repository.list_names(record.object_id)
            if _normalize_name(assignment.name) == normalized
        }

    def _lifecycle_conflicts(self, operation: WorldModelOperation) -> bool:
        if operation.target_object_id is None:
            return True
        active = self.world_model_repository.list_lifecycles(
            operation.target_object_id, active_only=True
        )
        if len(active) > 1:
            return True
        if not active:
            return False
        current = active[0]
        proposed = {
            "state": operation.lifecycle_state,
            "start_at": operation.start_at,
            "actual_end_at": operation.actual_end_at,
            "target_end_at": operation.target_end_at,
            "completion_condition": operation.completion_condition,
        }
        return any(
            value is not None
            and getattr(current, field) is not None
            and getattr(current, field) != value
            for field, value in proposed.items()
        )

    def _recover_automatic_receipts(
        self, atomic_information_id: str
    ) -> tuple[str, ...]:
        recovered: list[str] = []
        for receipt in self.world_model_repository.list_apply_receipts():
            records, created_object_ids = self._parse_apply_receipt(receipt.payload)
            first = records[0]
            if (
                first.mode != "automatic"
                or first.atomic_information_id != atomic_information_id
            ):
                continue
            recovered.extend(self._finalize_apply_receipt(records, created_object_ids))
        return tuple(dict.fromkeys(recovered))

    def _apply_operations(
        self,
        atomic_information: AtomicInformationRevision,
        operations: tuple[WorldModelOperation, ...],
        interpretation_fingerprint: str,
        *,
        mode: str,
        proposal_id: str | None,
        expected_before_state: str,
        resolved_ids: tuple[str, ...],
        expected_atomic_revision_id: str,
    ) -> tuple[str, ...]:
        actionable = tuple(
            operation
            for operation in operations
            if operation.kind != "no_structural_change"
        )
        planned: list[tuple[str, WorldModelOperation]] = []
        for index, operation in enumerate(actionable, start=1):
            change_id = _digest_id(
                "change",
                expected_atomic_revision_id,
                interpretation_fingerprint,
                str(index),
                operation.kind,
            )
            planned.append((change_id, operation))

        if not planned:
            return ()

        apply_id = self._apply_identity(
            expected_atomic_revision_id,
            interpretation_fingerprint,
            mode,
            proposal_id,
        )
        receipt = self.world_model_repository.get_apply_receipt(apply_id)
        expected_change_ids = tuple(change_id for change_id, _ in planned)
        if receipt is not None:
            records, created_object_ids = self._parse_apply_receipt(receipt.payload)
            if tuple(item.change_id for item in records) != expected_change_ids:
                raise ValueError("apply receipt does not match the requested change")
            return self._finalize_apply_receipt(records, created_object_ids)

        current_information = self.atomic_information_store.get_current(
            atomic_information.atomic_information_id
        )
        if current_information.revision_id != expected_atomic_revision_id:
            raise ValueError(
                "Atomic Information changed; run digestion and review again."
            )
        relevant_ids = self._relevant_existing_ids(resolved_ids, operations)
        if self._before_state_fingerprint(relevant_ids) != expected_before_state:
            raise ValueError(
                "The underlying business information changed; review this suggestion again."
            )

        existing_records = tuple(
            self.change_journal.get(change_id) for change_id, _ in planned
        )
        if all(existing_records):
            return ()
        if any(existing_records):
            raise ValueError("Change Journal is incomplete and has no apply receipt")

        affected_by_change: list[tuple[str, tuple[str, ...]]] = []
        created_object_ids: list[str] = []
        now = self.clock()
        records: list[ChangeJournalRecord] = []
        with self.world_model_repository.transaction():
            for change_id, operation in planned:
                affected, created_object_id = self._apply_operation(
                    operation,
                    atomic_information,
                    mode=mode,
                )
                affected_by_change.append((change_id, affected))
                if created_object_id is not None:
                    created_object_ids.append(created_object_id)
            for (change_id, operation), (_, affected) in zip(
                planned, affected_by_change, strict=True
            ):
                records.append(
                    ChangeJournalRecord(
                        change_id=change_id,
                        atomic_information_id=atomic_information.atomic_information_id,
                        atomic_information_revision_id=expected_atomic_revision_id,
                        operation=operation.kind,
                        resolved_object_ids=affected,
                        interpretation_fingerprint=interpretation_fingerprint,
                        mode=mode,
                        proposal_id=proposal_id,
                        status="applied",
                        created_at=now,
                        applied_at=now,
                        error_code=None,
                    )
                )
            payload = json.dumps(
                {
                    "version": 1,
                    "records": [journal_to_dict(item) for item in records],
                    "created_object_ids": created_object_ids,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.world_model_repository.put_apply_receipt(apply_id, payload)

        return self._finalize_apply_receipt(tuple(records), tuple(created_object_ids))

    @staticmethod
    def _apply_identity(
        atomic_information_revision_id: str,
        interpretation_fingerprint: str,
        mode: str,
        proposal_id: str | None,
    ) -> str:
        return _digest_id(
            "apply",
            atomic_information_revision_id,
            interpretation_fingerprint,
            mode,
            proposal_id or "automatic",
        )

    @staticmethod
    def _parse_apply_receipt(
        payload: str,
    ) -> tuple[tuple[ChangeJournalRecord, ...], tuple[str, ...]]:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("apply receipt is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != {
            "version",
            "records",
            "created_object_ids",
        }:
            raise ValueError("apply receipt does not match its schema")
        if value["version"] != 1 or not isinstance(value["records"], list):
            raise ValueError("apply receipt version or records are invalid")
        created_object_ids = value["created_object_ids"]
        if not isinstance(created_object_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in created_object_ids
        ):
            raise ValueError("apply receipt created Object identities are invalid")
        records = tuple(
            journal_from_dict(item, f"apply receipt record[{index}]")
            for index, item in enumerate(value["records"], start=1)
        )
        if not records:
            raise ValueError("apply receipt must contain at least one change")
        first = records[0]
        if any(
            item.atomic_information_id != first.atomic_information_id
            or item.atomic_information_revision_id
            != first.atomic_information_revision_id
            or item.mode != first.mode
            or item.proposal_id != first.proposal_id
            for item in records[1:]
        ):
            raise ValueError("apply receipt records do not share one apply identity")
        return records, tuple(created_object_ids)

    def _finalize_apply_receipt(
        self,
        records: tuple[ChangeJournalRecord, ...],
        created_object_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not records:
            raise ValueError("apply receipt must contain at least one change")
        current = self.atomic_information_store.get_current(
            records[0].atomic_information_id
        )
        missing_binding = any(
            object_id not in current.related_object_ids
            for object_id in created_object_ids
        )
        missing_journal = any(
            self.change_journal.get(record.change_id) is None for record in records
        )
        if not missing_binding and not missing_journal:
            return ()
        for object_id in created_object_ids:
            self._bind_new_object(current, object_id)
        for record in records:
            self.change_journal.append(record)
        return tuple(record.change_id for record in records)

    def _apply_operation(
        self,
        operation: WorldModelOperation,
        atomic_information: AtomicInformationRevision,
        *,
        mode: str,
    ) -> tuple[tuple[str, ...], str | None]:
        if operation.kind == "no_structural_change":
            return tuple(sorted(atomic_information.related_object_ids)), None
        if operation.kind == "add_role":
            target = self._required_target(operation)
            if operation.role not in ALLOWED_ROLES:
                raise ValueError("The suggested business role is not approved.")
            active_roles = {
                item.role
                for item in self.world_model_repository.list_roles(
                    target, active_only=True
                )
            }
            if operation.role not in active_roles:
                self.world_model_repository.add_role(
                    target,
                    operation.role,
                    source_atomic_information_id=(
                        atomic_information.atomic_information_id
                    ),
                    confidence=None,
                )
            return (target,), None
        if operation.kind == "end_role":
            if mode != "human_approved":
                raise ValueError("Ending a business role requires human approval.")
            target = self._required_target(operation)
            if operation.role not in ALLOWED_ROLES:
                raise ValueError("The suggested business role is not approved.")
            self.world_model_repository.end_role(target, operation.role)
            return (target,), None
        if operation.kind == "rename":
            target = self._required_target(operation)
            if not operation.name:
                raise ValueError("A suggested name is required.")
            current = self.resolver.resolve(target)
            if _normalize_name(current.current_name) != _normalize_name(operation.name):
                self.world_model_repository.rename_object(target, operation.name)
            return (target,), None
        if operation.kind == "set_lifecycle":
            target = self._required_target(operation)
            if not operation.lifecycle_state:
                raise ValueError("A Lifecycle state is required.")
            active = self.world_model_repository.list_lifecycles(
                target, active_only=True
            )
            current = active[0] if active else None
            values = {
                "state": operation.lifecycle_state,
                "start_at": operation.start_at,
                "actual_end_at": operation.actual_end_at,
                "target_end_at": operation.target_end_at,
                "completion_condition": operation.completion_condition,
            }
            if current is not None:
                for field in (
                    "start_at",
                    "actual_end_at",
                    "target_end_at",
                    "completion_condition",
                ):
                    if values[field] is None:
                        values[field] = getattr(current, field)
                if all(getattr(current, key) == value for key, value in values.items()):
                    return (target,), None
            self.world_model_repository.set_lifecycle(target, **values)
            return (target,), None
        if operation.kind == "create_relationship":
            target = self._required_target(operation)
            secondary = operation.secondary_object_id
            if secondary is None:
                raise ValueError("A second business record is required.")
            if operation.relation not in ALLOWED_RELATIONSHIPS:
                raise ValueError("The suggested business relationship is not approved.")
            existing = self._find_relationship(target, operation.relation, secondary)
            if existing is None:
                self.world_model_repository.create_relationship(
                    target,
                    operation.relation,
                    secondary,
                    source_atomic_information_id=(
                        atomic_information.atomic_information_id
                    ),
                    confidence=None,
                )
            return tuple(sorted((target, secondary))), None
        if operation.kind == "end_relationship":
            if mode != "human_approved" or operation.relationship_id is None:
                raise ValueError(
                    "Ending a business relationship requires human approval."
                )
            relationship = self.world_model_repository.end_relationship(
                operation.relationship_id
            )
            return (
                tuple(sorted((relationship.from_object_id, relationship.to_object_id))),
                None,
            )
        if operation.kind == "new_object":
            if mode != "human_approved":
                raise ValueError("Creating a new business record requires approval.")
            if not operation.name or operation.role not in ALLOWED_ROLES:
                raise ValueError(
                    "A new business record needs an approved name and business role."
                )
            owners = self._active_name_owners(operation.name)
            current_information = self.atomic_information_store.get_current(
                atomic_information.atomic_information_id
            )
            if owners:
                if len(owners) != 1 or not owners.issubset(
                    current_information.related_object_ids
                ):
                    raise ValueError(
                        "A business record with this name now exists; review the "
                        "suggestion again."
                    )
                created_object_id = next(iter(owners))
            else:
                created_object_id = self.world_model_repository.create_object(
                    operation.name, roles=(operation.role,)
                ).object_id
            affected = [created_object_id]
            if operation.secondary_object_id is not None:
                if operation.relation not in ALLOWED_RELATIONSHIPS:
                    raise ValueError(
                        "The suggested business relationship is not approved."
                    )
                if (
                    self._find_relationship(
                        created_object_id,
                        operation.relation,
                        operation.secondary_object_id,
                    )
                    is None
                ):
                    self.world_model_repository.create_relationship(
                        created_object_id,
                        operation.relation,
                        operation.secondary_object_id,
                        source_atomic_information_id=(
                            atomic_information.atomic_information_id
                        ),
                        confidence=None,
                    )
                affected.append(operation.secondary_object_id)
            needs_binding = (
                created_object_id not in current_information.related_object_ids
            )
            return (
                tuple(sorted(affected)),
                created_object_id if needs_binding else None,
            )
        if operation.kind == "delete_object":
            if mode != "human_approved":
                raise ValueError("Removing a business record requires approval.")
            target = self._required_target(operation)
            relationships = self.world_model_repository.list_relationships(
                object_id=target, active_only=True
            )
            for relationship in relationships:
                neighbor = (
                    relationship.to_object_id
                    if relationship.from_object_id == target
                    else relationship.from_object_id
                )
                remaining = tuple(
                    item
                    for item in self.world_model_repository.list_relationships(
                        object_id=neighbor, active_only=True
                    )
                    if item.relationship_id != relationship.relationship_id
                )
                if not remaining:
                    raise ValueError(
                        "Removing this record would leave a related business record "
                        "without any active connection."
                    )
            for relationship in relationships:
                self.world_model_repository.end_relationship(
                    relationship.relationship_id
                )
            self.world_model_repository.set_object_status(target, "deleted")
            return (target,), None
        raise ValueError("This suggestion cannot be executed safely.")

    def _bind_new_object(
        self,
        atomic_information: AtomicInformationRevision,
        object_id: str,
    ) -> None:
        current = self.atomic_information_store.get_current(
            atomic_information.atomic_information_id
        )
        if object_id in current.related_object_ids:
            return
        related_ids = tuple(sorted((*current.related_object_ids, object_id)))
        self.atomic_information_store.append_revision(
            replace(
                current,
                revision_number=current.revision_number + 1,
                revision_id=(
                    f"{current.atomic_information_id}-"
                    f"r{current.revision_number + 1:04d}"
                ),
                related_object_ids=related_ids,
                created_at=self.clock(),
                revision_reason="human_approved_object_binding",
            )
        )

    def _find_relationship(self, first: str, relation: str, second: str):
        return next(
            (
                item
                for item in self.world_model_repository.list_relationships(
                    object_id=first, active_only=True
                )
                if item.relation == relation
                and item.from_object_id == first
                and item.to_object_id == second
            ),
            None,
        )

    @staticmethod
    def _required_target(operation: WorldModelOperation) -> str:
        if operation.target_object_id is None:
            raise ValueError("A target business record is required.")
        return operation.target_object_id

    def _create_proposal(
        self,
        atomic_information: AtomicInformationRevision,
        interpretation: InterpretationResult,
        interpretation_fingerprint: str,
        resolved_ids: tuple[str, ...],
        before_state_fingerprint: str,
        related_information: tuple[AtomicInformationRevision, ...],
        claim_conflict: bool,
    ) -> ChangeProposal:
        operations = interpretation.operations or (
            WorldModelOperation(kind="unresolved"),
        )
        proposal_id = _digest_id(
            "proposal",
            atomic_information.revision_id,
            interpretation_fingerprint,
        )
        claim_summary = self._claim_summary(
            atomic_information,
            related_information,
            include_related=claim_conflict or interpretation.conflict,
            proposed_claim=interpretation.claim,
        )
        proposal = ChangeProposal(
            proposal_id=proposal_id,
            atomic_information_id=atomic_information.atomic_information_id,
            atomic_information_revision_id=atomic_information.revision_id,
            proposed_operations=operations,
            resolved_object_ids=resolved_ids,
            rationale=interpretation.rationale,
            supporting_evidence_refs=self._evidence_refs(atomic_information),
            before_state_fingerprint=before_state_fingerprint,
            interpretation_fingerprint=interpretation_fingerprint,
            human_review=self._human_review(
                atomic_information, interpretation, operations, claim_summary
            ),
            status="pending",
            created_at=self.clock(),
            decided_at=None,
            claim_summary=claim_summary,
            proposed_claim=interpretation.claim,
        )
        return self.proposal_store.add_pending(proposal)

    def _human_review(
        self,
        atomic_information: AtomicInformationRevision,
        interpretation: InterpretationResult,
        operations: tuple[WorldModelOperation, ...],
        claim_summary: str | None,
    ) -> HumanReviewContent:
        kinds = {operation.kind for operation in operations}
        if interpretation.conflict or "conflict" in kinds:
            finding = "新信息与当前可信记录可能不能同时成立。"
            importance = "直接覆盖可能让后续判断建立在互相矛盾的信息上。"
        elif "new_object" in kinds:
            finding = "新信息可能涉及一项值得长期单独保存的业务事项。"
            importance = "建立独立记录会影响以后如何持续关联和理解这项事项。"
        elif "delete_object" in kinds:
            finding = "新信息建议不再把一项现有业务事项视为有效记录。"
            importance = "停用后仍需保留历史，并避免破坏其他事项的业务联系。"
        else:
            finding = "系统无法在不猜测的情况下确认应如何调整现有业务记录。"
            importance = "人工确认可以避免把含义不清的信息误写为长期认知。"

        recommendation = self._recommendation(operations)
        evidence = "；".join(self._evidence_refs(atomic_information))
        if claim_summary:
            evidence = f"{claim_summary}；{evidence}"
        isolated = any(
            operation.kind == "new_object" and operation.secondary_object_id is None
            for operation in operations
        )
        consequences = (
            "批准后会建立一项暂时没有明确业务联系的独立记录；拒绝会保持现状；"
            "稍后决定会保留建议等待补充依据。"
            if isolated
            else "批准后会按建议更新并保留历史；拒绝会保持现状；稍后决定会保留建议等待补充依据。"
        )
        return HumanReviewContent(
            finding=finding,
            importance=importance,
            recommendation=recommendation,
            evidence=evidence,
            consequences=consequences,
        )

    @staticmethod
    def _claim_summary(
        atomic_information: AtomicInformationRevision,
        related_information: tuple[AtomicInformationRevision, ...],
        *,
        include_related: bool,
        proposed_claim: ClaimAttribution | None,
    ) -> str | None:
        items = (
            (atomic_information, *related_information)
            if include_related
            else (atomic_information,)
        )
        summaries: list[str] = []
        for item in items:
            if item.claim is None:
                continue
            claimant = (
                item.claim.claimant_label
                or item.claim.claimant_source_id
                or "未标明来源"
            )
            stance = {
                "assert": "主张",
                "deny": "否认",
                "uncertain": "表示不确定",
            }[item.claim.stance]
            summaries.append(f"{claimant}{stance}：{item.statement}")
        if proposed_claim is not None and proposed_claim != atomic_information.claim:
            claimant = (
                proposed_claim.claimant_label
                or proposed_claim.claimant_source_id
                or "未标明来源"
            )
            stance = {
                "assert": "主张",
                "deny": "否认",
                "uncertain": "表示不确定",
            }[proposed_claim.stance]
            summaries.append(
                f"建议修订归因为{claimant}{stance}：{atomic_information.statement}"
            )
        return "；".join(summaries) if summaries else None

    def _recommendation(self, operations: tuple[WorldModelOperation, ...]) -> str:
        descriptions: list[str] = []
        for operation in operations:
            if operation.kind == "new_object" and operation.name:
                descriptions.append(f"长期保存“{operation.name}”这项业务事项")
            elif operation.kind == "delete_object":
                descriptions.append("停用相关业务事项，同时保留历史记录")
            elif operation.kind == "rename" and operation.name:
                descriptions.append(f"把相关事项的当前名称调整为“{operation.name}”")
            elif operation.kind == "add_role" and operation.role:
                descriptions.append(f"补充“{operation.role}”这一业务身份")
            elif operation.kind == "set_lifecycle":
                descriptions.append("补充或调整这项事项的推进时间与状态")
            elif operation.kind == "create_relationship":
                descriptions.append("保存两项业务事项之间明确的长期联系")
            else:
                descriptions.append("先确认信息对应的业务事项和正确处理方式")
        return "；".join(descriptions)

    @staticmethod
    def _evidence_refs(
        atomic_information: AtomicInformationRevision,
    ) -> tuple[str, ...]:
        return tuple(
            f"{item.artifact} 第 {item.segment} 段：{item.excerpt}"
            for item in atomic_information.source_evidence
        )

    def _relevant_existing_ids(
        self,
        resolved_ids: tuple[str, ...],
        operations: tuple[WorldModelOperation, ...],
    ) -> tuple[str, ...]:
        candidates = set(resolved_ids)
        for operation in operations:
            if operation.target_object_id:
                candidates.add(operation.target_object_id)
            if operation.secondary_object_id:
                candidates.add(operation.secondary_object_id)
        existing: list[str] = []
        for object_id in sorted(candidates):
            try:
                self.world_model_repository.get_object(object_id)
            except ValueError:
                continue
            existing.append(object_id)
        return tuple(existing)

    def _before_state_fingerprint(self, object_ids: tuple[str, ...]) -> str:
        relationships = {
            item.relationship_id: asdict(item)
            for object_id in object_ids
            for item in self.world_model_repository.list_relationships(
                object_id=object_id, active_only=False
            )
        }
        payload = {
            "objects": [
                {
                    "record": asdict(self.world_model_repository.get_object(object_id)),
                    "names": [
                        asdict(item)
                        for item in self.world_model_repository.list_names(object_id)
                    ],
                    "roles": [
                        asdict(item)
                        for item in self.world_model_repository.list_roles(object_id)
                    ],
                    "lifecycles": [
                        asdict(item)
                        for item in self.world_model_repository.list_lifecycles(
                            object_id
                        )
                    ],
                }
                for object_id in object_ids
            ],
            "relationships": [relationships[key] for key in sorted(relationships)],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _interpretation_fingerprint(
        interpretation: InterpretationResult,
    ) -> str:
        payload = {
            "operations": [
                operation_to_dict(item) for item in interpretation.operations
            ],
            "rationale": interpretation.rationale,
            "evidence_sufficient": interpretation.evidence_sufficient,
            "conflict": interpretation.conflict,
            "ambiguous": interpretation.ambiguous,
            "claim": (
                None
                if interpretation.claim is None
                else claim_to_dict(interpretation.claim)
            ),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _validate_interpretation(interpretation: InterpretationResult) -> None:
        if not isinstance(interpretation, InterpretationResult):
            raise TypeError("interpretation must be an InterpretationResult")
        if not interpretation.operations:
            raise ValueError("interpretation must contain at least one operation")
        if not isinstance(interpretation.rationale, str) or not (
            interpretation.rationale.strip()
        ):
            raise ValueError("interpretation rationale must not be empty")
        for name in ("evidence_sufficient", "conflict", "ambiguous"):
            if not isinstance(getattr(interpretation, name), bool):
                raise TypeError(f"interpretation {name} must be a boolean")
        for index, operation in enumerate(interpretation.operations, start=1):
            validate_operation(operation, f"operation[{index}]")
