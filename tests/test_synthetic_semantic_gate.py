from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from archeos.representation_information import (
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisBatch,
    RepresentationAnalysisUnit,
)
from archeos.synthetic_semantic_gate import (
    SyntheticSemanticGateError,
    _receipt_fingerprint,
    build_synthetic_semantic_gate_expected_authority,
    execute_synthetic_semantic_gate,
    read_synthetic_semantic_gate_receipt,
)
from tests.test_semantic_handoff import FakeRunner

_BODY = "Private synthetic business body must never persist."
_UNIT_IDS = ("unit_" + "a" * 64, "unit_" + "b" * 64)


class SyntheticSemanticGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def batch(self) -> RepresentationAnalysisBatch:
        return RepresentationAnalysisBatch(
            tuple(
                RepresentationAnalysisUnit(
                    unit_id=unit_id,
                    representation_id="repr_" + "c" * 64,
                    source_id="src_" + "d" * 32,
                    source_content_hash="sha256:" + "e" * 64,
                    representation_kind="markdown_blocks",
                    kind="block",
                    content=_BODY,
                    structured_value=None,
                    locator={"line": index},
                    context="Private synthetic context.",
                    artifact_id="artifact_" + "f" * 64,
                    artifact_locator="artifacts/private-synthetic.json",
                    analysis_eligible=True,
                )
                for index, unit_id in enumerate(_UNIT_IDS, start=1)
            )
        )

    def provider(
        self,
        mode: str,
        *,
        diagnostic_root: Path | None = None,
    ) -> CodexCliRepresentationAnalysisProvider:
        return CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-technical-v1",
            runner=FakeRunner(mode),
            diagnostic_root=diagnostic_root or self.root / f"diagnostics-{mode}",
        )

    def execute(self, mode: str, **kwargs):
        provider = self.provider(mode)
        run = self.execute_provider(provider, f"receipt-{mode}", **kwargs)
        return provider, run

    def execute_provider(self, provider, receipt_name: str, **kwargs):
        batch = self.batch()
        expected_authority = build_synthetic_semantic_gate_expected_authority(
            batch, provider
        )
        run = execute_synthetic_semantic_gate(
            batch,
            provider,
            expected_authority=expected_authority,
            receipt_root=self.root / receipt_name,
            **kwargs,
        )
        return run

    def read_run(self, run, *, path: Path | None = None):
        return read_synthetic_semantic_gate_receipt(
            path or run.receipt_path,
            expected_authority=run.expected_authority,
            expected_receipt_fingerprint=run.expected_receipt_fingerprint,
        )

    def assert_private_content_free(self, run) -> None:
        text = run.receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(_BODY, text)
        self.assertNotIn("Private synthetic context", text)
        self.assertNotIn("private-synthetic", text)
        for unit_id in _UNIT_IDS:
            self.assertNotIn(unit_id, text)
        self.assertNotIn("repr_", text)
        self.assertNotIn("src_", text)
        self.assertNotIn("artifact_", text)
        self.assertNotIn("input_fingerprint", text)
        self.assertNotIn("result_fingerprint", text)
        self.assertNotIn(run.expected_authority.challenge, text)
        self.assertNotIn(run.expected_authority.input_fingerprint, text)
        self.assertEqual(stat.S_IMODE(run.receipt_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(run.receipt_path.stat().st_mode), 0o600)

    def assert_rehashed_attack_rejected(
        self,
        run,
        changes: dict[str, object],
    ) -> None:
        payload = dict(run.receipt)
        payload.update(changes)
        payload["receipt_fingerprint"] = _receipt_fingerprint(payload)
        run.receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(run.receipt_path, 0o600)
        with self.assertRaises(SyntheticSemanticGateError):
            self.read_run(run)

    def test_strict_success_with_grouping_is_pass_and_private(self) -> None:
        provider, run = self.execute("shared_candidate")

        self.assertEqual(provider.provider_start_count, 1)
        self.assertEqual(run.receipt["technical_gate_status"], "passed")
        self.assertEqual(run.receipt["strict_validation_status"], "passed")
        self.assertTrue(run.receipt["grouping_observed"])
        self.assertEqual(run.receipt["raw_record_count"], 2)
        self.assertEqual(run.receipt["projected_record_count"], 1)
        self.assertFalse(run.receipt["package_published"])
        self.assertFalse(run.receipt["atomic_information_written"])
        self.assertEqual(run.receipt["diagnostics_privacy_status"], "passed")
        self.assertEqual(run.receipt, run.anonymous_projection)
        self.assert_private_content_free(run)

    def test_strict_success_without_grouping_remains_pass(self) -> None:
        _provider, run = self.execute("record_body_drift")

        self.assertEqual(run.receipt["technical_gate_status"], "passed")
        self.assertEqual(run.receipt["strict_validation_status"], "passed")
        self.assertFalse(run.receipt["grouping_observed"])
        self.assertEqual(run.receipt["raw_record_count"], 2)
        self.assertEqual(run.receipt["projected_record_count"], 2)

    def test_runtime_nonzero_is_classified_with_stream_metadata(self) -> None:
        _provider, run = self.execute("nonzero")

        receipt = run.receipt
        self.assertEqual(receipt["provider_call_counted"], 1)
        self.assertEqual(receipt["provider_execution_status"], "failed")
        self.assertEqual(receipt["provider_failure_category"], "runtime_nonzero_exit")
        self.assertEqual(receipt["strict_validation_status"], "failed")
        self.assertRegex(receipt["stdout_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(receipt["stderr_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(receipt["harness_status"], "completed")
        self.assertEqual(receipt["technical_gate_status"], "failed")
        self.assert_private_content_free(run)

    def test_transport_failure_is_classified_without_stream_body(self) -> None:
        class TransportProcess:
            pid = 987654
            returncode = 1

            def communicate(self, *, input=None, timeout=None):
                del input, timeout
                return _BODY, "Codex provider transport connection reset"

        provider = self.provider("valid")
        provider.runner = lambda *_args, **_kwargs: TransportProcess()
        run = self.execute_provider(provider, "receipt-transport")

        self.assertEqual(run.receipt["provider_failure_category"], "runtime_nonzero_exit")
        self.assertEqual(run.receipt["provider_error_category"], "network_or_transport")
        self.assertNotIn(_BODY, run.receipt_path.read_text(encoding="utf-8"))

    def test_timeout_is_classified_and_cleanup_is_readable(self) -> None:
        _provider, run = self.execute("timeout")

        self.assertEqual(run.receipt["provider_failure_category"], "timeout")
        self.assertEqual(run.receipt["provider_call_counted"], 1)
        self.assertEqual(run.receipt["process_cleanup_status"], "verified")
        self.assertEqual(run.receipt["technical_gate_status"], "failed")

    def test_contract_failure_has_allowlisted_detail_and_counts(self) -> None:
        _provider, run = self.execute("accounting_missing_anchor")

        receipt = run.receipt
        self.assertEqual(receipt["provider_failure_category"], "result_contract_failure")
        self.assertEqual(receipt["contract_failure_detail"], "anchor_coverage")
        self.assertEqual(receipt["contract_failure_stage"], "coverage")
        self.assertEqual(receipt["eligible_units"], 2)
        self.assertLess(receipt["covered_units"], receipt["eligible_units"])
        self.assertEqual(receipt["result_readback_status"], "verified")

    def test_provider_and_strict_failure_projection_matrix_is_legal(self) -> None:
        expected = {
            "no_result": ("no_result", "not_applicable"),
            "invalid_json": ("invalid_json", "not_applicable"),
            "wrong_binding": ("result_binding_failure", "failed"),
            "candidate_shape": ("result_contract_failure", "verified"),
        }
        for mode, (failure, input_binding) in expected.items():
            with self.subTest(mode=mode):
                _provider, run = self.execute(mode)
                self.assertEqual(run.receipt["provider_failure_category"], failure)
                self.assertEqual(
                    run.receipt["input_binding_status"], input_binding
                )
                self.assertEqual(run.receipt["technical_gate_status"], "failed")
                self.assertEqual(self.read_run(run), run.receipt)

    def test_runtime_execution_failure_is_a_legal_started_state(self) -> None:
        class RuntimeFailureProcess:
            pid = 987655
            returncode = None

            def communicate(self, *, input=None, timeout=None):
                del input, timeout
                raise RuntimeError(_BODY)

        provider = self.provider("valid")
        provider.runner = lambda *_args, **_kwargs: RuntimeFailureProcess()
        run = self.execute_provider(provider, "receipt-runtime-failure")

        self.assertEqual(
            run.receipt["provider_failure_category"],
            "runtime_execution_failure",
        )
        self.assertEqual(run.receipt["provider_call_counted"], 1)
        self.assertEqual(run.receipt["technical_gate_status"], "failed")
        self.assertEqual(self.read_run(run), run.receipt)

    def test_post_success_assertion_failure_is_recorded_after_strict_pass(self) -> None:
        def fail_assertion(_result) -> None:
            raise AssertionError(_BODY)

        _provider, run = self.execute(
            "shared_candidate", post_success_assertion=fail_assertion
        )

        self.assertEqual(run.receipt["strict_validation_status"], "passed")
        self.assertEqual(run.receipt["harness_status"], "failed")
        self.assertEqual(
            run.receipt["harness_failure_category"],
            "post_success_assertion_failure",
        )
        self.assertEqual(run.receipt["technical_gate_status"], "failed")
        self.assert_private_content_free(run)

    def test_post_success_serialization_failure_is_classified(self) -> None:
        def fail_serialization(_result):
            raise TypeError(_BODY)

        _provider, run = self.execute(
            "shared_candidate", post_success_serializer=fail_serialization
        )

        self.assertEqual(run.receipt["strict_validation_status"], "passed")
        self.assertEqual(
            run.receipt["harness_failure_category"],
            "post_success_serialization_failure",
        )
        self.assertEqual(run.receipt["technical_gate_status"], "failed")

    def test_diagnostic_write_failure_still_has_classifiable_receipt(self) -> None:
        with patch(
            "archeos.representation_information._private_diagnostic_write",
            side_effect=OSError("synthetic diagnostic write failure"),
        ):
            _provider, run = self.execute("nonzero")

        self.assertEqual(run.receipt["diagnostic_persistence_status"], "failed")
        self.assertEqual(
            run.receipt["harness_failure_category"],
            "diagnostics_persistence_failure",
        )
        self.assertEqual(run.receipt["technical_gate_status"], "failed")

    def test_cleanup_failure_is_explicit_and_fail_closed(self) -> None:
        with patch(
            "archeos.representation_information.os.killpg",
            side_effect=PermissionError,
        ):
            _provider, run = self.execute("nonzero")

        self.assertEqual(run.receipt["provider_failure_category"], "process_cleanup_failure")
        self.assertEqual(run.receipt["process_cleanup_status"], "failed")
        self.assertEqual(run.receipt["technical_gate_status"], "failed")

    def test_possible_started_call_without_record_is_outcome_unknown_and_counted(self) -> None:
        provider = self.provider("valid")

        def lose_execution_record(_batch):
            provider.provider_start_count += 1
            raise RuntimeError(_BODY)

        provider.analyze = lose_execution_record  # type: ignore[method-assign]
        run = self.execute_provider(provider, "receipt-unknown")

        self.assertTrue(run.receipt["provider_call_started"])
        self.assertEqual(run.receipt["provider_call_counted"], 1)
        self.assertTrue(run.receipt["provider_outcome_unknown"])
        self.assertEqual(run.receipt["provider_execution_status"], "unknown")
        self.assertEqual(run.receipt["technical_gate_status"], "unknown")
        self.assertIsNone(run.receipt["stdout_bytes"])
        self.assert_private_content_free(run)

    def test_proven_pre_popen_failure_counts_zero(self) -> None:
        unsafe = self.root / "unsafe-diagnostics"
        unsafe.mkdir(mode=0o700)
        os.chmod(unsafe, 0o755)
        provider = self.provider("valid", diagnostic_root=unsafe)
        run = self.execute_provider(provider, "receipt-pre-provider")

        self.assertFalse(run.receipt["provider_call_started"])
        self.assertEqual(run.receipt["provider_call_counted"], 0)
        self.assertFalse(run.receipt["provider_outcome_unknown"])
        self.assertEqual(run.receipt["provider_execution_status"], "not_started")
        self.assertEqual(run.receipt["harness_status"], "failed")
        self.assertEqual(
            run.receipt["harness_failure_category"], "pre_provider_failure"
        )
        self.assertEqual(run.receipt["process_cleanup_status"], "not_started")

    def test_rehashed_semantic_attacks_cannot_bypass_exact_state_machine(self) -> None:
        _provider, grouped = self.execute("shared_candidate")
        attacks = (
            {
                "provider_call_started": False,
                "provider_call_counted": 0,
            },
            {
                "result_file_present": False,
                "result_size_bytes": 0,
                "result_readback_status": "not_applicable",
            },
            {"result_size_bytes": 0},
            {"eligible_units": 3},
            {"covered_units": 0, "missing_anchor_count": 0},
            {"missing_anchor_count": 1},
            {"accounting_item_count": 1},
            {"raw_record_count": 1, "grouping_observed": False},
            {"projected_record_count": 3, "grouping_observed": False},
            {"protocol_version": None},
            {"provider_route": None},
            {"provider_version": None},
            {"input_binding_status": "not_applicable"},
            {"model": None},
            {"fallback_policy": "retry"},
            {"grouping_observed": False},
            {"technical_gate_status": "failed"},
            {
                "provider_failure_category": "runtime_nonzero_exit",
                "strict_validation_status": "failed",
            },
            {"diagnostic_persistence_status": "failed"},
        )
        for changes in attacks:
            with self.subTest(changes=changes):
                self.assert_rehashed_attack_rejected(grouped, changes)

        _provider, ungrouped = self.execute("record_body_drift")
        self.assert_rehashed_attack_rejected(
            ungrouped,
            {"grouping_observed": True},
        )

    def test_rehashed_failure_state_attacks_are_rejected(self) -> None:
        _provider, failed = self.execute("nonzero")
        attacks = (
            {"technical_gate_status": "passed"},
            {"strict_validation_status": "passed"},
            {"provider_failure_category": None},
            {"process_cleanup_status": "failed"},
            {
                "harness_status": "failed",
                "harness_failure_category": "post_success_assertion_failure",
            },
        )
        for changes in attacks:
            with self.subTest(changes=changes):
                self.assert_rehashed_attack_rejected(failed, changes)

        _provider, contract = self.execute("accounting_missing_anchor")
        self.assert_rehashed_attack_rejected(
            contract,
            {"contract_failure_stage": "candidate"},
        )
        contract_count_attacks = (
            {"raw_record_count": 2, "grouping_observed": True},
            {"accounting_item_count": 0},
            {"projected_record_count": 2},
            {"grouping_collision_count": 2},
        )
        for changes in contract_count_attacks:
            with self.subTest(changes=changes):
                self.assert_rehashed_attack_rejected(contract, changes)

    def test_execution_record_binding_drift_becomes_outcome_unknown(self) -> None:
        for index, (field, value) in enumerate((
            ("protocol_version", "external-agent-semantic-handoff/3.3"),
            ("provider_route", "synthetic-route"),
            ("provider_version", "synthetic-technical-v2"),
            ("model", "gpt-5.6-sol"),
            ("reasoning_effort", "high"),
            ("fallback_policy", "retry"),
            ("input_fingerprint", "sha256:" + "0" * 64),
            ("anchor_unit_ids", tuple(reversed(_UNIT_IDS))),
            ("eligible_units", 1),
            ("deadline_ms", 1),
            ("diagnostic_schema_version", "external-agent-diagnostics/2.0"),
            ("covered_units", 1),
            ("missing_anchor_count", 1),
            ("raw_record_count", 1),
            ("raw_record_count", "invalid"),
            ("stdout_bytes", 1),
            ("stderr_bytes", 1),
            ("result_size_bytes", 999999),
        )):
            with self.subTest(field=field):
                provider = self.provider("shared_candidate")
                analyze = provider.analyze

                def drift_record(
                    batch,
                    *,
                    _field=field,
                    _value=value,
                    _analyze=analyze,
                    _provider=provider,
                ):
                    result = _analyze(batch)
                    _provider.execution_records[-1] = replace(
                        _provider.execution_records[-1], **{_field: _value}
                    )
                    return result

                provider.analyze = drift_record  # type: ignore[method-assign]
                run = self.execute_provider(
                    provider, f"receipt-binding-{index}-{field}"
                )

                self.assertEqual(provider.provider_start_count, 1)
                self.assertEqual(run.receipt["provider_call_counted"], 1)
                self.assertTrue(run.receipt["provider_outcome_unknown"])
                self.assertEqual(run.receipt["technical_gate_status"], "unknown")
                self.assertTrue(run.receipt_path.is_file())
                self.assertEqual(self.read_run(run), run.receipt)

    def test_observation_drift_becomes_outcome_unknown(self) -> None:
        for index, changes in enumerate(
            (
                {"result_readback_status": "not_applicable"},
                {"stdout_sha256": "sha256:" + "1" * 64},
                {"stdout_bytes": 1},
                {"stderr_bytes": 1},
                {"result_size_bytes": 999999},
                {"result_file_present": False},
                {"exit_code": 9},
                {"termination_signal": 9},
                {"provider_error_category": "unknown"},
                {"process_cleanup_status": "failed"},
            )
        ):
            with self.subTest(changes=changes):
                provider = self.provider("shared_candidate")
                analyze = provider.analyze

                def drift_observation(
                    batch,
                    *,
                    _analyze=analyze,
                    _provider=provider,
                    _changes=changes,
                ):
                    result = _analyze(batch)
                    _provider.technical_observations[-1] = replace(
                        _provider.technical_observations[-1], **_changes
                    )
                    return result

                provider.analyze = drift_observation  # type: ignore[method-assign]
                run = self.execute_provider(
                    provider, f"receipt-observation-drift-{index}"
                )

                self.assertEqual(provider.provider_start_count, 1)
                self.assertEqual(run.receipt["provider_call_counted"], 1)
                self.assertTrue(run.receipt["provider_outcome_unknown"])
                self.assertEqual(run.receipt["technical_gate_status"], "unknown")
                self.assertTrue(run.receipt_path.is_file())
                self.assertEqual(self.read_run(run), run.receipt)

    def test_coherent_record_and_observation_drift_is_outcome_unknown(self) -> None:
        attacks = (
            ({"covered_units": 1}, {"covered_units": 1}),
            ({"raw_record_count": 1}, {"raw_record_count": 1}),
            ({"stdout_bytes": 1}, {"stdout_bytes": 1}),
            ({"stderr_bytes": 1}, {"stderr_bytes": 1}),
            ({"result_size_bytes": 999999}, {"result_size_bytes": 999999}),
            ({"result_file_present": False}, {"result_file_present": False}),
            ({"exit_code": 9}, {"exit_code": 9}),
            ({"termination_signal": 9}, {"termination_signal": 9}),
            (
                {"provider_error_category": "unknown"},
                {"provider_error_category": "unknown"},
            ),
            (
                {"process_cleanup_status": "failed"},
                {"process_cleanup_status": "failed"},
            ),
        )
        for index, (record_changes, observation_changes) in enumerate(attacks):
            with self.subTest(record_changes=record_changes):
                provider = self.provider("shared_candidate")
                analyze = provider.analyze

                def drift_both(
                    batch,
                    *,
                    _analyze=analyze,
                    _provider=provider,
                    _record_changes=record_changes,
                    _observation_changes=observation_changes,
                ):
                    result = _analyze(batch)
                    _provider.execution_records[-1] = replace(
                        _provider.execution_records[-1], **_record_changes
                    )
                    _provider.technical_observations[-1] = replace(
                        _provider.technical_observations[-1],
                        **_observation_changes,
                    )
                    return result

                provider.analyze = drift_both  # type: ignore[method-assign]
                run = self.execute_provider(
                    provider, f"receipt-coherent-projection-drift-{index}"
                )

                self.assertEqual(provider.provider_start_count, 1)
                self.assertEqual(run.receipt["provider_call_counted"], 1)
                self.assertTrue(run.receipt["provider_outcome_unknown"])
                self.assertEqual(run.receipt["technical_gate_status"], "unknown")
                self.assertIsNone(run.receipt["stdout_bytes"])
                self.assertIsNone(run.receipt["result_size_bytes"])
                self.assertEqual(self.read_run(run), run.receipt)

    def test_strict_success_accepts_exact_nonzero_stream_and_result_projection(
        self,
    ) -> None:
        base_runner = FakeRunner("shared_candidate")

        class NonzeroStreamProcess:
            def __init__(self, process):
                self._process = process
                self.pid = process.pid

            @property
            def returncode(self):
                return self._process.returncode

            def communicate(self, *, input=None, timeout=None):
                self._process.communicate(input=input, timeout=timeout)
                return "safe synthetic stdout", "safe synthetic stderr"

        provider = self.provider("shared_candidate")
        provider.runner = lambda *args, **kwargs: NonzeroStreamProcess(
            base_runner(*args, **kwargs)
        )
        run = self.execute_provider(provider, "receipt-nonzero-success")

        self.assertEqual(provider.provider_start_count, 1)
        self.assertEqual(run.receipt["technical_gate_status"], "passed")
        self.assertGreater(run.receipt["stdout_bytes"], 0)
        self.assertGreater(run.receipt["stderr_bytes"], 0)
        self.assertGreater(run.receipt["result_size_bytes"], 0)
        self.assertEqual(run.receipt["result_readback_status"], "verified")
        self.assertEqual(self.read_run(run), run.receipt)
        self.assert_private_content_free(run)

    def test_reader_requires_original_external_authority_and_fingerprint(self) -> None:
        _provider, run = self.execute("shared_candidate")

        with self.assertRaisesRegex(SyntheticSemanticGateError, "external authority"):
            read_synthetic_semantic_gate_receipt(run.receipt_path)
        with self.assertRaises(SyntheticSemanticGateError):
            read_synthetic_semantic_gate_receipt(
                run.receipt_path,
                expected_authority=run.expected_authority,
                expected_receipt_fingerprint="sha256:" + "0" * 64,
            )
        for changes in (
            {"challenge": "0" * 64},
            {"technical_envelope_id": "synthetic_gate_" + "0" * 32},
            {"provider_version": "synthetic-technical-v2"},
            {"eligible_units": 3},
            {"ordered_anchor_unit_ids": tuple(reversed(_UNIT_IDS))},
            {"input_fingerprint": "sha256:" + "0" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                SyntheticSemanticGateError
            ):
                read_synthetic_semantic_gate_receipt(
                    run.receipt_path,
                    expected_authority=replace(run.expected_authority, **changes),
                    expected_receipt_fingerprint=run.expected_receipt_fingerprint,
                )

    def test_execute_rejects_drifted_preflight_authority_before_provider(self) -> None:
        batch = self.batch()
        for index, changes in enumerate(
            (
                {"model": "gpt-5.6-sol"},
                {"deadline_ms": 1},
                {"eligible_units": 3},
                {"ordered_anchor_unit_ids": tuple(reversed(_UNIT_IDS))},
                {"input_fingerprint": "sha256:" + "0" * 64},
            )
        ):
            with self.subTest(changes=changes):
                provider = self.provider("shared_candidate")
                expected = build_synthetic_semantic_gate_expected_authority(
                    batch, provider
                )
                receipt_root = self.root / f"receipt-wrong-authority-{index}"
                with self.assertRaisesRegex(
                    SyntheticSemanticGateError, "expected authority"
                ):
                    execute_synthetic_semantic_gate(
                        batch,
                        provider,
                        expected_authority=replace(expected, **changes),
                        receipt_root=receipt_root,
                    )
                self.assertEqual(provider.provider_start_count, 0)
                self.assertFalse(receipt_root.exists())

    def test_coherent_rehash_cannot_replace_external_receipt_authority(self) -> None:
        _provider, run = self.execute("shared_candidate")
        coherent_attacks = (
            {"provider_version": "synthetic-technical-v2"},
            {"model": "gpt-5.6-sol"},
            {"reasoning_effort": "high"},
            {
                "stdout_bytes": 1,
                "stdout_sha256": "sha256:" + hashlib.sha256(b"x").hexdigest(),
            },
            {"result_size_bytes": run.receipt["result_size_bytes"] + 1},
            {
                "eligible_units": 3,
                "covered_units": 3,
                "raw_record_count": 3,
            },
        )
        for changes in coherent_attacks:
            with self.subTest(changes=changes):
                self.assert_rehashed_attack_rejected(run, changes)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork")
    def test_cross_process_coherent_rehash_uses_original_external_authority(self) -> None:
        for index, changes in enumerate(
            (
                {"provider_version": "synthetic-technical-v2"},
                {"result_size_bytes": 1},
                {
                    "eligible_units": 3,
                    "covered_units": 3,
                    "raw_record_count": 3,
                },
            )
        ):
            with self.subTest(changes=changes):
                provider = self.provider("shared_candidate")
                run = self.execute_provider(provider, f"receipt-cross-{index}")
                payload = dict(run.receipt)
                payload.update(changes)
                payload["receipt_fingerprint"] = _receipt_fingerprint(payload)
                run.receipt_path.write_text(json.dumps(payload), encoding="utf-8")
                os.chmod(run.receipt_path, 0o600)
                pid = os.fork()
                if pid == 0:
                    try:
                        self.read_run(run)
                    except SyntheticSemanticGateError:
                        os._exit(0)
                    os._exit(1)
                _waited, status = os.waitpid(pid, 0)
                self.assertTrue(os.WIFEXITED(status), index)
                self.assertEqual(os.WEXITSTATUS(status), 0, index)

    def test_unsafe_receipt_root_fails_before_provider(self) -> None:
        receipt_root = self.root / "unsafe-receipt"
        receipt_root.mkdir(mode=0o700)
        os.chmod(receipt_root, 0o755)
        provider = self.provider("valid")
        batch = self.batch()
        expected_authority = build_synthetic_semantic_gate_expected_authority(
            batch, provider
        )

        with self.assertRaisesRegex(SyntheticSemanticGateError, "root 不安全"):
            execute_synthetic_semantic_gate(
                batch,
                provider,
                expected_authority=expected_authority,
                receipt_root=receipt_root,
            )
        self.assertEqual(provider.provider_start_count, 0)
        self.assertEqual(provider.execution_records, [])

    def test_receipt_missing_extra_keys_and_tamper_fail_closed(self) -> None:
        _provider, run = self.execute("shared_candidate")
        original = run.receipt_path.read_bytes()
        attacks = (
            lambda value: value.pop("covered_units"),
            lambda value: value.__setitem__("extra", True),
            lambda value: value.__setitem__("technical_gate_status", "passed")
            or value.__setitem__("strict_validation_status", "failed"),
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                payload = json.loads(original)
                attack(payload)
                run.receipt_path.write_text(json.dumps(payload), encoding="utf-8")
                os.chmod(run.receipt_path, 0o600)
                with self.assertRaises(SyntheticSemanticGateError):
                    self.read_run(run)
        duplicate = original.decode().replace(
            '"covered_units": 2',
            '"covered_units": 2, "covered_units": 2',
            1,
        )
        run.receipt_path.write_text(duplicate, encoding="utf-8")
        os.chmod(run.receipt_path, 0o600)
        with self.assertRaises(SyntheticSemanticGateError):
            self.read_run(run)
        run.receipt_path.unlink()
        with self.assertRaises(SyntheticSemanticGateError):
            self.read_run(run)

    def test_receipt_mode_symlink_and_extra_inventory_fail_closed(self) -> None:
        _provider, run = self.execute("shared_candidate")
        os.chmod(run.receipt_path, 0o644)
        with self.assertRaises(SyntheticSemanticGateError):
            self.read_run(run)
        os.chmod(run.receipt_path, 0o600)
        extra = run.receipt_path.parent / "extra.json"
        extra.write_text("{}", encoding="utf-8")
        os.chmod(extra, 0o600)
        with self.assertRaises(SyntheticSemanticGateError):
            self.read_run(run)
        extra.unlink()
        linked_root = self.root / "linked-receipt-root"
        linked_root.symlink_to(run.receipt_path.parent, target_is_directory=True)
        with self.assertRaises(SyntheticSemanticGateError):
            self.read_run(run, path=linked_root / run.receipt_path.name)

    def test_readback_drift_fails_closed_after_receipt_write(self) -> None:
        provider = self.provider("shared_candidate")

        def drifted_reader(path: Path, **expected) -> dict[str, object]:
            payload = read_synthetic_semantic_gate_receipt(path, **expected)
            payload["covered_units"] = 0
            return payload

        with (
            patch(
                "archeos.synthetic_semantic_gate."
                "read_synthetic_semantic_gate_receipt",
                side_effect=drifted_reader,
            ),
            self.assertRaisesRegex(SyntheticSemanticGateError, "读回不一致"),
        ):
            self.execute_provider(provider, "receipt-readback-drift")
        self.assertEqual(provider.provider_start_count, 1)

    def test_stream_hashes_match_bytes_but_never_store_stream_body(self) -> None:
        class StreamProcess:
            pid = 876543
            returncode = 2

            def communicate(self, *, input=None, timeout=None):
                del input, timeout
                return _BODY, "safe synthetic stderr"

        provider = self.provider("valid")
        provider.runner = lambda *_args, **_kwargs: StreamProcess()
        run = self.execute_provider(provider, "receipt-stream-hashes")

        self.assertEqual(run.receipt["stdout_bytes"], len(_BODY.encode()))
        self.assertEqual(
            run.receipt["stdout_sha256"],
            "sha256:" + hashlib.sha256(_BODY.encode()).hexdigest(),
        )
        self.assertNotIn(_BODY, run.receipt_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
