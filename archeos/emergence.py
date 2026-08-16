from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .atomic_information import AtomicInformationRevision, AtomicInformationStore
from .digestion import (
    ChangeJournal,
    ChangeJournalRecord,
    ChangeProposal,
    ChangeProposalStore,
    HumanJudgmentPort,
    HumanReviewContent,
    WorldModelOperation,
)
from .digestion.serialization import journal_from_dict, journal_to_dict
from .world_model import WorldModelRepository

IDENTITY_OUTCOMES = frozenset(
    {"bind_existing", "create_minimal", "accumulate", "human_review", "no_object"}
)
IDENTITY_KINDS = frozenset(
    {"long_lived", "action", "topic", "attribute", "pronoun", "unresolved_speaker"}
)
HUMAN_IDENTITY_ACTIONS = frozenset(
    {"bind_existing", "create_minimal", "edit_identity_and_create", "reject", "defer"}
)
IDENTITY_BASES = frozenset(
    {"stable_external_id", "repeated_consistent", "human_confirmed"}
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _digest_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]
    return f"{prefix}_{digest}"


@dataclass(frozen=True)
class IdentityEvidence:
    """Caller-supplied, non-durable evidence assessment for one identity gate run."""

    name: str | None
    supporting_revision_ids: tuple[str, ...]
    identity_bases: tuple[str, ...]
    identity_kind: str = "long_lived"
    stable_external_id: str | None = None
    approved_existing_object_id: str | None = None
    possible_existing_object_ids: tuple[str, ...] = ()
    long_lived: bool = True
    provenance_complete: bool = True
    low_consequence: bool = True
    requires_structure: bool = False
    identity_conflict: bool = False


@dataclass(frozen=True)
class IdentityAssessment:
    outcome: str
    atomic_information_id: str
    atomic_information_revision_id: str
    supporting_revision_ids: tuple[str, ...]
    name: str | None
    resolved_object_ids: tuple[str, ...]
    identity_fingerprint: str
    external_identity_key: str | None
    before_state_fingerprint: str


@dataclass(frozen=True)
class IdentityGateResult:
    outcome: str
    atomic_information: AtomicInformationRevision
    object_id: str | None
    change_ids: tuple[str, ...]
    proposal_id: str | None


class IdentityGateService:
    """Narrow, deterministic Object identity gate without a Candidate Store."""

    def __init__(
        self,
        atomic_information_store: AtomicInformationStore,
        world_model_repository: WorldModelRepository,
        proposal_store: ChangeProposalStore,
        change_journal: ChangeJournal,
        human_judgment: HumanJudgmentPort,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.atomic_information_store = atomic_information_store
        self.world_model_repository = world_model_repository
        self.proposal_store = proposal_store
        self.change_journal = change_journal
        self.human_judgment = human_judgment
        self.clock = clock

    def assess(
        self, atomic_information_id: str, evidence: IdentityEvidence
    ) -> IdentityAssessment:
        information = self.atomic_information_store.get_current(atomic_information_id)
        self._validate_evidence(information, evidence)
        fingerprint = self._identity_fingerprint(evidence)
        external_identity_key = self._external_identity_key(evidence)
        candidates = self._existing_candidates(evidence, external_identity_key)
        outcome, resolved = self._choose_outcome(information, evidence, candidates)
        return IdentityAssessment(
            outcome=outcome,
            atomic_information_id=information.atomic_information_id,
            atomic_information_revision_id=information.revision_id,
            supporting_revision_ids=tuple(sorted(evidence.supporting_revision_ids)),
            name=evidence.name,
            resolved_object_ids=resolved,
            identity_fingerprint=fingerprint,
            external_identity_key=external_identity_key,
            before_state_fingerprint=self._before_state_fingerprint(
                evidence.name, resolved, external_identity_key
            ),
        )

    def process(
        self, atomic_information_id: str, evidence: IdentityEvidence
    ) -> IdentityGateResult:
        fingerprint = self._identity_fingerprint(evidence)
        recovered = self._recover(atomic_information_id, fingerprint)
        if recovered is not None:
            return recovered
        assessment = self.assess(atomic_information_id, evidence)
        if assessment.outcome in {"bind_existing", "create_minimal"}:
            return self.apply(assessment)
        information = self.atomic_information_store.get_current(atomic_information_id)
        if assessment.outcome == "human_review":
            proposal = self._create_proposal(assessment, information)
            return IdentityGateResult(
                assessment.outcome, information, None, (), proposal.proposal_id
            )
        return IdentityGateResult(assessment.outcome, information, None, (), None)

    def apply(
        self,
        assessment: IdentityAssessment,
        *,
        mode: str = "automatic",
        proposal_id: str | None = None,
    ) -> IdentityGateResult:
        if assessment.outcome not in {"bind_existing", "create_minimal"}:
            raise ValueError(
                "only binding or minimal creation assessments can be applied"
            )
        if mode not in {"automatic", "human_approved"}:
            raise ValueError("Identity Gate apply mode is not supported")
        apply_id = self._apply_id(assessment, mode, proposal_id)
        receipt = self.world_model_repository.get_apply_receipt(apply_id)
        if receipt is not None:
            records, bound_object_ids = self._parse_receipt(receipt.payload)
            return self._finalize(
                records, bound_object_ids, assessment.outcome, proposal_id
            )

        information = self.atomic_information_store.get_current(
            assessment.atomic_information_id
        )
        if information.revision_id != assessment.atomic_information_revision_id:
            raise ValueError("Atomic Information revision is stale; assess again.")
        if (
            self._before_state_fingerprint(
                assessment.name,
                assessment.resolved_object_ids,
                assessment.external_identity_key,
            )
            != assessment.before_state_fingerprint
        ):
            raise ValueError("Object identity state changed; assess again.")

        change_id = _digest_id(
            "change",
            assessment.atomic_information_revision_id,
            assessment.identity_fingerprint,
            assessment.outcome,
            mode,
            proposal_id or "automatic",
        )
        existing = self.change_journal.get(change_id)
        if existing is not None:
            return IdentityGateResult(
                assessment.outcome,
                information,
                existing.resolved_object_ids[0]
                if existing.resolved_object_ids
                else None,
                (),
                proposal_id,
            )

        now = self.clock()
        with self.world_model_repository.transaction():
            mapped_object_id = self._mapped_object_id(assessment.external_identity_key)
            if assessment.outcome == "bind_existing":
                if len(assessment.resolved_object_ids) != 1:
                    raise ValueError(
                        "existing identity must resolve to exactly one Object"
                    )
                object_id = assessment.resolved_object_ids[0]
                self._require_active_object(object_id)
                if mapped_object_id is not None and mapped_object_id != object_id:
                    raise ValueError(
                        "external identity key conflicts with the resolved Object"
                    )
            else:
                if assessment.name is None:
                    raise ValueError(
                        "minimal Object creation requires an evidence-backed Name"
                    )
                if mapped_object_id is not None:
                    raise ValueError(
                        "external identity key is already mapped; assess again."
                    )
                if self._name_owners(assessment.name):
                    raise ValueError(
                        "an Object with this identity now exists; assess again."
                    )
                object_id = self.world_model_repository.create_object(
                    assessment.name, roles=()
                ).object_id
            if assessment.external_identity_key is not None:
                self.world_model_repository.put_external_identity_mapping(
                    assessment.external_identity_key, object_id
                )
            record = ChangeJournalRecord(
                change_id=change_id,
                atomic_information_id=assessment.atomic_information_id,
                atomic_information_revision_id=assessment.atomic_information_revision_id,
                operation=assessment.outcome,
                resolved_object_ids=(object_id,),
                interpretation_fingerprint=assessment.identity_fingerprint,
                mode=mode,
                proposal_id=proposal_id,
                status="applied",
                created_at=now,
                applied_at=now,
                error_code=None,
            )
            payload = json.dumps(
                {
                    "version": 1,
                    "records": [journal_to_dict(record)],
                    "created_object_ids": [object_id],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.world_model_repository.put_apply_receipt(apply_id, payload)
        return self._finalize((record,), (object_id,), assessment.outcome, proposal_id)

    def decide(
        self,
        proposal_id: str,
        action: str,
        *,
        object_id: str | None = None,
        name: str | None = None,
    ) -> IdentityGateResult:
        normalized = self.human_judgment.normalize_decision(action)
        if normalized not in HUMAN_IDENTITY_ACTIONS:
            raise ValueError("decision is not supported for an Identity Gate proposal")
        proposal = self.proposal_store.get(proposal_id)
        if proposal.human_review.allowed_actions != (
            "bind_existing",
            "create_minimal",
            "edit_identity_and_create",
            "reject",
            "defer",
        ):
            raise ValueError("Change Proposal is not an Identity Gate proposal")
        information = self.atomic_information_store.get_current(
            proposal.atomic_information_id
        )
        if normalized in {"reject", "defer"}:
            status = "rejected" if normalized == "reject" else "deferred"
            if proposal.status in {"approved", "rejected"}:
                if proposal.status != status:
                    raise ValueError("Change Proposal already has a different decision")
                return IdentityGateResult(
                    "human_review", information, None, (), proposal_id
                )
            decided = self.proposal_store.update(
                replace(
                    proposal,
                    status=status,
                    decided_at=None if status == "deferred" else self.clock(),
                )
            )
            return IdentityGateResult(
                "human_review", information, None, (), decided.proposal_id
            )

        if proposal.status == "rejected":
            raise ValueError("Change Proposal already has a different decision")
        if proposal.status == "approved":
            self._validate_repeated_human_action(proposal, normalized, object_id, name)
        if normalized == "bind_existing":
            if object_id is None or object_id not in proposal.resolved_object_ids:
                raise ValueError("human binding must select a reviewed existing Object")
            assessment = IdentityAssessment(
                "bind_existing",
                proposal.atomic_information_id,
                proposal.atomic_information_revision_id,
                (proposal.atomic_information_revision_id,),
                None,
                (object_id,),
                proposal.interpretation_fingerprint,
                None,
                self._before_state_fingerprint(None, (object_id,)),
            )
        else:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("human minimal creation requires a Name")
            assessment = IdentityAssessment(
                "create_minimal",
                proposal.atomic_information_id,
                proposal.atomic_information_revision_id,
                (proposal.atomic_information_revision_id,),
                name.strip(),
                (),
                proposal.interpretation_fingerprint,
                None,
                self._before_state_fingerprint(name.strip(), ()),
            )
        result = self.apply(assessment, mode="human_approved", proposal_id=proposal_id)
        if proposal.status != "approved":
            self.proposal_store.update(
                replace(proposal, status="approved", decided_at=self.clock())
            )
        return result

    def _validate_repeated_human_action(
        self,
        proposal: ChangeProposal,
        action: str,
        object_id: str | None,
        name: str | None,
    ) -> None:
        records = tuple(
            record
            for record in self.change_journal.list_changes()
            if record.proposal_id == proposal.proposal_id
        )
        if len(records) > 1:
            raise ValueError("Identity Gate proposal has multiple applied changes")
        if not records:
            return
        record = records[0]
        expected_operation = (
            "bind_existing" if action == "bind_existing" else "create_minimal"
        )
        if record.operation != expected_operation:
            raise ValueError("Change Proposal already has a different decision")
        if action == "bind_existing":
            if object_id != record.resolved_object_ids[0]:
                raise ValueError("Change Proposal already has a different decision")
            return
        if not isinstance(name, str) or not name.strip():
            raise ValueError("human minimal creation requires a Name")
        active_names = tuple(
            assignment.name
            for assignment in self.world_model_repository.list_names(
                record.resolved_object_ids[0], active_only=True
            )
            if assignment.is_primary
        )
        if len(active_names) != 1 or _normalize(active_names[0]) != _normalize(name):
            raise ValueError("Change Proposal already has a different decision")

    def _choose_outcome(
        self,
        information: AtomicInformationRevision,
        evidence: IdentityEvidence,
        candidates: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        if evidence.identity_kind in {
            "action",
            "topic",
            "attribute",
            "pronoun",
            "unresolved_speaker",
        }:
            return "no_object", ()
        if len(candidates) == 1 and not evidence.possible_existing_object_ids:
            return "bind_existing", candidates
        if len(candidates) > 1 or evidence.possible_existing_object_ids:
            return "human_review", candidates
        if (
            evidence.identity_conflict
            or evidence.requires_structure
            or not evidence.low_consequence
        ):
            return "human_review", ()
        if not evidence.long_lived:
            return "no_object", ()
        if evidence.name is None or not evidence.provenance_complete:
            return "accumulate", ()
        bases = set(evidence.identity_bases)
        strong_external = (
            "stable_external_id" in bases and evidence.stable_external_id is not None
        )
        repeated = (
            "repeated_consistent" in bases
            and len(evidence.supporting_revision_ids) >= 2
        )
        human_confirmed = "human_confirmed" in bases
        if strong_external or repeated or human_confirmed:
            return "create_minimal", ()
        return "accumulate", ()

    def _validate_evidence(
        self, information: AtomicInformationRevision, evidence: IdentityEvidence
    ) -> None:
        if evidence.identity_kind not in IDENTITY_KINDS:
            raise ValueError("Identity Gate identity kind is not supported")
        if not evidence.supporting_revision_ids:
            raise ValueError("Identity Gate requires supporting Information revisions")
        if len(set(evidence.supporting_revision_ids)) != len(
            evidence.supporting_revision_ids
        ):
            raise ValueError("supporting Information revisions must be unique")
        if information.revision_id not in evidence.supporting_revision_ids:
            raise ValueError(
                "current Atomic Information revision must support the gate"
            )
        if any(base not in IDENTITY_BASES for base in evidence.identity_bases):
            raise ValueError("Identity Gate identity basis is not supported")
        if len(set(evidence.identity_bases)) != len(evidence.identity_bases):
            raise ValueError("Identity Gate identity bases must be unique")
        if evidence.name is not None and not evidence.name.strip():
            raise ValueError("Identity Gate Name must not be blank")
        if (
            evidence.stable_external_id is not None
            and not evidence.stable_external_id.strip()
        ):
            raise ValueError("stable external ID must not be blank")
        if (
            "stable_external_id" in evidence.identity_bases
            and evidence.stable_external_id is None
        ):
            raise ValueError("stable external ID basis requires a stable external ID")
        if "human_confirmed" in evidence.identity_bases and evidence.name is None:
            raise ValueError("human-confirmed identity requires a Name")
        current_revisions = {
            item.revision_id
            for item in self.atomic_information_store.list_atomic_information()
        }
        if not set(evidence.supporting_revision_ids).issubset(current_revisions):
            raise ValueError("supporting Atomic Information revision is stale")
        if not information.source_evidence:
            raise ValueError("Identity Gate requires source Evidence")
        for object_id in (
            *evidence.possible_existing_object_ids,
            *(
                (evidence.approved_existing_object_id,)
                if evidence.approved_existing_object_id
                else ()
            ),
        ):
            self._require_active_object(object_id)

    def _existing_candidates(
        self, evidence: IdentityEvidence, external_identity_key: str | None
    ) -> tuple[str, ...]:
        candidates: set[str] = set(evidence.possible_existing_object_ids)
        if evidence.approved_existing_object_id is not None:
            candidates.add(evidence.approved_existing_object_id)
        if evidence.name is not None:
            candidates.update(self._name_owners(evidence.name))
        mapped_object_id = self._mapped_object_id(external_identity_key)
        if mapped_object_id is not None:
            self._require_active_object(mapped_object_id)
            if candidates and candidates != {mapped_object_id}:
                raise ValueError(
                    "external identity key conflicts with another Object candidate"
                )
            return (mapped_object_id,)
        return tuple(sorted(candidates))

    @staticmethod
    def _external_identity_key(evidence: IdentityEvidence) -> str | None:
        if evidence.stable_external_id is None:
            return None
        return _digest_id("external_identity", evidence.stable_external_id)

    def _mapped_object_id(self, external_identity_key: str | None) -> str | None:
        if external_identity_key is None:
            return None
        mapping = self.world_model_repository.get_external_identity_mapping(
            external_identity_key
        )
        return None if mapping is None else mapping.object_id

    def _name_owners(self, name: str) -> set[str]:
        normalized = _normalize(name)
        return {
            record.object_id
            for record in self.world_model_repository.list_objects()
            if record.status == "active"
            for assignment in self.world_model_repository.list_names(record.object_id)
            if _normalize(assignment.name) == normalized
        }

    def _require_active_object(self, object_id: str) -> None:
        if self.world_model_repository.get_object(object_id).status != "active":
            raise ValueError("Identity Gate target Object must be active")

    def _identity_fingerprint(self, evidence: IdentityEvidence) -> str:
        payload = {
            "version": 1,
            "name": evidence.name.strip() if evidence.name is not None else None,
            "supporting_revision_ids": sorted(evidence.supporting_revision_ids),
            "identity_bases": sorted(evidence.identity_bases),
            "identity_kind": evidence.identity_kind,
            "stable_external_id": evidence.stable_external_id,
            "approved_existing_object_id": evidence.approved_existing_object_id,
            "possible_existing_object_ids": sorted(
                evidence.possible_existing_object_ids
            ),
            "long_lived": evidence.long_lived,
            "provenance_complete": evidence.provenance_complete,
            "low_consequence": evidence.low_consequence,
            "requires_structure": evidence.requires_structure,
            "identity_conflict": evidence.identity_conflict,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return "identity_gate_" + hashlib.sha256(encoded.encode()).hexdigest()[:32]

    def _before_state_fingerprint(
        self,
        name: str | None,
        object_ids: tuple[str, ...],
        external_identity_key: str | None = None,
    ) -> str:
        payload = {
            "name_owners": sorted(self._name_owners(name)) if name is not None else [],
            "external_identity_object_id": self._mapped_object_id(
                external_identity_key
            ),
            "objects": [
                (
                    object_id,
                    self.world_model_repository.get_object(object_id).status,
                    sorted(
                        (assignment.name, assignment.valid_to)
                        for assignment in self.world_model_repository.list_names(
                            object_id
                        )
                    ),
                )
                for object_id in sorted(object_ids)
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _create_proposal(
        self, assessment: IdentityAssessment, information: AtomicInformationRevision
    ) -> ChangeProposal:
        proposal = ChangeProposal(
            proposal_id=_digest_id(
                "proposal",
                assessment.atomic_information_revision_id,
                assessment.identity_fingerprint,
            ),
            atomic_information_id=assessment.atomic_information_id,
            atomic_information_revision_id=assessment.atomic_information_revision_id,
            proposed_operations=(WorldModelOperation(kind="unresolved"),),
            resolved_object_ids=assessment.resolved_object_ids,
            rationale="Identity Gate requires a human identity decision.",
            supporting_evidence_refs=tuple(
                f"{item.artifact}#{item.segment}"
                for item in information.source_evidence
            ),
            before_state_fingerprint=self._before_state_fingerprint(
                None, assessment.resolved_object_ids
            ),
            interpretation_fingerprint=assessment.identity_fingerprint,
            human_review=HumanReviewContent(
                finding="系统无法安全确认这条信息对应的长期对象身份。",
                importance="错误绑定或重复创建会影响后续长期认知。",
                recommendation="请确认应绑定已有对象、创建最小对象，或先不处理。",
                evidence="当前信息保留了可追溯依据。",
                consequences="只会处理对象身份；不会同时推断角色、关系或生命周期。",
                allowed_actions=(
                    "bind_existing",
                    "create_minimal",
                    "edit_identity_and_create",
                    "reject",
                    "defer",
                ),
            ),
            status="pending",
            created_at=self.clock(),
            decided_at=None,
        )
        return self.proposal_store.add_pending(proposal)

    def _apply_id(
        self, assessment: IdentityAssessment, mode: str, proposal_id: str | None
    ) -> str:
        return _digest_id(
            "apply",
            assessment.atomic_information_revision_id,
            assessment.identity_fingerprint,
            assessment.outcome,
            mode,
            proposal_id or "automatic",
        )

    def _recover(
        self, atomic_information_id: str, fingerprint: str
    ) -> IdentityGateResult | None:
        for receipt in self.world_model_repository.list_apply_receipts():
            try:
                records, bound_object_ids = self._parse_receipt(receipt.payload)
            except ValueError:
                continue
            first = records[0]
            if (
                len(records) == 1
                and first.atomic_information_id == atomic_information_id
                and first.interpretation_fingerprint == fingerprint
                and first.operation in {"bind_existing", "create_minimal"}
            ):
                return self._finalize(
                    records, bound_object_ids, first.operation, first.proposal_id
                )
        return None

    @staticmethod
    def _parse_receipt(
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
        bound_object_ids = value["created_object_ids"]
        if not isinstance(bound_object_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in bound_object_ids
        ):
            raise ValueError("apply receipt Object identities are invalid")
        records = tuple(
            journal_from_dict(item, f"apply receipt record[{index}]")
            for index, item in enumerate(value["records"], start=1)
        )
        if len(records) != 1:
            raise ValueError("Identity Gate receipt must contain one logical change")
        return records, tuple(bound_object_ids)

    def _finalize(
        self,
        records: tuple[ChangeJournalRecord, ...],
        bound_object_ids: tuple[str, ...],
        outcome: str,
        proposal_id: str | None,
    ) -> IdentityGateResult:
        record = records[0]
        if len(bound_object_ids) != 1 or bound_object_ids != record.resolved_object_ids:
            raise ValueError("Identity Gate receipt binding does not match its change")
        current = self.atomic_information_store.get_current(
            record.atomic_information_id
        )
        object_id = bound_object_ids[0]
        if object_id not in current.related_object_ids:
            self.atomic_information_store.append_revision(
                replace(
                    current,
                    revision_number=current.revision_number + 1,
                    revision_id=(
                        f"{current.atomic_information_id}-r{current.revision_number + 1:04d}"
                    ),
                    related_object_ids=tuple(
                        sorted((*current.related_object_ids, object_id))
                    ),
                    created_at=self.clock(),
                    revision_reason=f"identity_gate_{record.operation}",
                )
            )
        self.change_journal.append(record)
        return IdentityGateResult(
            outcome,
            self.atomic_information_store.get_current(record.atomic_information_id),
            object_id,
            (record.change_id,),
            proposal_id,
        )
