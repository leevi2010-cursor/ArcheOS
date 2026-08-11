from __future__ import annotations

import inspect
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import archeos.atomic_information.jsonl_store as atomic_store_adapter
import archeos.world_model.sqlite_repository as world_model_adapter
from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    IngestionResult,
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
from archeos.world_model import ObjectResolver, SQLiteWorldModelRepository


class FakeInterpretationProvider:
    name = "fake"

    def __init__(self, result: InterpretationResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def interpret(self, atomic_information, current_world_state):
        del atomic_information, current_world_state
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def interpretation(
    *operations: WorldModelOperation,
    evidence_sufficient: bool = True,
    conflict: bool = False,
    ambiguous: bool = False,
) -> InterpretationResult:
    return InterpretationResult(
        operations=operations,
        rationale="Synthetic interpretation for governed digestion.",
        evidence_sufficient=evidence_sufficient,
        conflict=conflict,
        ambiguous=ambiguous,
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
        self.assertEqual(len(self.proposals.list_pending()), 1)
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
        self.assertEqual(role.confidence, 0.9)
        self.assertEqual(self.proposals.list_pending(), ())

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
        self.assertEqual(self.proposals.list_pending(), ())

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
        self.assertEqual(self.proposals.list_pending(), ())
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
            }
        )
        observed: dict[str, object] = {}

        class Result:
            final_response = payload

        class Thread:
            def run(self, prompt, **kwargs):
                observed["prompt"] = prompt
                observed["run"] = kwargs
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
        self.assertIn("output_schema", observed["run"])


if __name__ == "__main__":
    unittest.main()
