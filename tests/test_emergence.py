from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    JsonlAtomicInformationStore,
)
from archeos.digestion import (
    BusinessLanguageHumanJudgmentPort,
    JsonlChangeJournal,
    JsonlChangeProposalStore,
)
from archeos.digestion.serialization import proposal_to_dict
from archeos.emergence import IdentityEvidence, IdentityGateService
from archeos.world_model import SQLiteWorldModelRepository


def atomic_information(identifier: str, *, confidence: float = 0.9):
    source_id = f"source-{identifier}"
    candidate_id = f"candidate-{identifier}"
    atomic_id = (
        "atomic_info_"
        + hashlib.sha256(f"{source_id}\0{candidate_id}".encode()).hexdigest()[:32]
    )
    return AtomicInformationRevision(
        atomic_information_id=atomic_id,
        revision_number=1,
        revision_id=f"{atomic_id}-r0001",
        origin_source_id=source_id,
        origin_candidate_id=candidate_id,
        origin_fingerprint=hashlib.sha256(identifier.encode()).hexdigest(),
        statement="Synthetic governed identity evidence.",
        semantic_type="requirement",
        raw_concerns=("Synthetic identity",),
        related_object_ids=(),
        source_evidence=(
            EvidenceRecord(
                source_id=f"source-{identifier}",
                artifact="synthetic.md",
                segment=1,
                speaker="Speaker_1",
                start="00:00:01.000",
                end="00:00:02.000",
                excerpt="Synthetic evidence.",
            ),
        ),
        context="Synthetic context.",
        confidence=confidence,
        created_at="2026-08-17T00:00:00+00:00",
        revision_reason="initial_ingestion",
    )


class FailOnceJournal:
    def __init__(self, inner: JsonlChangeJournal) -> None:
        self.inner = inner
        self.failures_remaining = 1

    def append(self, record):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("synthetic journal failure")
        return self.inner.append(record)

    def get(self, change_id):
        return self.inner.get(change_id)

    def list_changes(self):
        return self.inner.list_changes()


class FailOnceReceiptRepository(SQLiteWorldModelRepository):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.failures_remaining = 1

    def put_apply_receipt(self, apply_id: str, payload: str):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("synthetic receipt failure")
        return super().put_apply_receipt(apply_id, payload)


class IdentityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.atomic_store = JsonlAtomicInformationStore(self.root / "atomic.jsonl")
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        self.proposals = JsonlChangeProposalStore(self.root / "proposals.jsonl")
        self.journal = JsonlChangeJournal(self.root / "journal.jsonl")
        self.service = self._service()

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary_directory.cleanup()

    def _service(self, journal=None) -> IdentityGateService:
        return IdentityGateService(
            self.atomic_store,
            self.repository,
            self.proposals,
            journal or self.journal,
            BusinessLanguageHumanJudgmentPort(),
            clock=lambda: "2026-08-17T00:00:00+00:00",
        )

    def ingest(self, identifier: str, *, confidence: float = 0.9):
        information = atomic_information(identifier, confidence=confidence)
        self.atomic_store.ingest_batch((information,))
        return information

    @staticmethod
    def evidence(information, name, *bases, **kwargs) -> IdentityEvidence:
        return IdentityEvidence(
            name=name,
            supporting_revision_ids=(information.revision_id,),
            identity_bases=bases,
            **kwargs,
        )

    def test_approved_external_mapping_binds_existing_append_only(self) -> None:
        target = self.repository.create_object("Existing Identity")
        information = self.ingest("external-bind")

        result = self.service.process(
            information.atomic_information_id,
            self.evidence(
                information,
                "Different source label",
                "stable_external_id",
                stable_external_id="external-001",
                approved_existing_object_id=target.object_id,
            ),
        )

        self.assertEqual(result.outcome, "bind_existing")
        self.assertEqual(result.object_id, target.object_id)
        current = self.atomic_store.get_current(information.atomic_information_id)
        self.assertEqual(current.related_object_ids, (target.object_id,))
        self.assertEqual(
            len(self.atomic_store.list_revisions(information.atomic_information_id)), 2
        )
        self.assertEqual(current.source_evidence, information.source_evidence)
        self.assertEqual(current.claim, information.claim)
        self.assertEqual(self.journal.list_changes()[0].operation, "bind_existing")

    def test_current_and_historical_name_resolution_bind_without_fuzzy_match(
        self,
    ) -> None:
        target = self.repository.create_object("Current Name")
        current = self.ingest("current-name")
        current_result = self.service.process(
            current.atomic_information_id,
            self.evidence(current, "  current   NAME "),
        )
        self.repository.rename_object(target.object_id, "Renamed Identity")
        historical = self.ingest("historical-name")
        historical_result = self.service.process(
            historical.atomic_information_id,
            self.evidence(historical, "Current Name"),
        )
        fuzzy = self.ingest("fuzzy-only")
        fuzzy_result = self.service.process(
            fuzzy.atomic_information_id,
            self.evidence(
                fuzzy, "Current Nam", possible_existing_object_ids=(target.object_id,)
            ),
        )

        self.assertEqual(current_result.object_id, target.object_id)
        self.assertEqual(historical_result.object_id, target.object_id)
        self.assertEqual(fuzzy_result.outcome, "human_review")
        self.assertEqual(len(self.repository.list_objects()), 1)

    def test_strong_external_identity_creates_minimal_object_idempotently(self) -> None:
        information = self.ingest("external-create")
        evidence = self.evidence(
            information,
            "Stable Synthetic Identity",
            "stable_external_id",
            stable_external_id="contract-001",
        )

        first = self.service.process(information.atomic_information_id, evidence)
        repeated = self.service.process(information.atomic_information_id, evidence)
        created = self.repository.get_object(first.object_id)

        self.assertEqual(first.outcome, "create_minimal")
        self.assertEqual(repeated.object_id, first.object_id)
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(self.repository.list_roles(created.object_id), ())
        self.assertEqual(
            self.repository.list_relationships(object_id=created.object_id), ()
        )
        self.assertEqual(len(self.journal.list_changes()), 1)
        self.assertEqual(len(self.repository.list_external_identity_mappings()), 1)

    def test_stable_external_identity_binds_across_information_with_name_change(
        self,
    ) -> None:
        first_information = self.ingest("external-stability-first")
        first = self.service.process(
            first_information.atomic_information_id,
            self.evidence(
                first_information,
                "Synthetic Name A",
                "stable_external_id",
                stable_external_id="synthetic-external-stability",
            ),
        )
        second_information = self.ingest("external-stability-second")
        second = self.service.process(
            second_information.atomic_information_id,
            self.evidence(
                second_information,
                "Synthetic Name B",
                "stable_external_id",
                stable_external_id="synthetic-external-stability",
            ),
        )

        self.assertEqual(first.outcome, "create_minimal")
        self.assertEqual(second.outcome, "bind_existing")
        self.assertEqual(second.object_id, first.object_id)
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(len(self.repository.list_external_identity_mappings()), 1)
        self.assertEqual(
            self.atomic_store.get_current(
                second_information.atomic_information_id
            ).related_object_ids,
            (first.object_id,),
        )

    def test_stable_external_identity_mapping_survives_repository_restart(self) -> None:
        first_information = self.ingest("external-restart-first")
        first = self.service.process(
            first_information.atomic_information_id,
            self.evidence(
                first_information,
                "Synthetic Restart Name A",
                "stable_external_id",
                stable_external_id="synthetic-external-restart",
            ),
        )

        self.repository.close()
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        self.service = self._service()
        second_information = self.ingest("external-restart-second")
        second = self.service.process(
            second_information.atomic_information_id,
            self.evidence(
                second_information,
                "Synthetic Restart Name B",
                "stable_external_id",
                stable_external_id="synthetic-external-restart",
            ),
        )

        self.assertEqual(second.outcome, "bind_existing")
        self.assertEqual(second.object_id, first.object_id)
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(len(self.repository.list_external_identity_mappings()), 1)

    def test_stable_external_identity_collision_fails_closed(self) -> None:
        first_information = self.ingest("external-collision-first")
        first = self.service.process(
            first_information.atomic_information_id,
            self.evidence(
                first_information,
                "Synthetic Collision Name A",
                "stable_external_id",
                stable_external_id="synthetic-external-collision",
            ),
        )
        conflicting = self.repository.create_object("Synthetic Collision Name B")
        second_information = self.ingest("external-collision-second")

        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.service.process(
                second_information.atomic_information_id,
                self.evidence(
                    second_information,
                    "Synthetic Collision Name B",
                    "stable_external_id",
                    stable_external_id="synthetic-external-collision",
                    approved_existing_object_id=conflicting.object_id,
                ),
            )

        self.assertEqual(len(self.repository.list_objects()), 2)
        self.assertEqual(
            self.repository.list_external_identity_mappings()[0].object_id,
            first.object_id,
        )
        self.assertEqual(
            self.atomic_store.get_current(
                second_information.atomic_information_id
            ).related_object_ids,
            (),
        )

    def test_repeated_current_revisions_can_create_but_duplicate_support_cannot_weight(
        self,
    ) -> None:
        first = self.ingest("repeated-first")
        second = self.ingest("repeated-second")
        evidence = IdentityEvidence(
            name="Repeated Stable Identity",
            supporting_revision_ids=(first.revision_id, second.revision_id),
            identity_bases=("repeated_consistent",),
        )

        result = self.service.process(first.atomic_information_id, evidence)
        self.assertEqual(result.outcome, "create_minimal")
        duplicate = replace(
            evidence, supporting_revision_ids=(first.revision_id, first.revision_id)
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            self.service.assess(first.atomic_information_id, duplicate)

    def test_high_confidence_without_identity_evidence_accumulates(self) -> None:
        information = self.ingest("weak", confidence=1.0)
        result = self.service.process(
            information.atomic_information_id,
            self.evidence(information, "Weak identity"),
        )

        self.assertEqual(result.outcome, "accumulate")
        self.assertEqual(self.repository.list_objects(), ())
        self.assertEqual(
            self.atomic_store.get_current(information.atomic_information_id),
            information,
        )

    def test_non_identity_kinds_remain_atomic_information(self) -> None:
        for kind in ("action", "topic", "attribute", "pronoun", "unresolved_speaker"):
            with self.subTest(kind=kind):
                information = self.ingest(kind)
                result = self.service.process(
                    information.atomic_information_id,
                    self.evidence(
                        information,
                        "Potential identity",
                        "stable_external_id",
                        stable_external_id=f"id-{kind}",
                        identity_kind=kind,
                    ),
                )
                self.assertEqual(result.outcome, "no_object")
        self.assertEqual(self.repository.list_objects(), ())

    def test_ambiguity_escalates_and_human_can_bind_reviewed_object(self) -> None:
        first = self.repository.create_object("Shared Identity")
        second = self.repository.create_object("Shared Identity")
        information = self.ingest("ambiguous")
        pending = self.service.process(
            information.atomic_information_id,
            self.evidence(information, "Shared Identity"),
        )

        self.assertEqual(pending.outcome, "human_review")
        proposal = self.proposals.get(pending.proposal_id)
        self.assertEqual(
            proposal.resolved_object_ids,
            tuple(sorted((first.object_id, second.object_id))),
        )
        rendered = self.service.human_judgment.render(proposal)
        self.assertIn("绑定已有对象", rendered)
        decided = self.service.decide(
            pending.proposal_id, "bind_existing", object_id=first.object_id
        )
        repeated = self.service.decide(
            pending.proposal_id, "bind_existing", object_id=first.object_id
        )
        with self.assertRaisesRegex(ValueError, "different decision"):
            self.service.decide(
                pending.proposal_id, "bind_existing", object_id=second.object_id
            )

        self.assertEqual(decided.object_id, first.object_id)
        self.assertEqual(repeated.object_id, first.object_id)
        self.assertEqual(self.proposals.get(pending.proposal_id).status, "approved")
        self.assertEqual(len(self.repository.list_objects()), 2)

    def test_human_confirmed_unique_name_does_not_create_redundant_review(self) -> None:
        information = self.ingest("human-confirmed")
        result = self.service.process(
            information.atomic_information_id,
            self.evidence(
                information,
                "Human confirmed identity",
                "human_confirmed",
            ),
        )

        self.assertEqual(result.outcome, "create_minimal")
        self.assertEqual(self.proposals.list_unresolved(), ())

    def test_human_reject_and_defer_are_idempotent(self) -> None:
        information = self.ingest("review-actions")
        pending = self.service.process(
            information.atomic_information_id,
            self.evidence(information, "Review identity", requires_structure=True),
        )
        deferred = self.service.decide(pending.proposal_id, "defer")
        repeated_defer = self.service.decide(pending.proposal_id, "defer")
        rejected = self.service.decide(pending.proposal_id, "reject")
        repeated_reject = self.service.decide(pending.proposal_id, "reject")

        self.assertEqual(deferred.proposal_id, repeated_defer.proposal_id)
        self.assertEqual(rejected.proposal_id, repeated_reject.proposal_id)
        self.assertEqual(self.proposals.get(pending.proposal_id).status, "rejected")
        self.assertEqual(self.repository.list_objects(), ())

    def test_human_can_create_minimal_identity_without_approving_structure(
        self,
    ) -> None:
        information = self.ingest("human-create")
        pending = self.service.process(
            information.atomic_information_id,
            self.evidence(
                information, "Needs identity review", requires_structure=True
            ),
        )

        created = self.service.decide(
            pending.proposal_id, "edit_identity_and_create", name="Reviewed identity"
        )
        object_record = self.repository.get_object(created.object_id)

        self.assertEqual(created.outcome, "create_minimal")
        self.assertEqual(self.repository.list_roles(object_record.object_id), ())
        self.assertEqual(
            self.repository.list_relationships(object_id=object_record.object_id), ()
        )

    def test_human_create_persists_external_identity_across_name_change(self) -> None:
        raw_external_id = "synthetic-human-create-external-id"
        first_information = self.ingest("human-external-create-first")
        pending = self.service.process(
            first_information.atomic_information_id,
            self.evidence(
                first_information,
                "Synthetic Human Name A",
                "stable_external_id",
                stable_external_id=raw_external_id,
                requires_structure=True,
            ),
        )
        proposal = self.proposals.get(pending.proposal_id)

        created = self.service.decide(
            pending.proposal_id,
            "create_minimal",
            name="Synthetic Human Name A",
        )
        second_information = self.ingest("human-external-create-second")
        bound = self.service.process(
            second_information.atomic_information_id,
            self.evidence(
                second_information,
                "Synthetic Human Name B",
                "stable_external_id",
                stable_external_id=raw_external_id,
            ),
        )

        mapping = self.repository.list_external_identity_mappings()
        self.assertIsNotNone(proposal.external_identity_key)
        self.assertNotEqual(proposal.external_identity_key, raw_external_id)
        self.assertEqual(mapping[0].identity_key, proposal.external_identity_key)
        self.assertEqual(mapping[0].object_id, created.object_id)
        self.assertEqual(bound.outcome, "bind_existing")
        self.assertEqual(bound.object_id, created.object_id)
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertNotIn(
            raw_external_id,
            (self.root / "proposals.jsonl").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            raw_external_id.encode(), (self.root / "world.sqlite3").read_bytes()
        )

    def test_human_bind_external_identity_survives_repository_restart(self) -> None:
        target = self.repository.create_object("Synthetic Existing Identity")
        first_information = self.ingest("human-external-bind-first")
        pending = self.service.process(
            first_information.atomic_information_id,
            self.evidence(
                first_information,
                "Synthetic Candidate Label",
                "stable_external_id",
                stable_external_id="synthetic-human-bind-external-id",
                possible_existing_object_ids=(target.object_id,),
            ),
        )
        decided = self.service.decide(
            pending.proposal_id, "bind_existing", object_id=target.object_id
        )

        self.repository.close()
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        self.service = self._service()
        second_information = self.ingest("human-external-bind-second")
        rebound = self.service.process(
            second_information.atomic_information_id,
            self.evidence(
                second_information,
                "Synthetic Renamed Candidate",
                "stable_external_id",
                stable_external_id="synthetic-human-bind-external-id",
            ),
        )

        self.assertEqual(decided.object_id, target.object_id)
        self.assertEqual(rebound.outcome, "bind_existing")
        self.assertEqual(rebound.object_id, target.object_id)
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(len(self.repository.list_external_identity_mappings()), 1)

    def test_human_external_identity_collision_fails_closed(self) -> None:
        mapped_target = self.repository.create_object("Mapped Identity")
        reviewed_target = self.repository.create_object("Reviewed Identity")
        information = self.ingest("human-external-collision")
        pending = self.service.process(
            information.atomic_information_id,
            self.evidence(
                information,
                "Synthetic Review Label",
                "stable_external_id",
                stable_external_id="synthetic-human-collision-external-id",
                possible_existing_object_ids=(reviewed_target.object_id,),
            ),
        )
        proposal = self.proposals.get(pending.proposal_id)
        self.repository.put_external_identity_mapping(
            proposal.external_identity_key, mapped_target.object_id
        )

        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.service.decide(
                pending.proposal_id,
                "bind_existing",
                object_id=reviewed_target.object_id,
            )

        mapping = self.repository.get_external_identity_mapping(
            proposal.external_identity_key
        )
        self.assertEqual(mapping.object_id, mapped_target.object_id)
        self.assertEqual(
            self.atomic_store.get_current(
                information.atomic_information_id
            ).related_object_ids,
            (),
        )
        self.assertEqual(self.proposals.get(pending.proposal_id).status, "pending")

    def test_legacy_proposal_without_external_identity_key_can_replay(self) -> None:
        information = self.ingest("legacy-human-proposal")
        pending = self.service.process(
            information.atomic_information_id,
            self.evidence(information, "Legacy Review", requires_structure=True),
        )
        payload = proposal_to_dict(self.proposals.get(pending.proposal_id))
        payload.pop("external_identity_key")
        legacy_path = self.root / "legacy-proposals.jsonl"
        legacy_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        legacy_store = JsonlChangeProposalStore(legacy_path)
        legacy_service = IdentityGateService(
            self.atomic_store,
            self.repository,
            legacy_store,
            self.journal,
            BusinessLanguageHumanJudgmentPort(),
            clock=lambda: "2026-08-17T00:00:00+00:00",
        )

        self.assertIsNone(legacy_store.get(pending.proposal_id).external_identity_key)
        first = legacy_service.decide(
            pending.proposal_id, "create_minimal", name="Legacy Reviewed Identity"
        )
        replay = legacy_service.decide(
            pending.proposal_id, "create_minimal", name="Legacy Reviewed Identity"
        )

        self.assertEqual(replay.object_id, first.object_id)
        self.assertEqual(self.repository.list_external_identity_mappings(), ())

    def test_stale_assessment_fails_closed_before_create(self) -> None:
        information = self.ingest("stale")
        assessment = self.service.assess(
            information.atomic_information_id,
            self.evidence(
                information,
                "Stale identity",
                "stable_external_id",
                stable_external_id="stale-001",
            ),
        )
        self.atomic_store.append_revision(
            replace(
                information,
                revision_number=2,
                revision_id=f"{information.atomic_information_id}-r0002",
                revision_reason="synthetic_correction",
            )
        )

        with self.assertRaisesRegex(ValueError, "stale"):
            self.service.apply(assessment)
        self.assertEqual(self.repository.list_objects(), ())

    def test_receipt_recovery_converges_after_journal_failure(self) -> None:
        information = self.ingest("recovery")
        evidence = self.evidence(
            information,
            "Recovery identity",
            "stable_external_id",
            stable_external_id="recovery-001",
        )
        failing_service = self._service(FailOnceJournal(self.journal))

        with self.assertRaisesRegex(OSError, "journal failure"):
            failing_service.process(information.atomic_information_id, evidence)
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(len(self.repository.list_external_identity_mappings()), 1)
        self.assertEqual(self.journal.list_changes(), ())

        self.repository.close()
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        self.service = self._service()
        recovered = self.service.process(information.atomic_information_id, evidence)
        current = self.atomic_store.get_current(information.atomic_information_id)
        self.assertEqual(recovered.outcome, "create_minimal")
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(len(self.repository.list_external_identity_mappings()), 1)
        self.assertEqual(len(self.journal.list_changes()), 1)
        self.assertEqual(current.related_object_ids, (recovered.object_id,))

    def test_receipt_failure_rolls_back_object_and_external_identity_mapping(
        self,
    ) -> None:
        self.repository.close()
        self.repository = FailOnceReceiptRepository(self.root / "world.sqlite3")
        self.service = self._service()
        information = self.ingest("receipt-rollback")
        evidence = self.evidence(
            information,
            "Synthetic Receipt Rollback",
            "stable_external_id",
            stable_external_id="synthetic-external-receipt-rollback",
        )

        with self.assertRaisesRegex(OSError, "receipt failure"):
            self.service.process(information.atomic_information_id, evidence)
        self.assertEqual(self.repository.list_objects(), ())
        self.assertEqual(self.repository.list_external_identity_mappings(), ())
        self.assertEqual(self.repository.list_apply_receipts(), ())
        self.assertEqual(self.journal.list_changes(), ())

        self.repository.close()
        self.repository = SQLiteWorldModelRepository(self.root / "world.sqlite3")
        self.service = self._service()
        recovered = self.service.process(information.atomic_information_id, evidence)

        self.assertEqual(recovered.outcome, "create_minimal")
        self.assertEqual(len(self.repository.list_objects()), 1)
        self.assertEqual(len(self.repository.list_external_identity_mappings()), 1)
        self.assertEqual(len(self.repository.list_apply_receipts()), 1)
        self.assertEqual(len(self.journal.list_changes()), 1)
        self.assertEqual(
            self.atomic_store.get_current(
                information.atomic_information_id
            ).related_object_ids,
            (recovered.object_id,),
        )

    def test_duplicate_race_fails_closed(self) -> None:
        information = self.ingest("race")
        assessment = self.service.assess(
            information.atomic_information_id,
            self.evidence(
                information,
                "Race identity",
                "stable_external_id",
                stable_external_id="race-001",
            ),
        )
        self.repository.create_object("Race identity")

        with self.assertRaisesRegex(ValueError, "identity state changed"):
            self.service.apply(assessment)
        self.assertEqual(len(self.repository.list_objects()), 1)
