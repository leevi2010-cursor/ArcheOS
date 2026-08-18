from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archeos.representation_information import (
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisBatch,
    RepresentationAnalysisUnit,
)
from archeos.synthetic_semantic_gate import (
    SyntheticSemanticGateError,
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
        run = execute_synthetic_semantic_gate(
            self.batch(),
            provider,
            receipt_root=self.root / f"receipt-{mode}",
            **kwargs,
        )
        return provider, run

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
        self.assertEqual(stat.S_IMODE(run.receipt_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(run.receipt_path.stat().st_mode), 0o600)

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
        run = execute_synthetic_semantic_gate(
            self.batch(),
            provider,
            receipt_root=self.root / "receipt-transport",
        )

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
        run = execute_synthetic_semantic_gate(
            self.batch(),
            provider,
            receipt_root=self.root / "receipt-unknown",
        )

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
        run = execute_synthetic_semantic_gate(
            self.batch(),
            provider,
            receipt_root=self.root / "receipt-pre-provider",
        )

        self.assertFalse(run.receipt["provider_call_started"])
        self.assertEqual(run.receipt["provider_call_counted"], 0)
        self.assertFalse(run.receipt["provider_outcome_unknown"])
        self.assertEqual(run.receipt["process_cleanup_status"], "not_started")

    def test_unsafe_receipt_root_fails_before_provider(self) -> None:
        receipt_root = self.root / "unsafe-receipt"
        receipt_root.mkdir(mode=0o700)
        os.chmod(receipt_root, 0o755)
        provider = self.provider("valid")

        with self.assertRaisesRegex(SyntheticSemanticGateError, "root 不安全"):
            execute_synthetic_semantic_gate(
                self.batch(), provider, receipt_root=receipt_root
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
                    read_synthetic_semantic_gate_receipt(run.receipt_path)
        duplicate = original.decode().replace(
            '"covered_units": 2',
            '"covered_units": 2, "covered_units": 2',
            1,
        )
        run.receipt_path.write_text(duplicate, encoding="utf-8")
        os.chmod(run.receipt_path, 0o600)
        with self.assertRaises(SyntheticSemanticGateError):
            read_synthetic_semantic_gate_receipt(run.receipt_path)
        run.receipt_path.unlink()
        with self.assertRaises(SyntheticSemanticGateError):
            read_synthetic_semantic_gate_receipt(run.receipt_path)

    def test_receipt_mode_symlink_and_extra_inventory_fail_closed(self) -> None:
        _provider, run = self.execute("shared_candidate")
        os.chmod(run.receipt_path, 0o644)
        with self.assertRaises(SyntheticSemanticGateError):
            read_synthetic_semantic_gate_receipt(run.receipt_path)
        os.chmod(run.receipt_path, 0o600)
        extra = run.receipt_path.parent / "extra.json"
        extra.write_text("{}", encoding="utf-8")
        os.chmod(extra, 0o600)
        with self.assertRaises(SyntheticSemanticGateError):
            read_synthetic_semantic_gate_receipt(run.receipt_path)
        extra.unlink()
        linked_root = self.root / "linked-receipt-root"
        linked_root.symlink_to(run.receipt_path.parent, target_is_directory=True)
        with self.assertRaises(SyntheticSemanticGateError):
            read_synthetic_semantic_gate_receipt(linked_root / run.receipt_path.name)

    def test_readback_drift_fails_closed_after_receipt_write(self) -> None:
        provider = self.provider("shared_candidate")

        def drifted_reader(path: Path) -> dict[str, object]:
            payload = read_synthetic_semantic_gate_receipt(path)
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
            execute_synthetic_semantic_gate(
                self.batch(),
                provider,
                receipt_root=self.root / "receipt-readback-drift",
            )
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
        run = execute_synthetic_semantic_gate(
            self.batch(),
            provider,
            receipt_root=self.root / "receipt-stream-hashes",
        )

        self.assertEqual(run.receipt["stdout_bytes"], len(_BODY.encode()))
        self.assertEqual(
            run.receipt["stdout_sha256"],
            "sha256:" + hashlib.sha256(_BODY.encode()).hexdigest(),
        )
        self.assertNotIn(_BODY, run.receipt_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
