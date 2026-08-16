from __future__ import annotations

from ..atomic_information.models import (
    claim_from_dict,
    claim_to_dict,
    validate_claim_attribution,
)
from .models import (
    OPERATION_KINDS,
    PROPOSAL_STATUSES,
    ChangeJournalRecord,
    ChangeProposal,
    HumanReviewContent,
    WorldModelOperation,
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    return tuple(_text(item, field) for item in value)


def operation_to_dict(operation: WorldModelOperation) -> dict[str, object]:
    validate_operation(operation)
    return {
        "kind": operation.kind,
        "target_object_id": operation.target_object_id,
        "secondary_object_id": operation.secondary_object_id,
        "name": operation.name,
        "role": operation.role,
        "relation": operation.relation,
        "relationship_id": operation.relationship_id,
        "lifecycle_state": operation.lifecycle_state,
        "start_at": operation.start_at,
        "actual_end_at": operation.actual_end_at,
        "target_end_at": operation.target_end_at,
        "completion_condition": operation.completion_condition,
    }


def operation_from_dict(value: object, field: str) -> WorldModelOperation:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    expected = {
        "kind",
        "target_object_id",
        "secondary_object_id",
        "name",
        "role",
        "relation",
        "relationship_id",
        "lifecycle_state",
        "start_at",
        "actual_end_at",
        "target_end_at",
        "completion_condition",
    }
    if set(value) != expected:
        raise ValueError(f"{field} does not match the operation schema")
    operation = WorldModelOperation(
        kind=_text(value["kind"], f"{field}.kind"),
        target_object_id=_optional_text(
            value["target_object_id"], f"{field}.target_object_id"
        ),
        secondary_object_id=_optional_text(
            value["secondary_object_id"], f"{field}.secondary_object_id"
        ),
        name=_optional_text(value["name"], f"{field}.name"),
        role=_optional_text(value["role"], f"{field}.role"),
        relation=_optional_text(value["relation"], f"{field}.relation"),
        relationship_id=_optional_text(
            value["relationship_id"], f"{field}.relationship_id"
        ),
        lifecycle_state=_optional_text(
            value["lifecycle_state"], f"{field}.lifecycle_state"
        ),
        start_at=_optional_text(value["start_at"], f"{field}.start_at"),
        actual_end_at=_optional_text(value["actual_end_at"], f"{field}.actual_end_at"),
        target_end_at=_optional_text(value["target_end_at"], f"{field}.target_end_at"),
        completion_condition=_optional_text(
            value["completion_condition"], f"{field}.completion_condition"
        ),
    )
    validate_operation(operation, field)
    return operation


def validate_operation(
    operation: WorldModelOperation, field: str = "operation"
) -> None:
    if not isinstance(operation, WorldModelOperation):
        raise TypeError(f"{field} must be a WorldModelOperation")
    if operation.kind not in OPERATION_KINDS:
        raise ValueError(f"{field}.kind is not supported")
    for name in (
        "target_object_id",
        "secondary_object_id",
        "name",
        "role",
        "relation",
        "relationship_id",
        "lifecycle_state",
        "start_at",
        "actual_end_at",
        "target_end_at",
        "completion_condition",
    ):
        _optional_text(getattr(operation, name), f"{field}.{name}")


def proposal_to_dict(proposal: ChangeProposal) -> dict[str, object]:
    validate_proposal(proposal)
    return {
        "proposal_id": proposal.proposal_id,
        "atomic_information_id": proposal.atomic_information_id,
        "atomic_information_revision_id": proposal.atomic_information_revision_id,
        "proposed_operations": [
            operation_to_dict(item) for item in proposal.proposed_operations
        ],
        "resolved_object_ids": list(proposal.resolved_object_ids),
        "rationale": proposal.rationale,
        "supporting_evidence_refs": list(proposal.supporting_evidence_refs),
        "claim_summary": proposal.claim_summary,
        "proposed_claim": (
            None
            if proposal.proposed_claim is None
            else claim_to_dict(proposal.proposed_claim)
        ),
        "before_state_fingerprint": proposal.before_state_fingerprint,
        "interpretation_fingerprint": proposal.interpretation_fingerprint,
        "external_identity_key": proposal.external_identity_key,
        "human_review": {
            "finding": proposal.human_review.finding,
            "importance": proposal.human_review.importance,
            "recommendation": proposal.human_review.recommendation,
            "evidence": proposal.human_review.evidence,
            "consequences": proposal.human_review.consequences,
            "allowed_actions": list(proposal.human_review.allowed_actions),
        },
        "status": proposal.status,
        "created_at": proposal.created_at,
        "decided_at": proposal.decided_at,
    }


def proposal_from_dict(value: object, field: str = "proposal") -> ChangeProposal:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    expected = {
        "proposal_id",
        "atomic_information_id",
        "atomic_information_revision_id",
        "proposed_operations",
        "resolved_object_ids",
        "rationale",
        "supporting_evidence_refs",
        "before_state_fingerprint",
        "interpretation_fingerprint",
        "human_review",
        "status",
        "created_at",
        "decided_at",
    }
    optional = {"claim_summary", "proposed_claim", "external_identity_key"}
    if not set(value).issubset(expected | optional) or not expected.issubset(value):
        raise ValueError(f"{field} does not match the Change Proposal schema")
    raw_operations = value["proposed_operations"]
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError(f"{field}.proposed_operations must not be empty")
    human_review = value["human_review"]
    human_review_required = {
        "finding",
        "importance",
        "recommendation",
        "evidence",
        "consequences",
    }
    if (
        not isinstance(human_review, dict)
        or not human_review_required.issubset(human_review)
        or not set(human_review).issubset(human_review_required | {"allowed_actions"})
    ):
        raise ValueError(f"{field}.human_review does not match its schema")
    proposal = ChangeProposal(
        proposal_id=_text(value["proposal_id"], f"{field}.proposal_id"),
        atomic_information_id=_text(
            value["atomic_information_id"], f"{field}.atomic_information_id"
        ),
        atomic_information_revision_id=_text(
            value["atomic_information_revision_id"],
            f"{field}.atomic_information_revision_id",
        ),
        proposed_operations=tuple(
            operation_from_dict(item, f"{field}.proposed_operations[{index}]")
            for index, item in enumerate(raw_operations, start=1)
        ),
        resolved_object_ids=_strings(
            value["resolved_object_ids"], f"{field}.resolved_object_ids"
        ),
        rationale=_text(value["rationale"], f"{field}.rationale"),
        supporting_evidence_refs=_strings(
            value["supporting_evidence_refs"],
            f"{field}.supporting_evidence_refs",
        ),
        before_state_fingerprint=_text(
            value["before_state_fingerprint"],
            f"{field}.before_state_fingerprint",
        ),
        interpretation_fingerprint=_text(
            value["interpretation_fingerprint"],
            f"{field}.interpretation_fingerprint",
        ),
        human_review=HumanReviewContent(
            finding=_text(human_review["finding"], f"{field}.human_review.finding"),
            importance=_text(
                human_review["importance"], f"{field}.human_review.importance"
            ),
            recommendation=_text(
                human_review["recommendation"],
                f"{field}.human_review.recommendation",
            ),
            evidence=_text(human_review["evidence"], f"{field}.human_review.evidence"),
            consequences=_text(
                human_review["consequences"],
                f"{field}.human_review.consequences",
            ),
            allowed_actions=(
                ("approve", "reject", "defer")
                if "allowed_actions" not in human_review
                else _strings(
                    human_review["allowed_actions"],
                    f"{field}.human_review.allowed_actions",
                )
            ),
        ),
        status=_text(value["status"], f"{field}.status"),
        created_at=_text(value["created_at"], f"{field}.created_at"),
        decided_at=_optional_text(value["decided_at"], f"{field}.decided_at"),
        claim_summary=_optional_text(
            value.get("claim_summary"), f"{field}.claim_summary"
        ),
        proposed_claim=(
            None
            if value.get("proposed_claim") is None
            else claim_from_dict(value["proposed_claim"], f"{field}.proposed_claim")
        ),
        external_identity_key=_optional_text(
            value.get("external_identity_key"), f"{field}.external_identity_key"
        ),
    )
    validate_proposal(proposal, field)
    return proposal


def validate_proposal(proposal: ChangeProposal, field: str = "proposal") -> None:
    if not isinstance(proposal, ChangeProposal):
        raise TypeError(f"{field} must be a ChangeProposal")
    if proposal.status not in PROPOSAL_STATUSES:
        raise ValueError(f"{field}.status is not supported")
    for name in (
        "proposal_id",
        "atomic_information_id",
        "atomic_information_revision_id",
        "rationale",
        "before_state_fingerprint",
        "interpretation_fingerprint",
        "created_at",
    ):
        _text(getattr(proposal, name), f"{field}.{name}")
    if not proposal.proposed_operations:
        raise ValueError(f"{field}.proposed_operations must not be empty")
    for index, operation in enumerate(proposal.proposed_operations, start=1):
        validate_operation(operation, f"{field}.proposed_operations[{index}]")
    _optional_text(proposal.claim_summary, f"{field}.claim_summary")
    _optional_text(proposal.external_identity_key, f"{field}.external_identity_key")
    if proposal.proposed_claim is not None:
        validate_claim_attribution(proposal.proposed_claim, f"{field}.proposed_claim")


def journal_to_dict(record: ChangeJournalRecord) -> dict[str, object]:
    validate_journal(record)
    return {
        "change_id": record.change_id,
        "atomic_information_id": record.atomic_information_id,
        "atomic_information_revision_id": record.atomic_information_revision_id,
        "operation": record.operation,
        "resolved_object_ids": list(record.resolved_object_ids),
        "interpretation_fingerprint": record.interpretation_fingerprint,
        "mode": record.mode,
        "proposal_id": record.proposal_id,
        "status": record.status,
        "created_at": record.created_at,
        "applied_at": record.applied_at,
        "error_code": record.error_code,
    }


def journal_from_dict(
    value: object, field: str = "change journal record"
) -> ChangeJournalRecord:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    expected = {
        "change_id",
        "atomic_information_id",
        "atomic_information_revision_id",
        "operation",
        "resolved_object_ids",
        "interpretation_fingerprint",
        "mode",
        "proposal_id",
        "status",
        "created_at",
        "applied_at",
        "error_code",
    }
    if set(value) != expected:
        raise ValueError(f"{field} does not match the Change Journal schema")
    record = ChangeJournalRecord(
        change_id=_text(value["change_id"], f"{field}.change_id"),
        atomic_information_id=_text(
            value["atomic_information_id"], f"{field}.atomic_information_id"
        ),
        atomic_information_revision_id=_text(
            value["atomic_information_revision_id"],
            f"{field}.atomic_information_revision_id",
        ),
        operation=_text(value["operation"], f"{field}.operation"),
        resolved_object_ids=_strings(
            value["resolved_object_ids"], f"{field}.resolved_object_ids"
        ),
        interpretation_fingerprint=_text(
            value["interpretation_fingerprint"],
            f"{field}.interpretation_fingerprint",
        ),
        mode=_text(value["mode"], f"{field}.mode"),
        proposal_id=_optional_text(value["proposal_id"], f"{field}.proposal_id"),
        status=_text(value["status"], f"{field}.status"),
        created_at=_text(value["created_at"], f"{field}.created_at"),
        applied_at=_optional_text(value["applied_at"], f"{field}.applied_at"),
        error_code=_optional_text(value["error_code"], f"{field}.error_code"),
    )
    validate_journal(record, field)
    return record


def validate_journal(
    record: ChangeJournalRecord, field: str = "change journal record"
) -> None:
    if not isinstance(record, ChangeJournalRecord):
        raise TypeError(f"{field} must be a ChangeJournalRecord")
    if record.mode not in {"automatic", "human_approved"}:
        raise ValueError(f"{field}.mode is not supported")
    if record.status not in {"applied", "failed"}:
        raise ValueError(f"{field}.status is not supported")
    for name in (
        "change_id",
        "atomic_information_id",
        "atomic_information_revision_id",
        "operation",
        "interpretation_fingerprint",
        "created_at",
    ):
        _text(getattr(record, name), f"{field}.{name}")
