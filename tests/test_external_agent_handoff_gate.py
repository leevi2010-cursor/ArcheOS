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


class CompleteSyntheticObserver:
    """Test double for artifact success paths; never used by the live CLI."""

    def __init__(self, root_pid: int, _forbidden_values) -> None:
        self.observed_pids = {root_pid}

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def summary(self) -> dict[str, object]:
        return {
            "backend": "synthetic_complete_test_double",
            "root_observed": True,
            "snapshots": 1,
            "observed_processes": 1,
            "metadata_sensitive_hits": 0,
            "metadata_read_failures": 0,
            "observation_complete": True,
        }


class ExternalAgentHandoffGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "audit-artifacts"
        self.package = gate.load_synthetic_package()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mode(
        self,
        mode: str,
        *,
        timeout: float = 3.0,
        complete_observer: bool = True,
        **kwargs,
    ):
        if complete_observer:
            kwargs["observer_factory"] = CompleteSyntheticObserver
        return gate.execute_handoff(
            self.package,
            provider_route="synthetic-fake-external-agent",
            provider_version="test-v1",
            invocation_builder=gate.synthetic_invocation_builder(mode),
            output_root=self.output_root,
            timeout_seconds=timeout,
            **kwargs,
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

    def test_valid_result_contract_and_atomic_readback_with_complete_test_double(self) -> None:
        run = self.run_mode("valid")

        self.assertEqual(run.audit["execution_status"], "succeeded")
        self.assertEqual(run.audit["privacy_observation_status"], "passed")
        self.assertEqual(run.audit["eligible_units"], 2)
        self.assertEqual(run.audit["covered_units"], 2)
        self.assertEqual(run.audit["unaccounted_units"], 0)
        self.assertEqual(run.audit["result_readback_status"], "verified")
        assert run.result_path is not None
        result = json.loads(run.result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["input_fingerprint"], gate.fingerprint(self.package))
        self.assertEqual(run.audit["result_fingerprint"], gate.fingerprint(result))

    def test_sampling_zero_hits_are_unavailable_never_passed(self) -> None:
        run = self.run_mode("valid", complete_observer=False)

        self.assertEqual(run.audit["execution_status"], "failed")
        self.assertEqual(run.audit["privacy_observation_status"], "unavailable")
        self.assertEqual(run.audit["failure_category"], "privacy_observation_unavailable")
        self.assertFalse(run.audit["privacy_observation"]["observation_complete"])
        self.assertIsNone(run.result_path)

    def test_short_lived_descendant_leak_never_false_passes_ten_runs(self) -> None:
        for attempt in range(10):
            with self.subTest(attempt=attempt):
                run = self.run_mode(
                    "short_lived_argv_leak", complete_observer=False
                )
                self.assertEqual(run.audit["execution_status"], "failed")
                self.assertIn(
                    run.audit["privacy_observation_status"],
                    {"failed", "unavailable"},
                )
                self.assertIsNone(run.result_path)

    def test_descendant_argv_and_environment_leaks_use_combined_metadata_channel(self) -> None:
        for mode in ("argv_leak", "env_leak"):
            with self.subTest(mode=mode):
                run = self.run_mode(mode, complete_observer=False)
                observation = run.audit["privacy_observation"]
                self.assertEqual(run.audit["privacy_observation_status"], "failed")
                self.assertEqual(
                    run.audit["failure_category"], "privacy_boundary_violation"
                )
                self.assertGreater(observation["metadata_sensitive_hits"], 0)
                self.assertNotIn("argv_sensitive_hits", observation)
                self.assertNotIn("environment_sensitive_hits", observation)

    def test_observer_reads_metadata_only_for_selected_tree_pids(self) -> None:
        observer = gate.ProcessTreePrivacyObserver(100, ("canary",))
        observer.backend = "procfs_sampling"
        observer._topology = lambda: {100: 1, 101: 100, 999: 1}
        selected: list[int] = []

        def read_selected(pid: int) -> bytes:
            selected.append(pid)
            return b"safe"

        observer._read_selected_metadata = read_selected
        observer._sample()

        self.assertEqual(set(selected), {100, 101})
        self.assertNotIn(999, selected)

    def test_lingering_descendant_is_terminated_and_verified(self) -> None:
        run = self.run_mode("lingering_child", complete_observer=False)

        cleanup = run.audit["cleanup_observation"]
        self.assertEqual(run.audit["cleanup_status"], "verified")
        self.assertTrue(cleanup["process_group_absent"])
        self.assertTrue(cleanup["observed_processes_absent"])
        self.assertTrue(cleanup["temporary_directory_absent"])
        self.assertGreaterEqual(cleanup["terminated_processes"], 1)
        self.assertGreaterEqual(
            run.audit["privacy_observation"]["observed_processes"], 2
        )
        self.assertTrue(
            all(not gate._pid_exists(pid) for pid in run.observed_process_ids)
        )

    def test_timeout_is_audited_and_fail_closed(self) -> None:
        run = self.assert_failed("timeout", "timeout", timeout=0.05)
        self.assertEqual(run.audit["cleanup_status"], "verified")

    def test_nonzero_runtime_failure_is_audited_and_fail_closed(self) -> None:
        self.assert_failed("runtime_failure", "runtime_failure")

    def test_missing_executable_produces_anonymous_failure_audit(self) -> None:
        def missing_builder(_request, _run_dir, _schema, _result):
            return gate.ExternalAgentInvocation(
                command=("/definitely/missing/issue66-agent",),
                stdin_text="",
                environment_extras={},
            )

        run = gate.execute_handoff(
            self.package,
            provider_route="missing-external-agent",
            provider_version="test-v1",
            invocation_builder=missing_builder,
            output_root=self.output_root,
            timeout_seconds=1,
            observer_factory=CompleteSyntheticObserver,
        )

        self.assertEqual(run.audit["failure_category"], "runtime_start_failure")
        self.assertEqual(run.audit["audit_readback_status"], "verified")
        self.assertEqual(run.audit["cleanup_status"], "verified")
        self.assertIsNone(run.result_path)

    def test_observer_start_failure_is_audited_and_processes_are_cleaned(self) -> None:
        class BrokenObserver:
            def __init__(self, root_pid: int, _forbidden_values) -> None:
                self.observed_pids = {root_pid}

            def start(self) -> None:
                raise OSError("synthetic observer failure")

            def stop(self) -> None:
                pass

            def summary(self) -> dict[str, object]:
                raise AssertionError("summary must not be used")

        run = gate.execute_handoff(
            self.package,
            provider_route="synthetic-provider",
            provider_version="test-v1",
            invocation_builder=gate.synthetic_invocation_builder("valid"),
            output_root=self.output_root,
            timeout_seconds=1,
            observer_factory=BrokenObserver,
        )

        self.assertEqual(
            run.audit["failure_category"], "privacy_observation_unavailable"
        )
        self.assertEqual(run.audit["cleanup_status"], "verified")
        self.assertIsNone(run.result_path)

    def test_missing_and_empty_results_are_distinct_failures(self) -> None:
        self.assert_failed("no_result", "no_result")
        self.assert_failed("empty_result", "empty_result")

    def test_invalid_json_and_valid_json_schema_errors_are_rejected(self) -> None:
        invalid = self.assert_failed("invalid_json", "invalid_json")
        self.assertEqual(invalid.audit["strict_validation_status"], "failed")
        for mode in ("extra_field", "wrong_type"):
            with self.subTest(mode=mode):
                run = self.assert_failed(mode, "result_contract_failure")
                self.assertEqual(run.audit["strict_validation_status"], "failed")

    def test_unknown_duplicate_incomplete_and_wrong_binding_are_rejected(self) -> None:
        for mode, category in (
            ("unknown_ref", "result_contract_failure"),
            ("duplicate_ref", "result_contract_failure"),
            ("incomplete_coverage", "result_contract_failure"),
            ("wrong_fingerprint", "result_binding_failure"),
        ):
            with self.subTest(mode=mode):
                self.assert_failed(mode, category)

    def test_temporary_and_durable_permissions_are_private(self) -> None:
        run = self.run_mode("valid")
        self.assertEqual(run.audit["temporary_permissions_status"], "verified")
        for path in (run.run_directory, *run.run_directory.rglob("*")):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & 0o077, 0, path)

    def test_audit_write_failure_falls_back_without_orphan_result(self) -> None:
        audit_failures = 0

        def fail_first_audit_write(path: Path, payload: object) -> None:
            nonlocal audit_failures
            if path.name == "processing-run-audit.json" and audit_failures == 0:
                audit_failures += 1
                raise OSError("synthetic audit write failure")
            gate._private_write(path, payload)

        run = self.run_mode("valid", artifact_writer=fail_first_audit_write)

        self.assertEqual(run.audit["execution_status"], "failed")
        self.assertEqual(run.audit["failure_category"], "artifact_persistence_failure")
        self.assertIsNone(run.result_path)
        self.assertEqual(
            {path.name for path in run.run_directory.iterdir()},
            {"processing-run-audit.json"},
        )
        self.assertFalse(any(path.name.endswith(".staging") for path in self.output_root.iterdir()))

    def test_audit_readback_status_is_verified_only_after_pending_readback(self) -> None:
        written_statuses: list[str] = []
        read_statuses: list[str] = []

        def recording_writer(path: Path, payload: object) -> None:
            if path.name == "processing-run-audit.json":
                assert isinstance(payload, dict)
                written_statuses.append(str(payload["audit_readback_status"]))
            gate._private_write(path, payload)

        def recording_reader(path: Path) -> dict[str, object]:
            payload = gate._load_json_object(path)
            if path.name == "processing-run-audit.json":
                read_statuses.append(str(payload["audit_readback_status"]))
            return payload

        run = self.run_mode(
            "valid",
            artifact_writer=recording_writer,
            artifact_reader=recording_reader,
        )

        self.assertEqual(written_statuses, ["pending", "verified"])
        self.assertEqual(read_statuses, ["pending", "verified"])
        self.assertEqual(run.audit["audit_readback_status"], "verified")

    def test_provider_metadata_injection_is_normalized_and_fails_before_execution(self) -> None:
        injections = (
            self.package["units"][0]["content"],
            "/private/synthetic/business/path",
            "sk-synthetic-credential-not-real",
        )
        calls = 0

        def forbidden_builder(*_args):
            nonlocal calls
            calls += 1
            raise AssertionError("unsafe provider metadata must fail before execution")

        for field in ("provider_route", "provider_version"):
            for injected in injections:
                with self.subTest(field=field, injected=injected):
                    metadata = {
                        "provider_route": "synthetic-provider",
                        "provider_version": "test-v1",
                    }
                    metadata[field] = str(injected)
                    run = gate.execute_handoff(
                        self.package,
                        invocation_builder=forbidden_builder,
                        output_root=self.output_root,
                        timeout_seconds=1,
                        **metadata,
                    )
                    serialized = run.audit_path.read_text(encoding="utf-8")
                    self.assertEqual(
                        run.audit["failure_category"], "unsafe_provider_metadata"
                    )
                    self.assertNotIn(str(injected), serialized)
        self.assertEqual(calls, 0)

    def test_audit_never_contains_any_unit_body_identifier_path_or_credential(self) -> None:
        for mode in ("valid", "runtime_failure"):
            with self.subTest(mode=mode):
                run = self.run_mode(mode)
                serialized = run.audit_path.read_text(encoding="utf-8")
                for value in gate.sensitive_values(self.package):
                    self.assertNotIn(value, serialized)

    def test_modified_local_fixture_is_rejected_by_hardcoded_fingerprint(self) -> None:
        modified_path = Path(self.temporary.name) / "modified-fixture.json"
        modified = json.loads(json.dumps(self.package, ensure_ascii=False))
        modified["units"][0]["content"] = "Modified local synthetic fixture."
        modified_path.write_text(json.dumps(modified), encoding="utf-8")
        original = gate.FIXTURE_PATH
        gate.FIXTURE_PATH = modified_path
        try:
            with self.assertRaisesRegex(ValueError, "committed fingerprint"):
                gate.load_synthetic_package()
        finally:
            gate.FIXTURE_PATH = original

    def test_noncommitted_package_is_rejected_before_external_execution(self) -> None:
        modified = json.loads(json.dumps(self.package, ensure_ascii=False))
        modified["units"][0]["content"] = "Not the committed synthetic fixture."
        with self.assertRaisesRegex(ValueError, "committed public synthetic"):
            gate.execute_handoff(
                modified,
                provider_route="synthetic-provider",
                provider_version="test-v1",
                invocation_builder=gate.synthetic_invocation_builder("valid"),
                output_root=self.output_root,
                timeout_seconds=3,
            )
        self.assertFalse(self.output_root.exists())

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
        audit_schema = json.loads(
            (EXPERIMENT / "schemas" / "processing-run-audit.schema.json").read_text()
        )
        observation = audit_schema["properties"]["privacy_observation"]["properties"]
        self.assertIn("metadata_sensitive_hits", observation)
        self.assertNotIn("argv_sensitive_hits", observation)
        self.assertNotIn("environment_sensitive_hits", observation)

    def test_missing_cli_version_probe_remains_auditable(self) -> None:
        missing = str(Path(self.temporary.name) / "missing-codex")
        self.assertEqual(gate._runtime_version(missing), "codex-cli 0.0-unavailable")
        run = gate.execute_handoff(
            self.package,
            provider_route="external-agent-codex-cli",
            provider_version=gate._runtime_version(missing),
            invocation_builder=gate.codex_invocation_builder(missing),
            output_root=self.output_root,
            timeout_seconds=3,
        )
        self.assertEqual(run.audit["failure_category"], "runtime_start_failure")
        self.assertEqual(run.audit["audit_readback_status"], "verified")


if __name__ == "__main__":
    unittest.main()
