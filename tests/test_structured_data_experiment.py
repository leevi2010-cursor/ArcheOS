from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ROOT = ROOT / "docs" / "experiments" / "structured-data" / "v0.1.0"
SPEC = importlib.util.spec_from_file_location(
    "structured_data_experiment", EXPERIMENT_ROOT / "run_experiment.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("structured data experiment module could not be loaded")
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


class StructuredDataExperimentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.temp.name)
        cls.specs = experiment.load_fixture_specs(EXPERIMENT_ROOT / "fixtures")
        cls.versions = experiment.build_representations(cls.specs, cls.workspace)
        cls.result = experiment.analyze_representations(cls.versions)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_three_real_xlsx_representation_versions_are_verified(self) -> None:
        self.assertEqual(
            [item["version_id"] for item in self.versions], ["v1", "v2", "v3"]
        )
        self.assertTrue(
            all(
                item["representation_kind"] == "xlsx_structure"
                for item in self.versions
            )
        )
        self.assertTrue(
            all(item["representation_status"] == "complete" for item in self.versions)
        )

    def test_four_interpretation_layers_remain_explicit(self) -> None:
        self.assertEqual(
            set(self.result["versions"][0]["layers"]),
            {
                "observed_source_structure",
                "inferred_structure_candidate",
                "domain_schema_candidate",
                "business_canonical_mapping_candidate",
            },
        )

    def test_faithful_representation_is_not_overwritten(self) -> None:
        for source, analyzed in zip(
            self.versions, self.result["versions"], strict=True
        ):
            self.assertEqual(
                analyzed["layers"]["observed_source_structure"][
                    "representation_fingerprint"
                ],
                experiment._fingerprint(source["payload"]),
            )
        self.assertFalse(self.result["safety"]["faithful_representation_overwritten"])

    def test_non_unique_key_is_identity_evidence_not_object_truth(self) -> None:
        identity = self.result["versions"][0]["projection"]["identity_evidence"]
        self.assertFalse(identity["unique_within_version"])
        self.assertFalse(identity["automatic_object_binding_allowed"])
        self.assertGreater(self.result["data_quality_checks"]["non_unique_key"], 0)

    def test_safe_length_and_money_normalization_preserve_raw_and_evidence(
        self,
    ) -> None:
        first = self.result["versions"][0]["projection"]["records"][1]["values"]
        self.assertEqual(first["width_mm"]["value"], 2800)
        self.assertEqual(first["width_mm"]["raw_value"], "2.8米/进口皮/左贵妃")
        self.assertEqual(
            first["quoted_price"]["value"], {"currency": "CNY", "amount": 5600}
        )
        self.assertEqual(
            set(first["width_mm"]["evidence"]),
            {"source_id", "representation_id", "source_locator"},
        )

    def test_ambiguous_material_remains_unresolved(self) -> None:
        unresolved = self.result["versions"][0]["projection"]["records"][0][
            "unresolved"
        ]
        self.assertIn("ambiguous_free_text", {item["category"] for item in unresolved})
        self.assertNotIn(
            "material", self.result["versions"][0]["projection"]["records"][0]["values"]
        )

    def test_schema_and_header_drift_are_version_specific(self) -> None:
        self.assertEqual(self.result["data_quality_checks"]["schema_drift"], 2)
        self.assertEqual(self.result["data_quality_checks"]["header_drift"], 2)
        headers = [
            item["layers"]["inferred_structure_candidate"]["observed_headers"]
            for item in self.result["versions"]
        ]
        self.assertEqual(len({tuple(item) for item in headers}), 3)

    def test_conflict_and_explicit_replaces_are_not_collapsed(self) -> None:
        self.assertGreaterEqual(
            self.result["data_quality_checks"]["conflicting_values"], 1
        )
        self.assertGreaterEqual(
            self.result["data_quality_checks"]["temporal_ambiguity"], 1
        )
        temporal = self.result["temporal_interpretation_candidates"]
        self.assertTrue(
            any(item["locator"]["from_version"] == "v2" for item in temporal)
        )
        self.assertTrue(all(item["status"] == "candidate" for item in temporal))

    def test_mapping_change_regenerates_projection_without_source_change(self) -> None:
        before = [experiment._fingerprint(item["payload"]) for item in self.versions]
        revised = experiment.analyze_representations(
            self.versions,
            mapping_overrides={"v1": {"产品编号": "supplier_series_code"}},
        )
        after = [experiment._fingerprint(item["payload"]) for item in self.versions]
        self.assertEqual(before, after)
        self.assertNotEqual(
            self.result["versions"][0]["projection"]["mapping_revision"],
            revised["versions"][0]["projection"]["mapping_revision"],
        )

    def test_table_is_not_forced_into_atomic_information(self) -> None:
        self.assertEqual(self.result["metrics"]["rows_total"], 9)
        self.assertEqual(len(self.result["atomic_information_candidates"]), 2)
        self.assertFalse(self.result["safety"]["full_table_atomized"])

    def test_context_keeps_structured_information_and_warnings_distinct(self) -> None:
        kinds = [
            item["entry_kind"] for item in self.result["context_preview"]["entries"]
        ]
        self.assertEqual(
            kinds,
            [
                "existing_object_context",
                "normalized_structured_state",
                "atomic_information",
                "data_quality_warnings",
            ],
        )
        self.assertTrue(self.result["context_preview"]["bounded"])

    def test_mapping_candidates_expose_confidence_and_uncertainty(self) -> None:
        for version in self.result["versions"]:
            candidates = version["layers"]["business_canonical_mapping_candidate"]
            for candidate in candidates:
                self.assertIn("confidence", candidate)
                self.assertIn("uncertainty", candidate)

    def test_all_required_quality_checks_are_reported(self) -> None:
        self.assertEqual(
            set(self.result["data_quality_checks"]),
            {
                "identity_ambiguity",
                "non_unique_key",
                "schema_drift",
                "header_drift",
                "hidden_structure",
                "type_mismatch",
                "unit_inconsistency",
                "missing_values",
                "conflicting_values",
                "possible_duplicate",
                "temporal_ambiguity",
                "provenance_completeness",
            },
        )
        self.assertEqual(
            self.result["data_quality_checks"]["provenance_completeness"], "pass"
        )

    def test_provider_world_model_and_failure_residue_remain_zero(self) -> None:
        self.assertEqual(
            {
                key: self.result["safety"][key]
                for key in (
                    "provider_calls",
                    "world_model_writes",
                    "residue_items_from_runtime_failure",
                )
            },
            {
                "provider_calls": 0,
                "world_model_writes": 0,
                "residue_items_from_runtime_failure": 0,
            },
        )

    def test_interpretation_failure_fails_closed(self) -> None:
        malformed = json.loads(json.dumps(self.versions))
        malformed[0]["payload"]["sheets"][0]["cells"][0]["source_locator"]["cell"] = (
            "bad"
        )
        with self.assertRaises(experiment.StructuredDataExperimentError):
            experiment.analyze_representations(malformed)
        with self.assertRaises(experiment.StructuredDataExperimentError):
            experiment.analyze_representations(
                self.versions, mapping_overrides={"v1": {"名称": "product_code"}}
            )

    def test_fresh_runs_are_deterministic_and_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            result_one = experiment.run(EXPERIMENT_ROOT / "fixtures", Path(one))
            result_two = experiment.run(EXPERIMENT_ROOT / "fixtures", Path(two))
        self.assertEqual(result_one, result_two)

    def test_anonymous_metrics_and_recommendation_are_stable(self) -> None:
        self.assertEqual(
            self.result["metrics"],
            {
                "rows_total": 9,
                "rows_safe_to_normalize": 9,
                "rows_needing_review": 6,
                "schema_drift_count": 2,
                "non_unique_key_count": 1,
                "unit_issue_count": 3,
                "hidden_structure_count": 6,
                "conflict_count": 2,
                "unresolved_count": 12,
            },
        )
        self.assertEqual(self.result["architecture_recommendation"], "C")


if __name__ == "__main__":
    unittest.main()
