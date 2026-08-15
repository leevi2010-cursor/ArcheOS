from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
EXPERIMENT = (
    REPOSITORY
    / "docs"
    / "experiments"
    / "external-agent-handoff"
    / "v0.1.0"
)
SPEC = importlib.util.spec_from_file_location(
    "external_agent_handoff_gate", EXPERIMENT / "run_synthetic_gate.py"
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class ExternalAgentHandoffGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "audit-artifacts"
        self.package = gate.load_synthetic_package()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mode(self, mode: str, *, timeout: float = 3.0):
        return gate.execute_handoff(
            self.package,
            provider_route="synthetic-fake-external-agent",
            provider_version="test-v1",
            invocation_builder=gate.synthetic_invocation_builder(mode),
            output_root=self.output_root,
            timeout_seconds=timeout,
        )

    def assert_failed(self, mode: str, category: str, *, timeout: float = 3.0):
        run = self.run_mode(mode, timeout=timeout)
        self.assertEqual(run.audit["execution_status"], "failed")
        self.assertEqual(run.audit["failure_category"], category)
        self.assertFalse(run.audit["result_present"])
        self.assertIsNone(run.audit["result_fingerprint"])
        self.assertIsNone(run.result_path)
        self.assertFalse(run.audit["package_published"])
        self.assertFalse(run.audit["information_ingested"])
        readback = json.loads(run.audit_path.read_text(encoding="utf-8"))
        self.assertEqual(readback, run.audit)
        self.assertEqual(run.audit["audit_readback_status"], "verified")
        return run

    def test_normal_result_passes_privacy_binding_coverage_and_readback(self) -> None:
        run = self.run_mode("valid")

        self.assertEqual(run.audit["execution_status"], "succeeded")
        self.assertEqual(run.audit["privacy_observation_status"], "passed")
        observation = run.audit["privacy_observation"]
        self.assertTrue(observation["root_observed"])
        self.assertGreaterEqual(observation["observed_processes"], 2)
        self.assertEqual(observation["argv_sensitive_hits"], 0)
        self.assertEqual(observation["environment_sensitive_hits"], 0)
        self.assertEqual(run.audit["eligible_units"], 2)
        self.assertEqual(run.audit["covered_units"], 2)
        self.assertEqual(run.audit["unaccounted_units"], 0)
        self.assertEqual(run.audit["result_readback_status"], "verified")
        assert run.result_path is not None
        result = json.loads(run.result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["input_fingerprint"], gate.fingerprint(self.package))
        self.assertEqual(run.audit["result_fingerprint"], gate.fingerprint(result))

    def test_timeout_is_audited_and_fail_closed(self) -> None:
        run = self.assert_failed("timeout", "timeout", timeout=0.05)
        self.assertEqual(run.audit["cleanup_status"], "verified")

    def test_nonzero_runtime_failure_is_audited_and_fail_closed(self) -> None:
        self.assert_failed("runtime_failure", "runtime_failure")

    def test_missing_and_empty_results_are_distinct_failures(self) -> None:
        self.assert_failed("no_result", "no_result")
        self.assert_failed("empty_result", "empty_result")

    def test_invalid_json_is_rejected(self) -> None:
        run = self.assert_failed("invalid_json", "invalid_json")
        self.assertEqual(run.audit["strict_validation_status"], "failed")

    def test_unknown_unit_reference_is_rejected(self) -> None:
        self.assert_failed("unknown_ref", "result_contract_failure")

    def test_duplicate_unit_reference_is_rejected(self) -> None:
        self.assert_failed("duplicate_ref", "result_contract_failure")

    def test_incomplete_coverage_is_rejected(self) -> None:
        self.assert_failed("incomplete_coverage", "result_contract_failure")

    def test_wrong_input_fingerprint_is_rejected(self) -> None:
        self.assert_failed("wrong_fingerprint", "result_binding_failure")

    def test_argv_canary_leak_fails_the_route(self) -> None:
        run = self.assert_failed("argv_leak", "privacy_boundary_violation")
        self.assertEqual(run.audit["privacy_observation_status"], "failed")
        self.assertGreater(run.audit["privacy_observation"]["argv_sensitive_hits"], 0)

    def test_environment_canary_leak_fails_the_route(self) -> None:
        run = self.assert_failed("env_leak", "privacy_boundary_violation")
        self.assertEqual(run.audit["privacy_observation_status"], "failed")
        self.assertGreater(
            run.audit["privacy_observation"]["environment_sensitive_hits"], 0
        )

    def test_temporary_and_durable_permissions_are_private(self) -> None:
        run = self.run_mode("valid")
        self.assertEqual(run.audit["temporary_permissions_status"], "verified")
        for path in (run.run_directory, *run.run_directory.rglob("*")):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & 0o077, 0, path)

    def test_cleanup_is_verified_after_success_and_failure(self) -> None:
        success = self.run_mode("valid")
        failure = self.run_mode("runtime_failure")
        self.assertEqual(success.audit["cleanup_status"], "verified")
        self.assertEqual(failure.audit["cleanup_status"], "verified")

    def test_success_and_failure_audits_are_readback_valid(self) -> None:
        for mode in ("valid", "runtime_failure"):
            with self.subTest(mode=mode):
                run = self.run_mode(mode)
                readback = json.loads(run.audit_path.read_text(encoding="utf-8"))
                gate._validate_audit(readback, gate.sensitive_values(self.package))
                self.assertEqual(readback["audit_readback_status"], "verified")

    def test_audit_never_contains_body_identifiers_path_or_credential(self) -> None:
        for mode in ("valid", "runtime_failure", "argv_leak", "env_leak"):
            with self.subTest(mode=mode):
                run = self.run_mode(mode)
                serialized = run.audit_path.read_text(encoding="utf-8")
                for value in gate.sensitive_values(self.package):
                    self.assertNotIn(value, serialized)

    def test_gate_never_publishes_processing_package_or_information(self) -> None:
        for mode in ("valid", "runtime_failure", "invalid_json"):
            with self.subTest(mode=mode):
                run = self.run_mode(mode)
                self.assertFalse(run.audit["package_published"])
                self.assertFalse(run.audit["information_ingested"])
                names = {path.name for path in run.run_directory.iterdir()}
                allowed = {"processing-run-audit.json"}
                if mode == "valid":
                    allowed.add("validated-result.json")
                self.assertEqual(names, allowed)

    def test_committed_schemas_are_strict_json_objects(self) -> None:
        for name in (
            "external-agent-result.schema.json",
            "processing-run-audit.schema.json",
        ):
            schema = json.loads((EXPERIMENT / "schemas" / name).read_text(encoding="utf-8"))
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema["additionalProperties"])

    def test_noncommitted_input_is_rejected_before_external_execution(self) -> None:
        modified = json.loads(json.dumps(self.package, ensure_ascii=False))
        modified["units"][0]["content"] = "Not the committed synthetic fixture."
        with self.assertRaisesRegex(ValueError, "only the committed public synthetic"):
            gate.execute_handoff(
                modified,
                provider_route="synthetic-fake-external-agent",
                provider_version="test-v1",
                invocation_builder=gate.synthetic_invocation_builder("valid"),
                output_root=self.output_root,
                timeout_seconds=3,
            )
        self.assertFalse(self.output_root.exists())


if __name__ == "__main__":
    unittest.main()
