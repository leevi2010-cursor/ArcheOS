from __future__ import annotations

import hashlib
import inspect
import json
import signal
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import archeos.atomic_information.jsonl_store as atomic_store_adapter
import archeos.world_model.sqlite_repository as world_model_adapter
from archeos.atomic_information import (
    AtomicInformationRevision,
    ClaimAttribution,
    EvidenceRecord,
    IngestionResult,
    JsonlAtomicInformationStore,
)
from archeos.atomic_information.models import (
    atomic_information_revision_from_dict,
    atomic_information_revision_to_dict,
)
from archeos.digestion import (
    AtomicInformationDigestionService,
    BusinessLanguageHumanJudgmentPort,
    CodexAtomicInformationInterpretationProvider,
    DigestionWorldState,
    InterpretationResult,
    JsonlChangeJournal,
    JsonlChangeProposalStore,
    WorldModelOperation,
)
from archeos.digestion.providers import CodexInterpretationTimeout
from archeos.world_model import (
    ALLOWED_RELATIONSHIPS,
    ObjectResolver,
    SQLiteWorldModelRepository,
)


class FakeInterpretationProvider:
    name = "fake"

    def __init__(self, result: InterpretationResult | Exception) -> None:
        self.result = result
        self.calls = 0
        self.world_states: list[DigestionWorldState] = []

    def interpret(self, atomic_information, current_world_state):
        del atomic_information
        self.calls += 1
        self.world_states.append(current_world_state)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def interpretation(
    *operations: WorldModelOperation,
    evidence_sufficient: bool = True,
    conflict: bool = False,
    ambiguous: bool = False,
    claim: ClaimAttribution | None = None,
) -> InterpretationResult:
    return InterpretationResult(
        operations=operations,
        rationale="Synthetic interpretation for governed digestion.",
        evidence_sufficient=evidence_sufficient,
        conflict=conflict,
        ambiguous=ambiguous,
        claim=claim,
    )


def claim(
    identifier: str,
    *,
    stance: str = "assert",
    claimant_object_id: str | None = None,
    label: str | None = "Speaker_1",
    attribution_confidence: float | None = 0.8,
) -> ClaimAttribution:
    return ClaimAttribution(
        claimant_object_id=claimant_object_id,
        claimant_source_id=f"source-{identifier}",
        claimant_label=label,
        stance=stance,
        claimed_at="2026-08-11T00:00:01+00:00",
        attribution_confidence=attribution_confidence,
    )


def atomic_information(
    identifier: str,
    *concerns: str,
    strict_identity: bool = False,
) -> AtomicInformationRevision:
    origin_source_id = f"source-{identifier}"
    origin_candidate_id = f"candidate-{identifier}"
    atomic_information_id = identifier
    if strict_identity:
        atomic_information_id = (
            "atomic_info_"
            + hashlib.sha256(
                f"{origin_source_id}\0{origin_candidate_id}".encode()
            ).hexdigest()[:32]
        )
    return AtomicInformationRevision(
        atomic_information_id=atomic_information_id,
        revision_number=1,
        revision_id=f"{atomic_information_id}-r0001",
        origin_source_id=origin_source_id,
        origin_candidate_id=origin_candidate_id,
        origin_fingerprint=hashlib.sha256(identifier.encode()).hexdigest(),
        statement="Synthetic evidence supports a governed business update.",
        semantic_type="requirement",
        raw_concerns=concerns,
        related_object_ids=(),
        source_evidence=(
            EvidenceRecord(
                source_id=origin_source_id,
                artifact="synthetic-transcript.md",
                segment=1,
                speaker="Speaker_1",
                start="00:00:01.000",
                end="00:00:03.000",
                excerpt="Synthetic evidence excerpt.",
            ),
        ),
        context="Synthetic context.",
        confidence=0.9,
        created_at="2026-08-11T00:00:00+00:00",
        revision_reason="initial_ingestion",
    )


class MemoryAtomicInformationStore:
    def __init__(self) -> None:
        self.revisions: dict[str, list[AtomicInformationRevision]] = {}

    def ingest_batch(self, revisions) -> IngestionResult:
        for revision in revisions:
            self.revisions[revision.atomic_information_id] = [revision]
        return IngestionResult(
            created=len(revisions),
            existing=0,
            failed=0,
            atomic_information_ids=tuple(
                item.atomic_information_id for item in revisions
            ),
        )

    def get_current(self, atomic_information_id: str) -> AtomicInformationRevision:
        try:
            return self.revisions[atomic_information_id][-1]
        except KeyError as exc:
            raise ValueError(
                f"Atomic Information not found: {atomic_information_id}"
            ) from exc

    def list_revisions(
        self, atomic_information_id: str
    ) -> tuple[AtomicInformationRevision, ...]:
        return tuple(self.revisions.get(atomic_information_id, ()))

    def append_revision(
        self, revision: AtomicInformationRevision
    ) -> AtomicInformationRevision:
        self.revisions[revision.atomic_information_id].append(revision)
        return revision

    def list_atomic_information(self) -> tuple[AtomicInformationRevision, ...]:
        return tuple(items[-1] for items in self.revisions.values())


class FailOnceChangeJournal:
    def __init__(self, inner: JsonlChangeJournal) -> None:
        self.inner = inner
        self.failures_remaining = 1

    def append(self, record):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("synthetic journal write failure")
        return self.inner.append(record)

    def get(self, change_id):
        return self.inner.get(change_id)

    def list_changes(self):
        return self.inner.list_changes()


class FailOnceProposalStore:
    def __init__(self, inner: JsonlChangeProposalStore) -> None:
        self.inner = inner
        self.failures_remaining = 1

    def add_pending(self, proposal):
        return self.inner.add_pending(proposal)

    def get(self, proposal_id):
        return self.inner.get(proposal_id)

    def list_unresolved(self):
        return self.inner.list_unresolved()

    def update(self, proposal):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("synthetic proposal write failure")
        return self.inner.update(proposal)


class DigestionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.atomic_store = MemoryAtomicInformationStore()
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        self.proposals = JsonlChangeProposalStore(self.root / "proposals.jsonl")
        self.journal = JsonlChangeJournal(self.root / "journal.jsonl")
        self.human = BusinessLanguageHumanJudgmentPort()

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary_directory.cleanup()

    def service(
        self, result: InterpretationResult | Exception
    ) -> AtomicInformationDigestionService:
        return AtomicInformationDigestionService(
            self.atomic_store,
            self.repository,
            ObjectResolver(self.repository),
            FakeInterpretationProvider(result),
            self.proposals,
            self.journal,
            self.human,
            clock=lambda: "2026-08-11T01:00:00+00:00",
        )

    def ingest(self, identifier: str, *concerns: str) -> AtomicInformationRevision:
        item = atomic_information(identifier, *concerns)
        self.atomic_store.ingest_batch((item,))
        return item

    def test_legacy_atomic_information_without_claim_reads_as_none(self) -> None:
        item = atomic_information("legacy-claimless", "Legacy", strict_identity=True)
        payload = atomic_information_revision_to_dict(item)
        payload.pop("claim")

        legacy_path = self.root / "legacy-atomic-information.jsonl"
        legacy_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        parsed = atomic_information_revision_from_dict(payload)

        self.assertIsNone(parsed.claim)
        self.assertIsNone(
            JsonlAtomicInformationStore(legacy_path)
            .get_current(item.atomic_information_id)
            .claim
        )

    def test_claim_enrichment_supports_all_stances_and_is_idempotent(self) -> None:
        for stance in ("assert", "deny", "uncertain"):
            with self.subTest(stance=stance):
                identifier = f"atomic-claim-{stance}"
                self.ingest(identifier, "Unresolved Claimant")
                service = self.service(
                    interpretation(
                        WorldModelOperation(kind="no_structural_change"),
                        claim=claim(identifier, stance=stance),
                    )
                )

                first = service.digest(identifier)
                second = service.digest(identifier)
                history = self.atomic_store.list_revisions(identifier)

                self.assertEqual(first.status, "automatic")
                self.assertEqual(second.status, "already_processed")
                self.assertEqual(len(history), 2)
                self.assertIsNone(history[0].claim)
                self.assertEqual(history[1].claim.stance, stance)
                self.assertIsNone(history[1].claim.claimant_object_id)
                self.assertEqual(self.repository.list_objects(), ())

    def test_claim_enrichment_round_trips_in_jsonl_revision_history(self) -> None:
        initial = atomic_information("claim-jsonl", "JSONL Claim", strict_identity=True)
        store = JsonlAtomicInformationStore(self.root / "claim-history.jsonl")
        store.ingest_batch((initial,))
        enriched = replace(
            initial,
            revision_number=2,
            revision_id=f"{initial.atomic_information_id}-r0002",
            revision_reason="claim_enrichment",
            claim=claim("claim-jsonl", stance="uncertain"),
        )

        store.append_revision(enriched)

        history = store.list_revisions(initial.atomic_information_id)
        self.assertIsNone(history[0].claim)
        self.assertEqual(history[1].claim, enriched.claim)

    def test_claimant_object_id_requires_unique_existing_object(self) -> None:
        claimant = self.repository.create_object("Synthetic Speaker", roles=("person",))
        self.ingest("atomic-claimant", "Synthetic Speaker")

        result = self.service(
            interpretation(
                WorldModelOperation(kind="no_structural_change"),
                claim=claim("atomic-claimant", claimant_object_id=claimant.object_id),
            )
        ).digest("atomic-claimant")

        self.assertEqual(
            result.atomic_information.claim.claimant_object_id, claimant.object_id
        )
        self.assertEqual(len(self.repository.list_objects()), 1)

    def test_opposing_claims_coexist_and_block_world_model_change(self) -> None:
        target = self.repository.create_object("Claim Conflict Target")
        statement = "The approved budget is twenty units."
        first = replace(
            atomic_information("atomic-claim-assert", "Claim Conflict Target"),
            statement=statement,
        )
        self.atomic_store.ingest_batch((first,))
        self.service(
            interpretation(
                WorldModelOperation(kind="no_structural_change"),
                claim=claim("atomic-claim-assert", stance="assert"),
            )
        ).digest(first.atomic_information_id)

        second = replace(
            atomic_information("atomic-claim-deny", "Claim Conflict Target"),
            statement=statement,
        )
        self.atomic_store.ingest_batch((second,))
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="add_role",
                    target_object_id=target.object_id,
                    role="project",
                ),
                claim=claim("atomic-claim-deny", stance="deny"),
            )
        )
        result = service.digest(second.atomic_information_id)

        self.assertEqual(result.status, "pending")
        self.assertEqual(self.repository.list_roles(target.object_id), ())
        self.assertEqual(
            self.atomic_store.get_current(first.atomic_information_id).claim.stance,
            "assert",
        )
        self.assertEqual(
            self.atomic_store.get_current(second.atomic_information_id).claim.stance,
            "deny",
        )
        proposal = self.proposals.get(result.proposal_id)
        self.assertIn("Speaker_1主张", proposal.claim_summary)
        self.assertIn("Speaker_1否认", proposal.claim_summary)
        world_state = service.interpretation_provider.world_states[0]
        self.assertEqual(
            world_state.related_atomic_information[0].atomic_information_id,
            first.atomic_information_id,
        )

    def test_claim_conflict_outside_provider_window_still_blocks_auto_change(
        self,
    ) -> None:
        target = self.repository.create_object("Bounded Claim Target")
        statement = "The bounded decision is approved."
        oldest = replace(
            atomic_information("atomic-related-00", "Bounded Claim Target"),
            statement=statement,
            related_object_ids=(target.object_id,),
            claim=claim("atomic-related-00", stance="assert"),
            created_at="2026-08-11T00:00:00+00:00",
        )
        self.atomic_store.ingest_batch((oldest,))
        for index in range(1, 21):
            item = replace(
                atomic_information(
                    f"atomic-related-{index:02d}", "Bounded Claim Target"
                ),
                related_object_ids=(target.object_id,),
                created_at=f"2026-08-11T00:{index:02d}:00+00:00",
            )
            self.atomic_store.ingest_batch((item,))
        current = replace(
            atomic_information("atomic-related-current", "Bounded Claim Target"),
            statement=statement,
            created_at="2026-08-11T00:21:00+00:00",
        )
        self.atomic_store.ingest_batch((current,))
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="add_role",
                    target_object_id=target.object_id,
                    role="project",
                ),
                claim=claim("atomic-related-current", stance="deny"),
            )
        )

        result = service.digest(current.atomic_information_id)

        bounded = service.interpretation_provider.world_states[0]
        self.assertEqual(len(bounded.related_atomic_information), 20)
        self.assertNotIn(
            oldest.atomic_information_id,
            {item.atomic_information_id for item in bounded.related_atomic_information},
        )
        self.assertEqual(result.status, "pending")
        self.assertEqual(self.repository.list_roles(target.object_id), ())
        proposal = self.proposals.get(result.proposal_id)
        self.assertIn("Speaker_1主张", proposal.claim_summary)
        self.assertIn("Speaker_1否认", proposal.claim_summary)

    def test_existing_claim_correction_is_applied_as_new_approved_revision(
        self,
    ) -> None:
        initial = replace(
            atomic_information("atomic-claim-correction", "Claim Correction"),
            claim=claim("atomic-claim-correction", stance="assert"),
        )
        self.atomic_store.ingest_batch((initial,))
        proposed_claim = claim("atomic-claim-correction", stance="deny")
        service = self.service(
            interpretation(
                WorldModelOperation(kind="no_structural_change"),
                claim=proposed_claim,
            )
        )
        pending = service.digest(initial.atomic_information_id)

        self.assertEqual(
            self.atomic_store.get_current(initial.atomic_information_id).claim.stance,
            "assert",
        )
        proposal = self.proposals.get(pending.proposal_id)
        self.assertEqual(proposal.proposed_claim, proposed_claim)
        self.assertIn("建议修订归因", proposal.claim_summary)

        approved = service.decide(pending.proposal_id, "approve")
        repeated = service.decide(pending.proposal_id, "approve")
        history = self.atomic_store.list_revisions(initial.atomic_information_id)

        self.assertEqual(approved.status, "approved")
        self.assertEqual(repeated.status, "approved")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].claim.stance, "assert")
        self.assertEqual(history[1].claim.stance, "deny")
        self.assertEqual(history[1].revision_reason, "human_approved_claim_correction")

    def test_applied_claim_correction_cannot_be_rejected_after_status_failure(
        self,
    ) -> None:
        initial = replace(
            atomic_information("atomic-claim-status-crash", "Claim Status Crash"),
            claim=claim("atomic-claim-status-crash", stance="assert"),
        )
        self.atomic_store.ingest_batch((initial,))
        result = interpretation(
            WorldModelOperation(kind="no_structural_change"),
            claim=claim("atomic-claim-status-crash", stance="deny"),
        )
        pending = self.service(result).digest(initial.atomic_information_id)
        inner_proposals = self.proposals
        self.proposals = FailOnceProposalStore(inner_proposals)
        service = self.service(result)

        with self.assertRaisesRegex(OSError, "proposal write failure"):
            service.decide(pending.proposal_id, "approve")
        self.assertEqual(
            self.atomic_store.get_current(initial.atomic_information_id).claim.stance,
            "deny",
        )
        self.assertEqual(inner_proposals.get(pending.proposal_id).status, "pending")

        with self.assertRaisesRegex(ValueError, "Claim correction"):
            service.decide(pending.proposal_id, "reject")
        with self.assertRaisesRegex(ValueError, "Claim correction"):
            service.decide(pending.proposal_id, "defer")

        recovered = service.decide(pending.proposal_id, "approve")

        self.assertEqual(recovered.status, "approved")
        self.assertEqual(
            len(self.atomic_store.list_revisions(initial.atomic_information_id)), 2
        )

    def test_unique_current_and_historical_names_bind_stable_object(self) -> None:
        record = self.repository.create_object("Synthetic Operations")
        self.ingest("atomic-current", "  SYNTHETIC   operations ")
        current_result = self.service(
            interpretation(WorldModelOperation(kind="no_structural_change"))
        ).digest("atomic-current")

        self.repository.rename_object(record.object_id, "Renamed Operations")
        self.ingest("atomic-history", "Synthetic Operations")
        history_result = self.service(
            interpretation(WorldModelOperation(kind="no_structural_change"))
        ).digest("atomic-history")

        self.assertEqual(
            current_result.atomic_information.related_object_ids, (record.object_id,)
        )
        self.assertEqual(
            history_result.atomic_information.related_object_ids, (record.object_id,)
        )
        self.assertEqual(
            self.atomic_store.get_current("atomic-history").raw_concerns,
            ("Synthetic Operations",),
        )

    def test_ambiguous_match_does_not_guess_and_creates_review(self) -> None:
        first = self.repository.create_object("Shared Name")
        second = self.repository.create_object("Shared Name")
        self.ingest("atomic-ambiguous", "shared name")

        result = self.service(
            interpretation(WorldModelOperation(kind="unresolved"), ambiguous=True)
        ).digest("atomic-ambiguous")

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.atomic_information.related_object_ids, ())
        self.assertEqual(len(self.proposals.list_unresolved()), 1)
        self.assertEqual(
            {item.object_id for item in self.repository.list_objects()},
            {first.object_id, second.object_id},
        )

    def test_unmatched_name_never_auto_creates_object(self) -> None:
        self.ingest("atomic-unmatched", "New Opportunity")
        result = self.service(
            interpretation(
                WorldModelOperation(
                    kind="new_object", name="New Opportunity", role="project"
                )
            )
        ).digest("atomic-unmatched")

        self.assertEqual(result.status, "pending")
        self.assertEqual(self.repository.list_objects(), ())

    def test_clear_lifecycle_fill_is_automatic_and_journaled_to_source(self) -> None:
        record = self.repository.create_object("Lifecycle Target", roles=("project",))
        self.repository.set_lifecycle(record.object_id, state="active")
        self.ingest("atomic-lifecycle", "Lifecycle Target")

        result = self.service(
            interpretation(
                WorldModelOperation(
                    kind="set_lifecycle",
                    target_object_id=record.object_id,
                    lifecycle_state="active",
                    target_end_at="2026-12-31",
                )
            )
        ).digest("atomic-lifecycle")

        lifecycle = self.repository.list_lifecycles(record.object_id, active_only=True)[
            0
        ]
        self.assertEqual(lifecycle.target_end_at, "2026-12-31")
        changes = self.journal.list_changes()
        self.assertEqual(result.proposal_id, None)
        self.assertTrue(any(item.operation == "set_lifecycle" for item in changes))
        self.assertTrue(
            all(item.atomic_information_id == "atomic-lifecycle" for item in changes)
        )

    def test_lifecycle_conflict_creates_proposal_without_overwrite(self) -> None:
        record = self.repository.create_object("Lifecycle Conflict")
        original = self.repository.set_lifecycle(record.object_id, state="active")
        self.ingest("atomic-lifecycle-conflict", "Lifecycle Conflict")

        result = self.service(
            interpretation(
                WorldModelOperation(
                    kind="set_lifecycle",
                    target_object_id=record.object_id,
                    lifecycle_state="completed",
                )
            )
        ).digest("atomic-lifecycle-conflict")

        self.assertEqual(result.status, "pending")
        self.assertEqual(
            self.repository.list_lifecycles(record.object_id, active_only=True),
            (original,),
        )

    def test_accepted_role_adds_automatically_with_provenance(self) -> None:
        record = self.repository.create_object("Role Target")
        self.ingest("atomic-role", "Role Target")
        result = self.service(
            interpretation(
                WorldModelOperation(
                    kind="add_role", target_object_id=record.object_id, role="project"
                )
            )
        ).digest("atomic-role")

        role = self.repository.list_roles(record.object_id, active_only=True)[0]
        self.assertEqual(result.status, "automatic")
        self.assertEqual(role.source_atomic_information_id, "atomic-role")
        self.assertIsNone(role.confidence)
        self.assertEqual(self.proposals.list_unresolved(), ())

    def test_unapproved_role_is_never_written(self) -> None:
        record = self.repository.create_object("Unsupported Role")
        self.ingest("atomic-role-unsupported", "Unsupported Role")
        result = self.service(
            interpretation(
                WorldModelOperation(
                    kind="add_role",
                    target_object_id=record.object_id,
                    role="initiative",
                )
            )
        ).digest("atomic-role-unsupported")

        self.assertEqual(result.status, "pending")
        self.assertEqual(self.repository.list_roles(record.object_id), ())
        with self.assertRaisesRegex(ValueError, "not approved"):
            self.service(interpretation(WorldModelOperation(kind="unresolved"))).decide(
                result.proposal_id, "approve"
            )

    def test_clear_rename_preserves_name_history(self) -> None:
        record = self.repository.create_object("Original Name")
        self.ingest("atomic-rename", "Original Name")
        self.service(
            interpretation(
                WorldModelOperation(
                    kind="rename",
                    target_object_id=record.object_id,
                    name="Current Name",
                )
            )
        ).digest("atomic-rename")

        self.assertEqual(
            [item.name for item in self.repository.list_names(record.object_id)],
            ["Original Name", "Current Name"],
        )

    def test_related_to_requires_two_resolved_endpoints(self) -> None:
        first = self.repository.create_object("First Endpoint")
        second = self.repository.create_object("Second Endpoint")
        self.ingest("atomic-link", "First Endpoint", "Second Endpoint")
        automatic = self.service(
            interpretation(
                WorldModelOperation(
                    kind="create_relationship",
                    target_object_id=first.object_id,
                    secondary_object_id=second.object_id,
                    relation="related_to",
                )
            )
        ).digest("atomic-link")

        self.assertEqual(automatic.status, "automatic")
        relationship = self.repository.list_relationships()[0]
        self.assertEqual(relationship.source_atomic_information_id, "atomic-link")

        third = self.repository.create_object("Third Endpoint")
        self.ingest("atomic-unclear-link", "First Endpoint")
        pending = self.service(
            interpretation(
                WorldModelOperation(
                    kind="create_relationship",
                    target_object_id=first.object_id,
                    secondary_object_id=third.object_id,
                    relation="related_to",
                )
            )
        ).digest("atomic-unclear-link")
        self.assertEqual(pending.status, "pending")
        self.assertEqual(len(self.repository.list_relationships()), 1)

    def test_all_approved_relationships_are_directional_and_confidence_free(
        self,
    ) -> None:
        self.assertEqual(
            ALLOWED_RELATIONSHIPS,
            {
                "part_of",
                "member_of",
                "responsible_for",
                "depends_on",
                "related_to",
            },
        )
        for index, relation in enumerate(sorted(ALLOWED_RELATIONSHIPS), start=1):
            first = self.repository.create_object(f"Relation Source {index}")
            second = self.repository.create_object(f"Relation Target {index}")
            identifier = f"atomic-relation-{index}"
            self.ingest(
                identifier,
                f"Relation Source {index}",
                f"Relation Target {index}",
            )

            result = self.service(
                interpretation(
                    WorldModelOperation(
                        kind="create_relationship",
                        target_object_id=first.object_id,
                        secondary_object_id=second.object_id,
                        relation=relation,
                    ),
                    claim=claim(identifier, attribution_confidence=0.99),
                )
            ).digest(identifier)

            self.assertEqual(result.status, "automatic")
            self.assertEqual(
                result.atomic_information.claim.attribution_confidence,
                0.99,
            )
            stored = self.repository.list_relationships(object_id=first.object_id)[0]
            self.assertEqual(stored.from_object_id, first.object_id)
            self.assertEqual(stored.to_object_id, second.object_id)
            self.assertEqual(stored.relation, relation)
            self.assertIsNone(stored.confidence)
            self.assertFalse(
                any(
                    item.from_object_id == second.object_id
                    and item.to_object_id == first.object_id
                    for item in self.repository.list_relationships(
                        object_id=second.object_id
                    )
                )
            )

    def test_unapproved_relationship_cannot_be_applied(self) -> None:
        first = self.repository.create_object("Unsupported Relation Source")
        second = self.repository.create_object("Unsupported Relation Target")
        self.ingest(
            "atomic-unsupported-relation",
            "Unsupported Relation Source",
            "Unsupported Relation Target",
        )
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="create_relationship",
                    target_object_id=first.object_id,
                    secondary_object_id=second.object_id,
                    relation="supports",
                )
            )
        )
        pending = service.digest("atomic-unsupported-relation")

        self.assertEqual(pending.status, "pending")
        with self.assertRaisesRegex(ValueError, "not approved"):
            service.decide(pending.proposal_id, "approve")
        self.assertEqual(self.repository.list_relationships(), ())

    def test_information_only_claim_does_not_create_relationship_object(self) -> None:
        self.ingest("atomic-work-description", "One-time photo editing")

        result = self.service(
            interpretation(
                WorldModelOperation(kind="no_structural_change"),
                claim=claim("atomic-work-description"),
            )
        ).digest("atomic-work-description")

        self.assertEqual(result.status, "automatic")
        self.assertEqual(self.repository.list_objects(), ())
        self.assertEqual(self.repository.list_relationships(), ())

    def test_retry_is_idempotent_and_safe_auto_has_no_proposal(self) -> None:
        record = self.repository.create_object("Retry Target")
        self.ingest("atomic-retry", "Retry Target")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="add_role", target_object_id=record.object_id, role="brand"
                )
            )
        )

        first = service.digest("atomic-retry")
        journal_count = len(self.journal.list_changes())
        second = service.digest("atomic-retry")

        self.assertEqual(first.status, "automatic")
        self.assertEqual(second.status, "already_processed")
        self.assertEqual(len(self.repository.list_roles(record.object_id)), 1)
        self.assertEqual(len(self.journal.list_changes()), journal_count)
        self.assertEqual(self.proposals.list_unresolved(), ())

    def test_conflict_creates_pending_without_changing_old_state(self) -> None:
        record = self.repository.create_object("Conflict Target", roles=("project",))
        self.ingest("atomic-conflict", "Conflict Target")
        result = self.service(
            interpretation(
                WorldModelOperation(kind="conflict", target_object_id=record.object_id),
                conflict=True,
            )
        ).digest("atomic-conflict")

        self.assertEqual(result.status, "pending")
        self.assertEqual(
            ObjectResolver(self.repository).resolve(record.object_id).roles,
            ("project",),
        )

    def test_approve_executes_once_and_records_human_change(self) -> None:
        record = self.repository.create_object("Approval Target", roles=("project",))
        self.ingest("atomic-approve", "Approval Target")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="end_role", target_object_id=record.object_id, role="project"
                )
            )
        )
        pending = service.digest("atomic-approve")

        first = service.decide(pending.proposal_id, "approve")
        second = service.decide(pending.proposal_id, "approve")

        self.assertEqual(first.status, "approved")
        self.assertEqual(second.change_ids, first.change_ids)
        self.assertEqual(
            self.repository.list_roles(record.object_id, active_only=True), ()
        )
        human_changes = [
            item
            for item in self.journal.list_changes()
            if item.mode == "human_approved"
        ]
        self.assertEqual(len(human_changes), 1)

    def test_reject_and_defer_do_not_modify_world_model_or_history(self) -> None:
        before_objects = self.repository.list_objects()
        for suffix, decision, expected in (
            ("reject", "reject", "rejected"),
            ("defer", "defer", "deferred"),
        ):
            identifier = f"atomic-{suffix}"
            self.ingest(identifier, f"New {suffix}")
            service = self.service(
                interpretation(
                    WorldModelOperation(
                        kind="new_object", name=f"New {suffix}", role="project"
                    )
                )
            )
            pending = service.digest(identifier)
            initial_history = self.atomic_store.list_revisions(identifier)
            decided = service.decide(pending.proposal_id, decision)
            self.assertEqual(decided.status, expected)
            self.assertEqual(
                self.atomic_store.list_revisions(identifier), initial_history
            )
        self.assertEqual(self.repository.list_objects(), before_objects)

    def test_deferred_proposal_remains_unresolved_then_can_be_decided(self) -> None:
        for suffix, final_decision, expected_status in (
            ("approve", "approve", "approved"),
            ("reject", "reject", "rejected"),
        ):
            identifier = f"atomic-deferred-{suffix}"
            self.ingest(identifier, f"Deferred {suffix}")
            service = self.service(
                interpretation(
                    WorldModelOperation(
                        kind="new_object",
                        name=f"Deferred {suffix}",
                        role="project",
                    )
                )
            )
            pending = service.digest(identifier)

            deferred = service.decide(pending.proposal_id, "defer")

            self.assertEqual(deferred.status, "deferred")
            self.assertIsNone(self.proposals.get(pending.proposal_id).decided_at)
            self.assertIn(
                pending.proposal_id,
                {item.proposal_id for item in service.list_pending()},
            )

            decided = service.decide(pending.proposal_id, final_decision)

            self.assertEqual(decided.status, expected_status)
            self.assertNotIn(
                pending.proposal_id,
                {item.proposal_id for item in service.list_pending()},
            )

    def test_human_review_uses_business_language_without_raw_jargon(self) -> None:
        self.ingest("atomic-language", "Independent Item")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="new_object", name="Independent Item", role="project"
                )
            )
        )
        service.digest("atomic-language")
        rendered = service.render_pending()[0]

        for expected in ("系统发现", "为什么重要", "建议", "依据", "选择及后果"):
            self.assertIn(expected, rendered)
        for forbidden in (
            "object_id",
            "repository",
            "mutation",
            "foreign key",
            "JSONL",
            "SQLite",
        ):
            self.assertNotIn(forbidden.lower(), rendered.lower())
        self.assertIn("没有明确业务联系", rendered)

    def test_stale_proposal_is_blocked(self) -> None:
        record = self.repository.create_object("Stale Target", roles=("project",))
        self.ingest("atomic-stale", "Stale Target")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="end_role", target_object_id=record.object_id, role="project"
                )
            )
        )
        pending = service.digest("atomic-stale")
        self.repository.add_role(record.object_id, "brand")

        with self.assertRaisesRegex(ValueError, "changed"):
            service.decide(pending.proposal_id, "approve")
        self.assertEqual(
            set(ObjectResolver(self.repository).resolve(record.object_id).roles),
            {"brand", "project"},
        )

    def test_stale_atomic_information_revision_blocks_approval(self) -> None:
        record = self.repository.create_object(
            "Stale Information Target", roles=("project",)
        )
        self.ingest("atomic-stale-information", "Stale Information Target")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="end_role",
                    target_object_id=record.object_id,
                    role="project",
                )
            )
        )
        pending = service.digest("atomic-stale-information")
        current = self.atomic_store.get_current("atomic-stale-information")
        self.atomic_store.append_revision(
            replace(
                current,
                revision_number=current.revision_number + 1,
                revision_id=(
                    f"{current.atomic_information_id}-"
                    f"r{current.revision_number + 1:04d}"
                ),
                revision_reason="synthetic_correction",
            )
        )

        with self.assertRaisesRegex(ValueError, "Atomic Information changed"):
            service.decide(pending.proposal_id, "approve")
        self.assertEqual(
            ObjectResolver(self.repository).resolve(record.object_id).roles,
            ("project",),
        )

    def test_apply_receipt_recovers_after_journal_write_failure(self) -> None:
        self.ingest("atomic-crash-journal", "Crash Recovery Object")
        inner_journal = self.journal
        self.journal = FailOnceChangeJournal(inner_journal)
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="new_object",
                    name="Crash Recovery Object",
                    role="project",
                )
            )
        )
        pending = service.digest("atomic-crash-journal")

        with self.assertRaisesRegex(OSError, "journal write failure"):
            service.decide(pending.proposal_id, "approve")
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(self.proposals.get(pending.proposal_id).status, "pending")

        self.repository.close()
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="new_object",
                    name="Crash Recovery Object",
                    role="project",
                )
            )
        )

        recovered = service.decide(pending.proposal_id, "approve")

        self.assertEqual(recovered.status, "approved")
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(len(inner_journal.list_changes()), 1)
        created = self.repository.list_objects()[0]
        self.assertIn(
            created.object_id,
            self.atomic_store.get_current("atomic-crash-journal").related_object_ids,
        )

    def test_automatic_receipt_recovers_before_changed_provider_output(self) -> None:
        record = self.repository.create_object("Automatic Crash Target")
        initial = replace(
            atomic_information("atomic-auto-crash", "Automatic Crash Target"),
            related_object_ids=(record.object_id,),
        )
        self.atomic_store.ingest_batch((initial,))
        inner_journal = self.journal
        self.journal = FailOnceChangeJournal(inner_journal)
        first_service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="add_role",
                    target_object_id=record.object_id,
                    role="project",
                )
            )
        )

        with self.assertRaisesRegex(OSError, "journal write failure"):
            first_service.digest("atomic-auto-crash")
        self.assertEqual(
            len(self.repository.list_roles(record.object_id, active_only=True)), 1
        )
        self.assertEqual(inner_journal.list_changes(), ())

        self.repository.close()
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        self.journal = inner_journal
        second_service = self.service(
            interpretation(WorldModelOperation(kind="no_structural_change"))
        )

        recovered = second_service.digest("atomic-auto-crash")

        self.assertEqual(recovered.status, "automatic")
        self.assertEqual(second_service.interpretation_provider.calls, 1)
        self.assertEqual(len(inner_journal.list_changes()), 1)
        self.assertEqual(
            len(self.repository.list_roles(record.object_id, active_only=True)), 1
        )

    def test_apply_receipt_recovers_after_proposal_write_failure(self) -> None:
        record = self.repository.create_object(
            "Crash Proposal Target", roles=("project",)
        )
        self.ingest("atomic-crash-proposal", "Crash Proposal Target")
        inner_proposals = self.proposals
        self.proposals = FailOnceProposalStore(inner_proposals)
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="end_role",
                    target_object_id=record.object_id,
                    role="project",
                )
            )
        )
        pending = service.digest("atomic-crash-proposal")

        with self.assertRaisesRegex(OSError, "proposal write failure"):
            service.decide(pending.proposal_id, "approve")
        self.assertEqual(
            self.repository.list_roles(record.object_id, active_only=True), ()
        )
        self.assertEqual(inner_proposals.get(pending.proposal_id).status, "pending")

        with self.assertRaisesRegex(ValueError, "already committed"):
            service.decide(pending.proposal_id, "reject")
        with self.assertRaisesRegex(ValueError, "already committed"):
            service.decide(pending.proposal_id, "defer")
        self.assertEqual(inner_proposals.get(pending.proposal_id).status, "pending")

        recovered = service.decide(pending.proposal_id, "approve")

        self.assertEqual(recovered.status, "approved")
        self.assertEqual(
            self.repository.list_roles(record.object_id, active_only=True), ()
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in self.journal.list_changes()
                    if item.operation == "end_role"
                ]
            ),
            1,
        )

    def test_new_object_and_relationship_apply_in_one_approved_plan(self) -> None:
        existing = self.repository.create_object("Existing Business")
        self.ingest("atomic-new-linked", "Existing Business", "New Project")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="new_object",
                    name="New Project",
                    role="project",
                    secondary_object_id=existing.object_id,
                    relation="related_to",
                )
            )
        )
        pending = service.digest("atomic-new-linked")
        approved = service.decide(pending.proposal_id, "approve")

        self.assertEqual(approved.status, "approved")
        created = next(
            item
            for item in self.repository.list_objects()
            if item.object_id != existing.object_id
        )
        relationship = self.repository.list_relationships()[0]
        self.assertEqual(
            {relationship.from_object_id, relationship.to_object_id},
            {created.object_id, existing.object_id},
        )
        self.assertIn(
            created.object_id,
            self.atomic_store.get_current("atomic-new-linked").related_object_ids,
        )

    def test_delete_orphan_guard_and_logical_tombstone(self) -> None:
        target = self.repository.create_object("Delete Target")
        neighbor = self.repository.create_object("Only Neighbor")
        self.repository.create_relationship(
            target.object_id, "related_to", neighbor.object_id
        )
        self.ingest("atomic-delete-blocked", "Delete Target")
        blocked_service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="delete_object", target_object_id=target.object_id
                )
            )
        )
        blocked = blocked_service.digest("atomic-delete-blocked")
        with self.assertRaisesRegex(ValueError, "without any active connection"):
            blocked_service.decide(blocked.proposal_id, "approve")
        self.assertEqual(self.repository.get_object(target.object_id).status, "active")
        self.assertEqual(len(self.repository.list_relationships()), 1)

        anchor = self.repository.create_object("Neighbor Anchor")
        self.repository.create_relationship(
            neighbor.object_id, "related_to", anchor.object_id
        )
        approved = blocked_service.decide(blocked.proposal_id, "approve")
        self.assertEqual(approved.status, "approved")
        self.assertEqual(self.repository.get_object(target.object_id).status, "deleted")
        self.assertEqual(len(self.repository.list_names(target.object_id)), 1)
        self.assertEqual(len(self.repository.list_relationships(active_only=False)), 2)

    def test_runtime_failure_is_not_business_conflict(self) -> None:
        self.ingest("atomic-runtime", "Runtime Target")
        with self.assertRaisesRegex(RuntimeError, "synthetic runtime unavailable"):
            self.service(RuntimeError("synthetic runtime unavailable")).digest(
                "atomic-runtime"
            )
        self.assertEqual(self.proposals.list_unresolved(), ())
        self.assertEqual(self.journal.list_changes(), ())
        self.assertEqual(len(self.atomic_store.list_revisions("atomic-runtime")), 1)

    def test_multi_operation_failure_rolls_back_world_model(self) -> None:
        target = self.repository.create_object("Rollback Target", roles=("project",))
        other = self.repository.create_object("Rollback Other")
        self.ingest("atomic-rollback", "Rollback Target", "Rollback Other")
        service = self.service(
            interpretation(
                WorldModelOperation(
                    kind="end_role", target_object_id=target.object_id, role="project"
                ),
                WorldModelOperation(
                    kind="create_relationship",
                    target_object_id=target.object_id,
                    secondary_object_id=other.object_id,
                    relation="unsupported_relation",
                ),
            )
        )
        pending = service.digest("atomic-rollback")
        with self.assertRaisesRegex(ValueError, "not approved"):
            service.decide(pending.proposal_id, "approve")

        self.assertEqual(
            ObjectResolver(self.repository).resolve(target.object_id).roles,
            ("project",),
        )
        self.assertEqual(self.repository.list_relationships(), ())
        self.assertEqual(
            [
                item
                for item in self.journal.list_changes()
                if item.mode == "human_approved"
            ],
            [],
        )

    def test_storage_adapters_do_not_contain_human_approval_policy(self) -> None:
        for module in (atomic_store_adapter, world_model_adapter):
            source = inspect.getsource(module).lower()
            for policy_word in ("approve", "reject", "defer", "human judgment"):
                self.assertNotIn(policy_word, source)


class CodexDigestionProviderTest(unittest.TestCase):
    def test_official_sdk_adapter_is_read_only_structured_and_ephemeral(self) -> None:
        operation = {
            "kind": "no_structural_change",
            "target_object_id": None,
            "secondary_object_id": None,
            "name": None,
            "role": None,
            "relation": None,
            "relationship_id": None,
            "lifecycle_state": None,
            "start_at": None,
            "actual_end_at": None,
            "target_end_at": None,
            "completion_condition": None,
        }
        payload = json.dumps(
            {
                "operations": [operation],
                "rationale": "No durable structural change.",
                "evidence_sufficient": True,
                "conflict": False,
                "ambiguous": False,
                "claim": None,
            }
        )
        observed: dict[str, object] = {}

        class Result:
            final_response = payload

        class Thread:
            def turn(self, prompt, **kwargs):
                observed["prompt"] = prompt
                observed["run"] = kwargs
                return self

            def run(self):
                return Result()

        class Codex:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def thread_start(self, **kwargs):
                observed["thread"] = kwargs
                return Thread()

        provider = CodexAtomicInformationInterpretationProvider(
            sdk_loader=lambda: (Codex, "deny-all", "read-only")
        )
        result = provider.interpret(
            atomic_information("atomic-sdk", "SDK Target", strict_identity=True),
            current_world_state=DigestionWorldState(
                resolved_objects=(),
                unmatched_concerns=("SDK Target",),
                ambiguous_concerns=(),
            ),
        )

        self.assertEqual(result.operations[0].kind, "no_structural_change")
        self.assertEqual(observed["thread"]["approval_mode"], "deny-all")
        self.assertEqual(observed["thread"]["sandbox"], "read-only")
        self.assertTrue(observed["thread"]["ephemeral"])
        self.assertEqual(observed["run"]["sandbox"], "read-only")
        schema = observed["run"]["output_schema"]
        self.assertIn("claim", schema["required"])
        relation_schema = schema["properties"]["operations"]["items"]["properties"][
            "relation"
        ]
        self.assertEqual(set(relation_schema["enum"]), {*ALLOWED_RELATIONSHIPS, None})
        self.assertIn("related_atomic_information", observed["prompt"])


    def test_run_session_reuses_app_server_but_not_thread_or_cwd(self) -> None:
        operation = {
            "kind": "no_structural_change",
            "target_object_id": None,
            "secondary_object_id": None,
            "name": None,
            "role": None,
            "relation": None,
            "relationship_id": None,
            "lifecycle_state": None,
            "start_at": None,
            "actual_end_at": None,
            "target_end_at": None,
            "completion_condition": None,
        }
        response = json.dumps(
            {
                "operations": [operation],
                "rationale": "No durable structural change.",
                "evidence_sufficient": True,
                "conflict": False,
                "ambiguous": False,
                "claim": None,
            }
        )

        class Turn:
            def run(self):
                return type("Result", (), {"final_response": response})()

            def interrupt(self):
                raise AssertionError("successful turn must not be interrupted")

        class Thread:
            def __init__(self, prompt_log):
                self.prompt_log = prompt_log

            def turn(self, prompt, **kwargs):
                self.prompt_log.append((prompt, kwargs))
                return Turn()

        instances = []

        class Codex:
            def __init__(self):
                self.closed = False
                self.thread_kwargs = []
                self.prompts = []
                instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def thread_start(self, **kwargs):
                self.thread_kwargs.append(kwargs)
                return Thread(self.prompts)

        provider = CodexAtomicInformationInterpretationProvider(
            sdk_loader=lambda: (Codex, "deny-all", "read-only")
        )
        first = replace(
            atomic_information("atomic-one", "One", strict_identity=True),
            statement="FIRST_PRIVATE_STATEMENT",
        )
        second = replace(
            atomic_information("atomic-two", "Two", strict_identity=True),
            statement="SECOND_PRIVATE_STATEMENT",
        )
        world_state = DigestionWorldState((), (), ())
        with provider.session():
            provider.interpret(first, world_state)
            provider.interpret(second, world_state)

        self.assertEqual(len(instances), 1)
        instance = instances[0]
        self.assertTrue(instance.closed)
        self.assertEqual(len(instance.thread_kwargs), 2)
        cwd_values = [item["cwd"] for item in instance.thread_kwargs]
        self.assertEqual(len(set(cwd_values)), 2)
        self.assertTrue(all(item["ephemeral"] for item in instance.thread_kwargs))
        self.assertTrue(all(not Path(path).exists() for path in cwd_values))
        self.assertIn("FIRST_PRIVATE_STATEMENT", instance.prompts[0][0])
        self.assertNotIn("FIRST_PRIVATE_STATEMENT", instance.prompts[1][0])
        self.assertIn("SECOND_PRIVATE_STATEMENT", instance.prompts[1][0])
        metrics = provider.metrics_since(0)
        self.assertEqual(metrics["app_server_start_count"], 1)
        self.assertEqual(metrics["thread_count"], 2)
        self.assertEqual(metrics["turn_count"], 2)
        self.assertEqual(metrics["failure_count"], 0)

    def test_timeout_interrupts_destroys_session_and_prevents_next_turn(self) -> None:
        operation = {
            "kind": "no_structural_change",
            "target_object_id": None,
            "secondary_object_id": None,
            "name": None,
            "role": None,
            "relation": None,
            "relationship_id": None,
            "lifecycle_state": None,
            "start_at": None,
            "actual_end_at": None,
            "target_end_at": None,
            "completion_condition": None,
        }
        response = json.dumps(
            {
                "operations": [operation],
                "rationale": "No durable structural change.",
                "evidence_sufficient": True,
                "conflict": False,
                "ambiguous": False,
                "claim": None,
            }
        )

        class Turn:
            def __init__(self):
                self.released = threading.Event()
                self.interrupted = False

            def run(self):
                self.released.wait()
                return type("Result", (), {"final_response": response})()

            def interrupt(self):
                self.interrupted = True
                self.released.set()

        class Thread:
            turn_handle = Turn()

            def turn(self, *_args, **_kwargs):
                return self.turn_handle

        class Codex:
            instance = None

            def __init__(self):
                self.closed = False
                self.thread_starts = 0
                self.__class__.instance = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def thread_start(self, **_kwargs):
                self.thread_starts += 1
                return Thread()

        provider = CodexAtomicInformationInterpretationProvider(
            sdk_loader=lambda: (Codex, "deny-all", "read-only"),
            timeout_seconds=0.01,
            interrupt_grace_seconds=0.1,
        )
        item = atomic_information("atomic-timeout", "Timeout", strict_identity=True)
        with provider.session():
            with self.assertRaises(CodexInterpretationTimeout):
                provider.interpret(item, DigestionWorldState((), (), ()))
            with self.assertRaisesRegex(RuntimeError, "cannot be reused"):
                provider.interpret(item, DigestionWorldState((), (), ()))

        assert Codex.instance is not None
        self.assertTrue(Thread.turn_handle.interrupted)
        self.assertTrue(Codex.instance.closed)
        self.assertEqual(Codex.instance.thread_starts, 1)
        metrics = provider.metrics_since(0)
        self.assertEqual(metrics["timeout_count"], 1)
        self.assertEqual(metrics["failure_categories"], {"timeout": 1})

    def test_sigterm_closes_session_and_restores_previous_handler(self) -> None:
        class Codex:
            instance = None

            def __init__(self):
                self.closed = False
                self.__class__.instance = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

        current_handler = {"value": signal.SIG_DFL}

        def getsignal(_signum):
            return current_handler["value"]

        def install_signal(_signum, handler):
            previous = current_handler["value"]
            current_handler["value"] = handler
            return previous

        provider = CodexAtomicInformationInterpretationProvider(
            sdk_loader=lambda: (Codex, "deny-all", "read-only")
        )
        with (
            patch(
                "archeos.digestion.providers.signal.getsignal",
                side_effect=getsignal,
            ),
            patch(
                "archeos.digestion.providers.signal.signal",
                side_effect=install_signal,
            ),
            self.assertRaises(SystemExit),
            provider.session(),
        ):
            provider._ensure_session()
            handler = current_handler["value"]
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)

        assert Codex.instance is not None
        self.assertTrue(Codex.instance.closed)
        self.assertIs(current_handler["value"], signal.SIG_DFL)


if __name__ == "__main__":
    unittest.main()
