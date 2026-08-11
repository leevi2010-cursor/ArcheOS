from __future__ import annotations

import inspect
import json
import sys
import unittest
from dataclasses import asdict

import archeos.context.builder as context_builder_module
from archeos.atomic_information import (
    AtomicInformationRevision,
    ClaimAttribution,
    EvidenceRecord,
)
from archeos.context import ContextBuilder, ContextRequest
from archeos.digestion.models import (
    ChangeJournalRecord,
    ChangeProposal,
    HumanReviewContent,
    WorldModelOperation,
)
from archeos.world_model import LifecycleRecord, ObjectReadModel, RelationshipRecord


class FakeResolver:
    def __init__(self, objects: dict[str, ObjectReadModel], *, error: Exception | None = None):
        self.objects = objects
        self.error = error
        self.calls: list[str] = []

    def resolve(self, object_id: str) -> ObjectReadModel:
        self.calls.append(object_id)
        if self.error:
            raise self.error
        try:
            return self.objects[object_id]
        except KeyError as exc:
            raise ValueError(f"unknown object: {object_id}") from exc


class FakeWorld:
    def __init__(self, relationships: tuple[RelationshipRecord, ...] = ()):
        self.relationships = relationships
        self.calls: list[tuple[str | None, bool]] = []

    def list_relationships(self, *, object_id: str | None = None, active_only: bool = True):
        self.calls.append((object_id, active_only))
        return self.relationships


class FakeAtomic:
    def __init__(self, revisions: tuple[AtomicInformationRevision, ...]):
        self.revisions = revisions
        self.calls: list[tuple[str, ...] | str] = []

    def list_atomic_information(self):
        self.calls.append(("list",))
        current: dict[str, AtomicInformationRevision] = {}
        for item in self.revisions:
            current[item.atomic_information_id] = item
        return tuple(current.values())

    def list_revisions(self, atomic_information_id: str):
        self.calls.append(atomic_information_id)
        return tuple(item for item in self.revisions if item.atomic_information_id == atomic_information_id)


class FakeJournal:
    def __init__(self, records=()):
        self.records = tuple(records)
        self.calls = 0

    def list_changes(self):
        self.calls += 1
        return self.records


class FakeProposals:
    def __init__(self, proposals=()):
        self.proposals = tuple(proposals)
        self.calls = 0

    def list_unresolved(self):
        self.calls += 1
        return self.proposals


def obj(object_id: str, name: str, *, status: str = "active", lifecycle=None, roles=("person",)):
    return ObjectReadModel(object_id, name, tuple(roles), status, lifecycle)


def relation(rid: str, source: str, rel: str, target: str, *, confidence=0.7):
    return RelationshipRecord(rid, source, rel, target, "2026-01-01T00:00:00Z", None, "ai-1", confidence)


def evidence(index: int, *, excerpt: str | None = None):
    return EvidenceRecord("source-1", "meeting.wav", index, "Speaker_1", f"00:0{index}", f"00:1{index}", excerpt or f"quote {index}")


def atomic(aid: str, created: str, *, objects=("root",), revision=1, claim=None, evidence_count=1, statement="statement"):
    return AtomicInformationRevision(
        aid, revision, f"{aid}-r{revision}", "source-1", aid, f"fingerprint-{aid}", statement,
        "fact", (), tuple(objects), tuple(evidence(i) for i in range(1, evidence_count + 1)),
        "context", 0.8, created, "test", claim,
    )


def proposal(pid: str, status: str = "pending", *, resolved=(), operation=None, claim=None, aid="ai-1", created="2026-01-01T00:00:00Z"):
    return ChangeProposal(
        pid, aid, f"{aid}-r1", (operation,) if operation else (), tuple(resolved), "rationale",
        ("evidence-1",), "before", "interpretation", HumanReviewContent("finding", "high", "review", "evidence", "consequence"),
        status, created, None, "claim summary" if claim else None, claim,
    )


def change(cid: str, status="applied", *, resolved=("root",), created="2026-01-01T00:00:00Z"):
    return ChangeJournalRecord(cid, "ai-1", "ai-1-r1", "rename", tuple(resolved), "interpretation", "automatic", None, status, created, created if status == "applied" else None, None)


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = LifecycleRecord("life-1", "root", "2026-01-01", None, None, None, "active", "2026-01-01", None)
        self.objects = {"root": obj("root", "Root", lifecycle=self.lifecycle), "b": obj("b", "Bravo"), "c": obj("c", "Charlie")}
        self.world = FakeWorld()
        self.atomic_store = FakeAtomic(())
        self.journal = FakeJournal()
        self.proposals = FakeProposals()
        self.resolver = FakeResolver(self.objects)
        self.builder = ContextBuilder(self.world, self.resolver, self.atomic_store, self.journal, self.proposals, clock=lambda: "fixed")

    def build(self, **kwargs):
        return self.builder.build(ContextRequest("object", "root", **kwargs))

    def test_01_root_current_state(self):
        root = self.build().root
        self.assertEqual((root.current_name, root.roles, root.lifecycle.state), ("Root", ("person",), "active"))

    def test_02_rename_is_current(self):
        self.objects["root"] = obj("root", "Renamed")
        self.assertEqual(self.build().root.current_name, "Renamed")

    def test_03_no_lifecycle(self):
        self.objects["root"] = obj("root", "Root", lifecycle=None)
        self.assertIsNone(self.build().root.lifecycle)

    def test_04_deleted_root_readable(self):
        self.objects["root"] = obj("root", "Root", status="deleted")
        self.assertEqual(self.build().root.status, "deleted")

    def test_05_unknown_root_fails(self):
        with self.assertRaises(ValueError):
            self.builder.build(ContextRequest("object", "missing"))

    def test_06_resolver_invariant_fails(self):
        self.resolver.error = ValueError("multiple primary names")
        with self.assertRaises(ValueError):
            self.build()

    def test_07_outgoing_relationship(self):
        self.world.relationships = (relation("r1", "root", "depends_on", "b"),)
        self.assertEqual((self.build().relationships[0].direction, self.build().relationships[0].neighbor.object_id), ("outgoing", "b"))

    def test_08_incoming_relationship(self):
        self.world.relationships = (relation("r1", "b", "member_of", "root"),)
        self.assertEqual(self.build().relationships[0].direction, "incoming")

    def test_09_five_relationships(self):
        self.world.relationships = tuple(relation(f"r{i}", "root", rel, "b") for i, rel in enumerate(("part_of", "member_of", "responsible_for", "depends_on", "related_to")))
        self.assertEqual(len(self.build().relationships), 5)

    def test_10_no_inverse_relationship(self):
        self.world.relationships = (relation("r1", "root", "depends_on", "b"),)
        self.assertEqual(len(self.build().relationships), 1)

    def test_11_no_recursive_relationships(self):
        self.world.relationships = (relation("r1", "root", "depends_on", "b"),)
        self.assertEqual({x.neighbor.object_id for x in self.build().relationships}, {"b"})

    def test_12_shared_neighbor_is_preserved(self):
        self.world.relationships = (relation("r1", "root", "related_to", "b"), relation("r2", "root", "member_of", "b"))
        self.assertEqual(len(self.build().relationships), 2)

    def test_13_unknown_relationship_fails(self):
        self.world.relationships = (relation("r1", "root", "unknown", "b"),)
        with self.assertRaises(ValueError):
            self.build()

    def test_14_neighbor_rename(self):
        self.world.relationships = (relation("r1", "root", "related_to", "b"),)
        self.objects["b"] = obj("b", "Renamed Bravo")
        self.assertEqual(self.build().relationships[0].neighbor.current_name, "Renamed Bravo")

    def test_15_only_related_information(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-02"), atomic("ai-2", "2026-01-03", objects=("other",)))
        self.assertEqual([x.atomic_information_id for x in self.build().atomic_information], ["ai-1"])

    def test_16_raw_concern_without_binding_excluded(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", objects=()),)
        self.assertFalse(self.build().atomic_information)

    def test_17_revision_count(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", revision=1), atomic("ai-1", "2026-01-02", revision=2))
        item = self.build().atomic_information[0]
        self.assertEqual((item.revision_number, item.revision_count), (2, 2))

    def test_18_shared_information(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", objects=("root", "b")),)
        self.assertEqual(self.build().atomic_information[0].related_object_ids, ("root", "b"))

    def test_19_claim_none(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01"),)
        self.assertIsNone(self.build().atomic_information[0].claim)

    def test_20_claim_fields(self):
        claim = ClaimAttribution("root", "source-1", "Root", "assert", "2026-01-01", 0.9)
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", claim=claim),)
        self.assertEqual(self.build().atomic_information[0].claim, claim)

    def test_21_conflicting_claims_both_visible(self):
        claim1 = ClaimAttribution("root", "s1", "Root", "assert", None, 0.8)
        claim2 = ClaimAttribution("root", "s2", "Root", "deny", None, 0.8)
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", claim=claim1), atomic("ai-2", "2026-01-02", claim=claim2))
        self.assertEqual(len(self.build().atomic_information), 2)

    def test_22_human_claim_revision_is_current(self):
        claim = ClaimAttribution("root", "s2", "Root", "deny", None, 1.0)
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", revision=1), atomic("ai-1", "2026-01-02", revision=2, claim=claim))
        self.assertEqual((self.build().atomic_information[0].revision_number, self.build().atomic_information[0].claim), (2, claim))

    def test_23_evidence_within_limit(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", evidence_count=2),)
        self.assertEqual(len(self.build(max_evidence_per_information=2).atomic_information[0].source_evidence), 2)

    def test_24_evidence_truncated_first_n(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", evidence_count=3),)
        bundle = self.build(max_evidence_per_information=2)
        self.assertEqual([e.segment for e in bundle.atomic_information[0].source_evidence], [1, 2])
        self.assertFalse(bundle.metadata.complete)

    def test_25_no_raw_payload(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01"),)
        self.assertNotIn("transcript", json.dumps(asdict(self.build())))

    def test_26_automatic_change(self):
        self.journal.records = (change("c1"),)
        self.assertEqual(self.build().recent_changes[0].change_id, "c1")

    def test_27_human_change(self):
        self.journal.records = (ChangeJournalRecord("c1", "ai-1", "ai-1-r1", "rename", ("root",), "i", "human", "p1", "applied", "2026-01-01", "2026-01-01", None),)
        self.assertEqual(self.build().recent_changes[0].mode, "human")

    def test_28_unrelated_change_excluded(self):
        self.journal.records = (change("c1", resolved=("other",)),)
        self.assertFalse(self.build().recent_changes)

    def test_29_failed_change_excluded(self):
        self.journal.records = (change("c1", status="failed"),)
        self.assertFalse(self.build().recent_changes)

    def test_30_receipt_not_shown(self):
        self.journal.records = (change("c1"),)
        self.assertNotIn("receipt", json.dumps(asdict(self.build())))

    def test_31_pending_judgment(self):
        self.proposals.proposals = (proposal("p1", resolved=("root",)),)
        self.assertEqual(self.build().pending_judgments[0].status, "pending")

    def test_32_deferred_judgment(self):
        self.proposals.proposals = (proposal("p1", "deferred", resolved=("root",)),)
        self.assertEqual(self.build().pending_judgments[0].status, "deferred")

    def test_33_approved_rejected_excluded(self):
        self.proposals.proposals = (proposal("p1", "approved", resolved=("root",)), proposal("p2", "rejected", resolved=("root",)))
        self.assertFalse(self.build().pending_judgments)

    def test_34_conflict_proposal_does_not_mutate_root(self):
        before = self.objects["root"]
        self.proposals.proposals = (proposal("p1", resolved=("root",), operation=WorldModelOperation("conflict", "root")),)
        self.build()
        self.assertEqual(self.objects["root"], before)

    def test_35_target_secondary_matching(self):
        self.proposals.proposals = (proposal("p1", operation=WorldModelOperation("create_relationship", "b", "root")),)
        self.assertEqual(len(self.build().pending_judgments), 1)

    def test_36_bound_atomic_matches_pending(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01"),)
        self.proposals.proposals = (proposal("p1"),)
        self.assertEqual(len(self.build().pending_judgments), 1)

    def test_37_relationship_limit(self):
        self.world.relationships = tuple(relation(f"r{i}", "root", "related_to", "b") for i in range(3))
        bundle = self.build(max_relationships=2)
        self.assertEqual((len(bundle.relationships), bundle.metadata.relationships.total), (2, 3))

    def test_38_atomic_limit(self):
        self.atomic_store.revisions = tuple(atomic(f"ai-{i}", f"2026-01-0{i}") for i in range(1, 4))
        self.assertTrue(self.build(max_atomic_information=2).metadata.atomic_information.truncated)

    def test_39_change_limit(self):
        self.journal.records = tuple(change(f"c{i}", created=f"2026-01-0{i}") for i in range(1, 4))
        self.assertTrue(self.build(max_changes=2).metadata.recent_changes.truncated)

    def test_40_pending_limit(self):
        self.proposals.proposals = tuple(proposal(f"p{i}", resolved=("root",), created=f"2026-01-0{i}") for i in range(1, 4))
        self.assertTrue(self.build(max_pending_judgments=2).metadata.pending_judgments.truncated)

    def test_41_evidence_limit_reason(self):
        self.atomic_store.revisions = (atomic("ai-1", "2026-01-01", evidence_count=2),)
        self.assertIn("evidence_limit", self.build(max_evidence_per_information=1).metadata.incomplete_reasons)

    def test_42_any_section_reason(self):
        self.world.relationships = tuple(relation(f"r{i}", "root", "related_to", "b") for i in range(2))
        self.assertIn("relationships_limit", self.build(max_relationships=1).metadata.incomplete_reasons)

    def test_43_no_truncation_complete(self):
        self.assertTrue(self.build().metadata.complete)

    def test_44_invalid_limits(self):
        for value in (0, -1, True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ContextRequest("object", "root", max_changes=value)

    def test_45_fixed_clock_is_stable(self):
        self.assertEqual(asdict(self.build()), asdict(self.build()))

    def test_46_sorting_is_insertion_order_independent(self):
        self.atomic_store.revisions = (atomic("ai-2", "2026-01-02"), atomic("ai-1", "2026-01-02"))
        first = [x.atomic_information_id for x in self.build().atomic_information]
        self.atomic_store.revisions = tuple(reversed(self.atomic_store.revisions))
        self.assertEqual(first, [x.atomic_information_id for x in self.build().atomic_information])

    def test_47_builder_uses_read_only_methods(self):
        self.assertEqual(self.world.calls, [])
        self.build()
        self.assertEqual(self.world.calls, [("root", True)])

    def test_48_state_unchanged(self):
        snapshot = (self.objects.copy(), self.world.relationships, self.atomic_store.revisions, self.journal.records, self.proposals.proposals)
        self.build()
        self.assertEqual(snapshot, (self.objects, self.world.relationships, self.atomic_store.revisions, self.journal.records, self.proposals.proposals))

    def test_49_no_model_or_network_dependency(self):
        self.assertFalse(any("codex" in name.lower() or "requests" in name.lower() for name in sys.modules if name.startswith("archeos.context")))

    def test_50_full_bundle_is_json_serializable(self):
        json.dumps(asdict(self.build()), ensure_ascii=False)

    def test_51_context_layer_has_no_persistence_adapter_imports(self):
        source = inspect.getsource(context_builder_module)
        self.assertNotIn("sqlite", source.lower())
        self.assertNotIn("jsonl", source.lower())


if __name__ == "__main__":
    unittest.main()
