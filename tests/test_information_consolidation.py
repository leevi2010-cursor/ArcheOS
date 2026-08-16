from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from archeos.atomic_information import (
    AtomicInformationRevision,
    ClaimAttribution,
    EvidenceRecord,
    JsonlAtomicInformationStore,
)
from archeos.consolidation import (
    ACTIVE_INFORMATION_RELATIONS,
    DEFAULT_TOP_K,
    MAX_CANDIDATE_POOL,
    MAX_TOP_K,
    BoundedInformationCandidateRetriever,
    ConsolidatedInformationViewBuilder,
    InformationComparisonProjection,
    InformationRelationJudgment,
)


def _revision(
    candidate: str,
    statement: str,
    *,
    source: str = "src_" + "a" * 32,
    concerns: tuple[str, ...] = ("synthetic concern",),
    revision_number: int = 1,
    claimed_at: str | None = None,
    representation: str | None = "repr_synthetic",
    evidence_count: int = 1,
) -> AtomicInformationRevision:
    atomic_id = (
        "atomic_info_"
        + hashlib.sha256(f"{source}\0{candidate}".encode()).hexdigest()[:32]
    )
    evidence = tuple(
        EvidenceRecord(
            source_id=source,
            artifact="synthetic.txt",
            segment=index,
            speaker="Synthetic Speaker" if claimed_at else None,
            start=None,
            end=None,
            excerpt=f"Synthetic evidence {index}.",
            representation_id=representation,
            representation_kind=("synthetic" if representation else None),
            artifact_id=(f"artifact-{index}" if representation else None),
            unit_id=(f"unit-{index}" if representation else None),
            locator=(f"line:{index}" if representation else None),
        )
        for index in range(1, evidence_count + 1)
    )
    claim = (
        None
        if claimed_at is None
        else ClaimAttribution(
            claimant_object_id=None,
            claimant_source_id=source,
            claimant_label="Synthetic Speaker",
            stance="assert",
            claimed_at=claimed_at,
            attribution_confidence=1.0,
        )
    )
    return AtomicInformationRevision(
        atomic_information_id=atomic_id,
        revision_number=revision_number,
        revision_id=f"{atomic_id}-r{revision_number:04d}",
        origin_source_id=source,
        origin_candidate_id=candidate,
        origin_fingerprint=hashlib.sha256(candidate.encode()).hexdigest(),
        statement=statement,
        semantic_type="observation",
        raw_concerns=concerns,
        related_object_ids=(),
        source_evidence=evidence,
        context="Synthetic test context.",
        confidence=1.0,
        created_at=f"2026-01-{revision_number:02d}T00:00:00+00:00",
        revision_reason="synthetic test",
        claim=claim,
    )


def _judgment(
    left: AtomicInformationRevision,
    right: AtomicInformationRevision,
    relation: str,
    *,
    left_span: tuple[int, int] | None = None,
    right_span: tuple[int, int] | None = None,
    left_time: str | None = None,
    right_time: str | None = None,
) -> InformationRelationJudgment:
    left_projection = InformationComparisonProjection.from_revision(
        left,
        start_offset=0 if left_span is None else left_span[0],
        end_offset=len(left.statement) if left_span is None else left_span[1],
    )
    right_projection = InformationComparisonProjection.from_revision(
        right,
        start_offset=0 if right_span is None else right_span[0],
        end_offset=len(right.statement) if right_span is None else right_span[1],
    )
    return InformationRelationJudgment.create(
        left_projection,
        right_projection,
        relation,
        left_revision=left,
        right_revision=right,
        provenance_basis=("synthetic review",),
        evidence_independence="unknown",
        uncertainty=("requires review" if relation == "uncertain" else None),
        left_time=left_time,
        right_time=right_time,
    )


class ProjectionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.revision = _revision("projection", "Alpha beta gamma.")

    def test_full_statement_projection_is_valid(self) -> None:
        projection = InformationComparisonProjection.from_revision(self.revision)
        self.assertEqual(projection.projection_text, self.revision.statement)
        self.assertTrue(projection.is_full_statement)
        projection.validate_against(self.revision)

    def test_contiguous_span_is_valid_and_verifiable(self) -> None:
        projection = InformationComparisonProjection.from_revision(
            self.revision, start_offset=6, end_offset=10
        )
        self.assertEqual(projection.projection_text, "beta")
        self.assertFalse(projection.is_full_statement)
        projection.validate_against(self.revision)

    def test_empty_or_out_of_bounds_span_fails_closed(self) -> None:
        for start, end in ((-1, 4), (0, 100), (4, 4), (8, 3)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                InformationComparisonProjection.from_revision(
                    self.revision, start_offset=start, end_offset=end
                )

    def test_projection_text_or_fingerprint_mismatch_fails_closed(self) -> None:
        projection = InformationComparisonProjection.from_revision(self.revision)
        with self.assertRaisesRegex(ValueError, "projection text"):
            replace(projection, projection_text="Changed").validate_against(
                self.revision
            )
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            replace(projection, projection_fingerprint="0" * 64).validate_against(
                self.revision
            )

    def test_revision_mismatch_is_stale_and_fails_closed(self) -> None:
        projection = InformationComparisonProjection.from_revision(self.revision)
        current = replace(
            self.revision,
            revision_number=2,
            revision_id=f"{self.revision.atomic_information_id}-r0002",
            statement="Alpha beta gamma revised.",
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            projection.validate_against(current)

    def test_projection_never_modifies_revision(self) -> None:
        before = self.revision
        InformationComparisonProjection.from_revision(
            self.revision, start_offset=0, end_offset=5
        )
        self.assertEqual(self.revision, before)


class RetrievalContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.query = _revision(
            "query",
            "Synthetic metric on 2026-01-02 is 42 units.",
            concerns=("quality metric",),
        )
        self.retriever = BoundedInformationCandidateRetriever()

    def test_exact_normalized_basis(self) -> None:
        candidate = _revision(
            "exact",
            " synthetic METRIC on 2026 01 02 is 42 units ",
            source="src_" + "b" * 32,
            concerns=("different",),
        )
        result = self.retriever.retrieve(self.query, (candidate,))
        self.assertIn("exact_normalized", result.candidates[0].retrieval_basis)

    def test_same_source_basis(self) -> None:
        candidate = _revision("source", "Unmatched synthetic text.")
        result = self.retriever.retrieve(self.query, (candidate,))
        self.assertIn("same_source", result.candidates[0].retrieval_basis)
        self.assertEqual(result.candidates[0].source_relationship, "same")

    def test_raw_concern_overlap_basis(self) -> None:
        candidate = _revision(
            "concern",
            "Distinct statement.",
            source="src_" + "b" * 32,
            concerns=("quality metric review",),
        )
        result = self.retriever.retrieve(self.query, (candidate,))
        self.assertIn("raw_concern_overlap", result.candidates[0].retrieval_basis)

    def test_number_date_and_key_token_basis(self) -> None:
        candidate = _revision(
            "token",
            "Reference 2026-01-02 and code A42.",
            source="src_" + "b" * 32,
            concerns=("different",),
        )
        result = self.retriever.retrieve(self.query, (candidate,))
        self.assertIn("number_date_key_token", result.candidates[0].retrieval_basis)
        self.assertTrue(result.candidates[0].time_available)

    def test_bounded_lexical_basis(self) -> None:
        candidate = _revision(
            "lexical",
            "Synthetic metric quality changed substantially.",
            source="src_" + "b" * 32,
            concerns=("different",),
        )
        result = self.retriever.retrieve(self.query, (candidate,))
        self.assertIn("bounded_lexical", result.candidates[0].retrieval_basis)

    def test_priority_and_tie_break_are_stable(self) -> None:
        exact = _revision(
            "z-exact",
            self.query.statement,
            source="src_" + "b" * 32,
            concerns=("different",),
        )
        same_source = _revision("a-source", "Completely different content.")
        first = self.retriever.retrieve(self.query, (same_source, exact), top_k=2)
        second = self.retriever.retrieve(self.query, (exact, same_source), top_k=2)
        self.assertEqual(
            [item.atomic_information_id for item in first.candidates],
            [item.atomic_information_id for item in second.candidates],
        )
        self.assertEqual(
            first.candidates[0].atomic_information_id, exact.atomic_information_id
        )
        self.assertEqual([item.rank for item in first.candidates], [1, 2])

    def test_default_and_configurable_top_k_are_bounded(self) -> None:
        pool = tuple(
            _revision(f"candidate-{index}", f"Synthetic metric candidate {index}.")
            for index in range(DEFAULT_TOP_K + 3)
        )
        result = self.retriever.retrieve(self.query, pool)
        self.assertEqual(result.metadata.top_k, DEFAULT_TOP_K)
        self.assertEqual(len(result.candidates), DEFAULT_TOP_K)
        self.assertTrue(result.metadata.truncated)
        configured = self.retriever.retrieve(self.query, pool, top_k=3)
        self.assertEqual(len(configured.candidates), 3)
        for invalid in (0, MAX_TOP_K + 1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.retriever.retrieve(self.query, pool, top_k=invalid)

    def test_candidate_pool_must_be_bounded(self) -> None:
        pool = tuple(
            _revision(f"bounded-{index}", f"Synthetic {index}.")
            for index in range(MAX_CANDIDATE_POOL + 1)
        )
        with self.assertRaisesRegex(ValueError, "candidate_pool"):
            self.retriever.retrieve(self.query, pool)

    def test_self_is_not_returned_and_outside_pool_cannot_appear(self) -> None:
        inside = _revision("inside", "Synthetic metric inside.")
        outside = _revision("outside", "Synthetic metric outside.")
        result = self.retriever.retrieve(self.query, (self.query, inside))
        ids = {item.atomic_information_id for item in result.candidates}
        self.assertNotIn(self.query.atomic_information_id, ids)
        self.assertIn(inside.atomic_information_id, ids)
        self.assertNotIn(outside.atomic_information_id, ids)

    def test_explainability_and_completeness_are_disclosed(self) -> None:
        candidate = _revision(
            "metadata",
            "Synthetic metric 42.",
            claimed_at="2026-01-02",
        )
        result = self.retriever.retrieve(self.query, (candidate,), pool_complete=False)
        item = result.candidates[0]
        self.assertTrue(item.retrieval_basis)
        self.assertTrue(item.representation_available)
        self.assertTrue(item.claimant_available)
        self.assertFalse(result.metadata.pool_complete)
        self.assertFalse(result.metadata.complete)
        self.assertEqual(result.metadata.candidate_pool_size, 1)

    def test_retrieval_never_creates_relation_truth(self) -> None:
        candidate = _revision("candidate", "Synthetic metric 42.")
        result = self.retriever.retrieve(self.query, (candidate,))
        self.assertFalse(hasattr(result.candidates[0], "relation"))
        self.assertFalse(hasattr(result, "judgments"))


class RelationJudgmentContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.left = _revision("left", "Alpha scope. Extra detail.")
        self.right = _revision("right", "Alpha scope.", source="src_" + "b" * 32)

    def test_all_six_active_relations_are_supported(self) -> None:
        self.assertEqual(len(ACTIVE_INFORMATION_RELATIONS), 6)
        for relation in ACTIVE_INFORMATION_RELATIONS:
            with self.subTest(relation=relation):
                judgment = _judgment(self.left, self.right, relation)
                self.assertEqual(judgment.relation, relation)

    def test_unrelated_is_not_an_active_judgment(self) -> None:
        with self.assertRaisesRegex(ValueError, "active relation"):
            _judgment(self.left, self.right, "unrelated")

    def test_pair_order_is_deterministic(self) -> None:
        forward = _judgment(
            self.left,
            self.right,
            "temporal_update",
            left_time="2026-01-01",
            right_time="2026-01-02",
        )
        reverse = _judgment(
            self.right,
            self.left,
            "temporal_update",
            left_time="2026-01-02",
            right_time="2026-01-01",
        )
        self.assertEqual(forward, reverse)

    def test_judgment_scope_remains_projection_scoped(self) -> None:
        judgment = _judgment(
            self.left,
            self.right,
            "equivalent",
            left_span=(0, 12),
        )
        self.assertFalse(judgment.left_projection.is_full_statement)
        self.assertNotEqual(
            judgment.left_projection.projection_text, self.left.statement
        )

    def test_stale_judgment_fails_when_view_validates_current_revisions(self) -> None:
        judgment = _judgment(self.left, self.right, "complementary")
        current_left = replace(
            self.left,
            revision_number=2,
            revision_id=f"{self.left.atomic_information_id}-r0002",
            statement="Current statement.",
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            ConsolidatedInformationViewBuilder().build(
                (current_left, self.right), (judgment,)
            )

    def test_conflict_and_uncertain_never_become_truth(self) -> None:
        third = _revision("third", "Third scope.")
        fourth = _revision("fourth", "Fourth scope.", source="src_" + "c" * 32)
        judgments = (
            _judgment(self.left, self.right, "conflict"),
            _judgment(third, fourth, "uncertain"),
        )
        revisions = (self.left, self.right, third, fourth)
        view = ConsolidatedInformationViewBuilder().build(revisions, judgments)
        self.assertIn("conflict", view.metadata.relation_states)
        self.assertIn("uncertain", view.metadata.relation_states)
        self.assertFalse(hasattr(view, "truth"))


class GroupedViewContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.first = _revision(
            "first",
            "Synthetic state at first time.",
            representation="repr_first",
            evidence_count=2,
        )
        self.second = _revision(
            "second",
            "Synthetic state at second time.",
            source="src_" + "b" * 32,
            representation="repr_second",
            evidence_count=2,
        )
        self.third = _revision(
            "third",
            "Synthetic independent detail.",
            source="src_" + "c" * 32,
            representation="repr_third",
            evidence_count=1,
        )
        self.builder = ConsolidatedInformationViewBuilder()

    def test_no_judgments_preserves_raw_information_access(self) -> None:
        revisions = (self.first, self.second, self.third)
        view = self.builder.build(revisions)
        self.assertEqual(view.raw_information, revisions)
        self.assertEqual(view.ungrouped_information, revisions)
        self.assertEqual(view.groups, ())
        self.assertEqual(view.metadata.total_information, 3)
        self.assertEqual(view.metadata.included_information, 3)

    def test_equivalent_and_derived_never_delete_information_or_evidence(self) -> None:
        for relation in ("equivalent", "derived"):
            with self.subTest(relation=relation):
                view = self.builder.build(
                    (self.first, self.second),
                    (_judgment(self.first, self.second, relation),),
                )
                self.assertEqual(len(view.raw_information), 2)
                self.assertEqual(view.metadata.evidence_count, 4)
                self.assertEqual(view.metadata.independent_source_count, 2)
                self.assertEqual(view.groups[0].evidence_count, 4)

    def test_complementary_conflict_and_uncertain_remain_explicit_and_parallel(
        self,
    ) -> None:
        relations = ("complementary", "conflict", "uncertain")
        for relation in relations:
            with self.subTest(relation=relation):
                view = self.builder.build(
                    (self.first, self.second),
                    (_judgment(self.first, self.second, relation),),
                )
                group = view.groups[0]
                self.assertEqual(group.relation_states, (relation,))
                self.assertEqual(len(group.information), 2)
        uncertain_view = self.builder.build(
            (self.first, self.second),
            (_judgment(self.first, self.second, "uncertain"),),
        )
        self.assertEqual(uncertain_view.metadata.pending_or_uncertain_count, 1)

    def test_temporal_order_uses_explicit_time_only(self) -> None:
        judgment = _judgment(
            self.first,
            self.second,
            "temporal_update",
            left_time="2026-01-02",
            right_time="2026-01-01",
        )
        view = self.builder.build((self.first, self.second), (judgment,))
        group = view.groups[0]
        self.assertTrue(group.temporal_order_complete)
        self.assertEqual(
            group.display_projections[0].atomic_information_id,
            self.second.atomic_information_id,
        )

    def test_temporal_order_does_not_guess_when_time_is_missing(self) -> None:
        judgment = _judgment(
            self.first,
            self.second,
            "temporal_update",
            left_time="2026-01-01",
        )
        group = self.builder.build((self.first, self.second), (judgment,)).groups[0]
        self.assertFalse(group.temporal_order_complete)
        self.assertEqual(
            group.display_projections,
            tuple(sorted(group.projections, key=lambda item: item.sort_key)),
        )

    def test_different_projections_of_same_information_can_join_different_groups(
        self,
    ) -> None:
        split = _revision("split", "Alpha projection. Beta projection.")
        alpha = _revision("alpha", "Alpha projection.", source="src_" + "b" * 32)
        beta = _revision("beta", "Beta projection.", source="src_" + "c" * 32)
        judgments = (
            _judgment(split, alpha, "equivalent", left_span=(0, 17)),
            _judgment(split, beta, "complementary", left_span=(18, 34)),
        )
        view = self.builder.build((split, alpha, beta), judgments)
        self.assertEqual(len(view.groups), 2)
        split_memberships = sum(
            any(
                item.atomic_information_id == split.atomic_information_id
                for item in group.projections
            )
            for group in view.groups
        )
        self.assertEqual(split_memberships, 2)
        self.assertEqual(len(view.raw_information), 3)

    def test_metadata_and_group_summary_boundary(self) -> None:
        view = self.builder.build(
            (self.first, self.second, self.third),
            (_judgment(self.first, self.second, "conflict"),),
            retrieval_completeness="caller_bounded",
        )
        self.assertEqual(view.metadata.total_information, 3)
        self.assertEqual(view.metadata.included_information, 3)
        self.assertEqual(view.metadata.group_count, 1)
        self.assertEqual(view.metadata.evidence_count, 5)
        self.assertEqual(view.metadata.independent_source_count, 3)
        self.assertEqual(view.metadata.representation_count, 3)
        self.assertEqual(view.metadata.retrieval_completeness, "caller_bounded")
        self.assertFalse(hasattr(view.groups[0], "summary"))

    def test_store_and_world_model_bytes_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            information_path = root / "atomic_information.jsonl"
            world_model_path = root / "world_model.sqlite3"
            store = JsonlAtomicInformationStore(information_path)
            store.ingest_batch((self.first, self.second))
            world_model_path.write_bytes(b"synthetic-world-model-bytes")
            information_before = information_path.read_bytes()
            world_model_before = world_model_path.read_bytes()

            revisions = store.list_atomic_information()
            self.builder.build(
                revisions, (_judgment(revisions[0], revisions[1], "derived"),)
            )

            self.assertEqual(information_path.read_bytes(), information_before)
            self.assertEqual(world_model_path.read_bytes(), world_model_before)


if __name__ == "__main__":
    unittest.main()
