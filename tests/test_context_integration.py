from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from archeos.atomic_information import (
    AtomicInformationRevision,
    ClaimAttribution,
    EvidenceRecord,
    JsonlAtomicInformationStore,
)
from archeos.context import ContextBuilder, ContextRequest
from archeos.digestion import JsonlChangeJournal, JsonlChangeProposalStore
from archeos.digestion.models import (
    ChangeJournalRecord,
    ChangeProposal,
    HumanReviewContent,
    WorldModelOperation,
)
from archeos.world_model import ObjectResolver, SQLiteWorldModelRepository


def revision(
    source_id: str,
    candidate_id: str,
    created_at: str,
    object_ids: tuple[str, ...],
    *,
    revision_number: int = 1,
    claim: ClaimAttribution | None = None,
) -> AtomicInformationRevision:
    atomic_id = "atomic_info_" + hashlib.sha256(
        f"{source_id}\0{candidate_id}".encode()
    ).hexdigest()[:32]
    statement = f"Synthetic observation {candidate_id} revision {revision_number}."
    return AtomicInformationRevision(
        atomic_information_id=atomic_id,
        revision_number=revision_number,
        revision_id=f"{atomic_id}-r{revision_number:04d}",
        origin_source_id=source_id,
        origin_candidate_id=candidate_id,
        origin_fingerprint=hashlib.sha256(candidate_id.encode()).hexdigest(),
        statement=statement,
        semantic_type="observation",
        raw_concerns=(statement,),
        related_object_ids=object_ids,
        source_evidence=(
            EvidenceRecord(
                source_id=source_id,
                artifact="synthetic-transcript.md",
                segment=revision_number,
                speaker="Speaker_1",
                start="00:00:01.000",
                end="00:00:02.000",
                excerpt=statement,
            ),
        ),
        context="Synthetic integration context.",
        confidence=0.8,
        created_at=created_at,
        revision_reason="initial_ingestion" if revision_number == 1 else "test_revision",
        claim=claim,
    )


class ContextAdapterIntegrationTest(unittest.TestCase):
    def test_real_sqlite_jsonl_context_is_bounded_deterministic_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root_path = Path(temp)
            database_path = root_path / "world.sqlite3"
            information_path = root_path / "atomic_information.jsonl"
            journal_path = root_path / "change_journal.jsonl"
            proposal_path = root_path / "change_proposals.jsonl"

            with SQLiteWorldModelRepository(database_path) as repository:
                root = repository.create_object("Synthetic Root", roles=("project",))
                outgoing = repository.create_object("Synthetic Outgoing")
                incoming = repository.create_object("Synthetic Incoming")
                repository.create_relationship(
                    root.object_id, "depends_on", outgoing.object_id
                )
                repository.create_relationship(
                    incoming.object_id, "member_of", root.object_id
                )

            claim = ClaimAttribution(
                claimant_object_id=root.object_id,
                claimant_source_id="source-claim",
                claimant_label="Synthetic Root",
                stance="assert",
                claimed_at="2026-08-11T00:00:00Z",
                attribution_confidence=0.9,
            )
            second_claim = ClaimAttribution(
                claimant_object_id=incoming.object_id,
                claimant_source_id="source-claim-2",
                claimant_label="Synthetic Incoming",
                stance="deny",
                claimed_at="2026-08-11T00:00:00Z",
                attribution_confidence=0.8,
            )
            no_claim = revision("source-none", "candidate-none", "2026-08-11T00:00:01Z", (root.object_id,))
            claimed = revision("source-claim", "candidate-claim", "2026-08-11T00:00:02Z", (root.object_id, outgoing.object_id), claim=claim)
            second_claimed = revision("source-claim-2", "candidate-claim-2", "2026-08-11T00:00:03Z", (root.object_id, incoming.object_id), claim=second_claim)
            multi_first = revision("source-multi", "candidate-multi", "2026-08-11T00:00:04Z", (root.object_id,))
            multi_current = revision("source-multi", "candidate-multi", "2026-08-11T00:00:05Z", (root.object_id,), revision_number=2)
            information_store = JsonlAtomicInformationStore(information_path)
            information_store.ingest_batch((no_claim, claimed, second_claimed, multi_first))
            information_store.append_revision(multi_current)

            journal_store = JsonlChangeJournal(journal_path)
            journal_store.append(ChangeJournalRecord(
                "change-auto", claimed.atomic_information_id, claimed.revision_id,
                "add_role", (root.object_id,), "synthetic-auto", "automatic", None,
                "applied", "2026-08-11T00:01:00Z", "2026-08-11T00:01:01Z", None,
            ))
            journal_store.append(ChangeJournalRecord(
                "change-human", multi_current.atomic_information_id, multi_current.revision_id,
                "rename", (root.object_id,), "synthetic-human", "human_approved", "proposal-human",
                "applied", "2026-08-11T00:02:00Z", "2026-08-11T00:02:01Z", None,
            ))

            proposal_store = JsonlChangeProposalStore(proposal_path)
            review = HumanReviewContent("Synthetic finding", "medium", "Review", "Synthetic evidence", "Synthetic consequence")
            pending = ChangeProposal(
                "proposal-pending", no_claim.atomic_information_id, no_claim.revision_id,
                (WorldModelOperation("no_structural_change", target_object_id=root.object_id),),
                (root.object_id,), "Pending synthetic proposal", ("source-none",), "before", "interpretation-pending",
                review, "pending", "2026-08-11T00:03:00Z", None, None, None,
            )
            proposal_store.add_pending(pending)
            deferred = replace(
                pending,
                proposal_id="proposal-deferred",
                status="pending",
                created_at="2026-08-11T00:04:00Z",
                interpretation_fingerprint="interpretation-deferred",
            )
            proposal_store.add_pending(deferred)
            proposal_store.update(replace(deferred, status="deferred"))

            with SQLiteWorldModelRepository(database_path) as repository:
                before_world = (
                    repository.list_objects(),
                    repository.list_relationships(active_only=False),
                )
                before_artifacts = {
                    "atomic_information": information_path.read_bytes(),
                    "change_journal": journal_path.read_bytes(),
                    "change_proposals": proposal_path.read_bytes(),
                }
                builder = ContextBuilder(
                    repository,
                    ObjectResolver(repository),
                    information_store,
                    journal_store,
                    proposal_store,
                    clock=lambda: "2026-08-11T01:00:00Z",
                )
                request = ContextRequest("object", root.object_id, max_evidence_per_information=1)
                first = builder.build(request)
                second = builder.build(request)
                after_world = (
                    repository.list_objects(),
                    repository.list_relationships(active_only=False),
                )
                after_artifacts = {
                    "atomic_information": information_path.read_bytes(),
                    "change_journal": journal_path.read_bytes(),
                    "change_proposals": proposal_path.read_bytes(),
                }

            self.assertEqual(asdict(first), asdict(second))
            self.assertEqual(before_world, after_world)
            self.assertEqual(before_artifacts, after_artifacts)
            self.assertEqual({item.direction for item in first.relationships}, {"incoming", "outgoing"})
            self.assertEqual(len(first.atomic_information), 4)
            self.assertEqual(first.atomic_information[0].revision_count, 2)
            self.assertEqual(
                {item.claim.stance for item in first.atomic_information if item.claim},
                {"assert", "deny"},
            )
            self.assertEqual({item.mode for item in first.recent_changes}, {"automatic", "human_approved"})
            self.assertEqual({item.status for item in first.pending_judgments}, {"pending", "deferred"})
            self.assertTrue(first.metadata.complete)


if __name__ == "__main__":
    unittest.main()
