from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from archeos.representation_information import (
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisBatch,
    RepresentationAnalysisUnit,
    _provider_error_category,
)

_SYNTHETIC_BODY = "Synthetic only."
_SYNTHETIC_UNIT_ID = "unit_" + "a" * 64
_DIAGNOSTIC_V2_METADATA_FIELDS = {
    "accounting_item_count",
    "candidate_anchor_ref_count",
    "candidate_item_count",
    "contract_failure_stage",
    "covered_units",
    "created_at",
    "deadline_ms",
    "diagnostic_schema_version",
    "dual_assignment_count",
    "duplicate_accounting_count",
    "duplicate_anchor_ref_count",
    "elapsed_ms",
    "eligible_units",
    "exit_code",
    "expires_at",
    "failure_category",
    "fallback_policy",
    "finished_at",
    "input_fingerprint",
    "missing_anchor_count",
    "model",
    "process_cleanup_status",
    "protocol_version",
    "provider_error_category",
    "provider_route",
    "provider_version",
    "reasoning_effort",
    "residue_anchor_ref_count",
    "residue_item_count",
    "result_file_present",
    "result_fingerprint",
    "result_size_bytes",
    "started_at",
    "stderr_bytes",
    "stderr_sha256",
    "stdout_bytes",
    "stdout_sha256",
    "termination_signal",
    "timeout_phase",
    "unknown_anchor_ref_count",
}


class _Process:
    pid = 12345

    def __init__(self, mode: str, command: list[str]) -> None:
        self.mode = mode
        self.command = command
        self.returncode: int | None = None
        self.calls = 0

    def communicate(self, *, input=None, timeout=None):
        del input, timeout
        self.calls += 1
        if self.mode == "timeout" and self.calls == 1:
            raise subprocess.TimeoutExpired(
                self.command,
                1,
                output=f"partial stdout {_SYNTHETIC_BODY} ",
                stderr=f"partial stderr {_SYNTHETIC_UNIT_ID} ",
            )
        if self.mode == "timeout":
            self.returncode = -signal.SIGTERM
            return (
                f"drained stdout {_SYNTHETIC_BODY}",
                f"drained stderr {_SYNTHETIC_UNIT_ID}",
            )
        if self.mode == "nonzero":
            self.returncode = 9
            return _SYNTHETIC_BODY, _SYNTHETIC_UNIT_ID
        if self.mode == "transport":
            self.returncode = 1
            return (
                _SYNTHETIC_BODY,
                f"Codex provider transport connection reset {_SYNTHETIC_UNIT_ID}",
            )
        if self.mode == "known_error":
            self.returncode = 1
            return "", "HTTP 429 rate limit"
        if self.mode == "unknown_error":
            self.returncode = 1
            return "", "unrecognized synthetic condition"
        if self.mode == "nonzero_with_result":
            result_path = Path(
                self.command[self.command.index("--output-last-message") + 1]
            )
            result_path.write_text("synthetic partial result", encoding="utf-8")
            self.returncode = 1
            return "", "Codex provider returned HTTP 500"
        raise AssertionError(f"unexpected mode {self.mode}")


class _Runner:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def __call__(self, command, **_kwargs):
        return _Process(self.mode, list(command))


class ExternalAgentFailureDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def batch(self) -> RepresentationAnalysisBatch:
        return RepresentationAnalysisBatch(
            (
                RepresentationAnalysisUnit(
                    unit_id="unit_" + "a" * 64,
                    representation_id="repr_" + "b" * 64,
                    source_id="src_" + "c" * 32,
                    source_content_hash="sha256:" + "d" * 64,
                    representation_kind="markdown_blocks",
                    kind="block",
                    content="Synthetic only.",
                    structured_value=None,
                    locator={"line": 1},
                    context="Synthetic context.",
                    artifact_id="artifact_" + "e" * 64,
                    artifact_locator="artifacts/synthetic.json",
                    analysis_eligible=True,
                ),
            )
        )

    def provider(self, mode: str) -> CodexCliRepresentationAnalysisProvider:
        return CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=_Runner(mode),
            diagnostic_root=self.root / "diagnostics",
        )

    def echoing_runner(self, mode: str):
        from tests.test_semantic_handoff import FakeProcess, FakeRunner

        class EchoingProcess(FakeProcess):
            def communicate(self, *, input=None, timeout=None):
                super().communicate(input=input, timeout=timeout)
                return _SYNTHETIC_BODY, _SYNTHETIC_UNIT_ID

        class EchoingRunner(FakeRunner):
            def __call__(self, command, **_kwargs):
                command = list(command)
                self.schemas.append(
                    json.loads(
                        Path(
                            command[command.index("--output-schema") + 1]
                        ).read_text(encoding="utf-8")
                    )
                )
                return EchoingProcess(command, mode=self.mode, calls=self.calls)

        return EchoingRunner(mode)

    def assert_content_free_bundle(self, bundle: Path) -> dict[str, object]:
        self.assertEqual(
            {path.name for path in bundle.iterdir()},
            {"metadata.json"},
        )
        metadata_path = bundle / "metadata.json"
        metadata_text = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_text)
        self.assertEqual(set(metadata), _DIAGNOSTIC_V2_METADATA_FIELDS)
        self.assertRegex(metadata["stdout_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(metadata["stderr_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(_SYNTHETIC_BODY, metadata_text)
        self.assertNotIn(_SYNTHETIC_UNIT_ID, metadata_text)
        return metadata

    def _run_failure(self, mode: str):
        provider = self.provider(mode)
        with (
            patch(
                "archeos.representation_information.os.killpg",
                side_effect=ProcessLookupError,
            ),
            self.assertRaisesRegex(Exception, "未产生可验证"),
        ):
            provider.analyze(self.batch())
        return provider, provider.execution_records[0]

    def test_success_does_not_create_raw_bundle(self) -> None:
        root = self.root / "diagnostics"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=self.echoing_runner("valid"),
            diagnostic_root=root,
        )
        provider.analyze(self.batch())
        self.assertFalse(root.exists())

    def test_timeout_captures_partial_and_drain_with_cleaned_process(self) -> None:
        provider = self.provider("timeout")

        def terminate_then_absent(_pid: int, requested: int) -> None:
            if requested == signal.SIGTERM:
                return
            if requested == 0:
                raise ProcessLookupError
            raise AssertionError("SIGKILL should not be needed")

        with (
            patch(
                "archeos.representation_information.os.killpg",
                side_effect=terminate_then_absent,
            ),
            self.assertRaisesRegex(Exception, "未产生可验证"),
        ):
            provider.analyze(self.batch())
        record = provider.execution_records[0]
        bundle = provider.diagnostic_root / record.processing_run_id
        self.assertEqual(record.failure_category, "timeout")
        self.assertEqual(record.process_cleanup_status, "verified")
        self.assertEqual(record.timeout_phase, "initial_communicate")
        self.assertEqual(record.termination_signal, signal.SIGTERM)
        self.assertGreater(record.stdout_bytes, len(_SYNTHETIC_BODY))
        self.assertGreater(record.stderr_bytes, len(_SYNTHETIC_UNIT_ID))
        metadata = self.assert_content_free_bundle(bundle)
        self.assertEqual(
            metadata["diagnostic_schema_version"],
            "external-agent-diagnostics/2.0",
        )
        self.assertEqual(metadata["model"], "gpt-5.6-terra")
        self.assertEqual(metadata["reasoning_effort"], "medium")
        self.assertEqual(metadata["fallback_policy"], "none")
        self.assertNotIn("chain_of_thought", metadata)
        self.assertNotIn("reasoning_content", metadata)

    def test_timeout_escalates_to_kill_and_captures_final_drain(self) -> None:
        class KillDrainProcess(_Process):
            def communicate(self, *, input=None, timeout=None):
                del input, timeout
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(
                        self.command, 1, output="initial", stderr="initial-error"
                    )
                if self.calls == 2:
                    raise subprocess.TimeoutExpired(
                        self.command, 1, output="term", stderr="term-error"
                    )
                self.returncode = -signal.SIGKILL
                return "killed", "killed-error"

        class KillDrainRunner:
            def __call__(self, command, **_kwargs):
                return KillDrainProcess("timeout", list(command))

        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=KillDrainRunner(),
            diagnostic_root=self.root / "diagnostics",
        )
        signals: list[int] = []

        def kill_then_absent(_pid: int, requested: int) -> None:
            signals.append(requested)
            if requested == 0 and signal.SIGKILL in signals:
                raise ProcessLookupError

        with (
            patch(
                "archeos.representation_information.os.killpg",
                side_effect=kill_then_absent,
            ),
            self.assertRaisesRegex(Exception, "未产生可验证"),
        ):
            provider.analyze(self.batch())
        record = provider.execution_records[0]
        self.assertEqual(record.termination_signal, signal.SIGKILL)
        self.assertEqual(record.timeout_phase, "term_drain")
        self.assertIn(signal.SIGTERM, signals)
        self.assertIn(signal.SIGKILL, signals)
        self.assertGreater(record.stdout_bytes, len("initial"))

    def test_nonzero_bundle_is_metadata_only_and_private(self) -> None:
        provider, record = self._run_failure("nonzero")
        bundle = provider.diagnostic_root / record.processing_run_id
        self.assertEqual(record.failure_category, "runtime_nonzero_exit")
        metadata = self.assert_content_free_bundle(bundle)
        self.assertEqual(metadata["stdout_bytes"], len(_SYNTHETIC_BODY))
        self.assertEqual(metadata["stderr_bytes"], len(_SYNTHETIC_UNIT_ID))
        self.assertEqual(
            metadata["stdout_sha256"],
            "sha256:" + hashlib.sha256(_SYNTHETIC_BODY.encode()).hexdigest(),
        )
        self.assertEqual(
            metadata["stderr_sha256"],
            "sha256:" + hashlib.sha256(_SYNTHETIC_UNIT_ID.encode()).hexdigest(),
        )
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        for child in bundle.iterdir():
            self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o600)

    def test_transport_bundle_never_persists_stream_content(self) -> None:
        provider, record = self._run_failure("transport")
        self.assertEqual(record.provider_error_category, "network_or_transport")
        bundle = provider.diagnostic_root / record.processing_run_id
        self.assert_content_free_bundle(bundle)

    def test_contract_bundle_never_persists_stream_content(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=self.echoing_runner("top_level_missing"),
            diagnostic_root=self.root / "diagnostics",
        )
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(self.batch())
        record = provider.execution_records[0]
        self.assertEqual(record.failure_category, "result_contract_failure")
        self.assertEqual(record.contract_failure_stage, "top_level")
        bundle = provider.diagnostic_root / record.processing_run_id
        self.assert_content_free_bundle(bundle)

    def test_symlink_diagnostic_root_fails_closed_without_touching_target(self) -> None:
        target = self.root / "external-target"
        target.mkdir(mode=0o755)
        root = self.root / "diagnostics-link"
        root.symlink_to(target, target_is_directory=True)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=lambda *_args, **_kwargs: self.fail("Provider must not start"),
            diagnostic_root=root,
        )
        with self.assertRaisesRegex(Exception, "诊断目录不安全"):
            provider.analyze(self.batch())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(provider.cleanup_failure_diagnostics())

    def test_symlink_diagnostic_parent_fails_closed_without_touching_target(self) -> None:
        target = self.root / "external-parent-target"
        target.mkdir(mode=0o755)
        parent = self.root / "diagnostics-parent-link"
        parent.symlink_to(target, target_is_directory=True)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=lambda *_args, **_kwargs: self.fail("Provider must not start"),
            diagnostic_root=parent / "diagnostics",
        )
        with self.assertRaisesRegex(Exception, "诊断目录不安全"):
            provider.analyze(self.batch())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(list(target.iterdir()), [])

    def test_nonprivate_parent_fails_closed_before_provider_start(self) -> None:
        parent = self.root / "unsafe-parent"
        parent.mkdir(mode=0o700)
        os.chmod(parent, 0o777)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=lambda *_args, **_kwargs: self.fail("Provider must not start"),
            diagnostic_root=parent / "diagnostics",
        )
        with self.assertRaisesRegex(Exception, "诊断目录不安全"):
            provider.analyze(self.batch())
        self.assertFalse((parent / "diagnostics").exists())

    def test_durable_record_has_no_free_text_and_known_error_is_mapped(self) -> None:
        _provider, record = self._run_failure("known_error")
        self.assertEqual(record.provider_error_category, "rate_limited")
        payload = json.dumps(record.__dict__, ensure_ascii=False)
        self.assertNotIn("HTTP 429", payload)
        self.assertNotIn("rate limit", payload)
        self.assertNotIn('"stdout":', payload)
        self.assertNotIn('"stderr":', payload)

    def test_unknown_error_is_not_guessed(self) -> None:
        _provider, record = self._run_failure("unknown_error")
        self.assertEqual(record.provider_error_category, "unknown")

    def test_provider_error_mapping_requires_explicit_provider_context(self) -> None:
        known = {
            "Codex provider returned HTTP 401": "auth_or_permission",
            "Codex provider returned HTTP 429": "rate_limited",
            "Codex provider transport connection reset": "network_or_transport",
            "Codex provider returned HTTP 503": "service_unavailable",
            "Codex provider HTTP 503 connection reset": "service_unavailable",
            "Codex provider rejected output schema": "structured_output_rejected",
            "Codex provider returned HTTP 500": "provider_internal_error",
            "Codex request cancelled": "cancelled",
        }
        for stderr, expected in known.items():
            with self.subTest(stderr=stderr):
                self.assertEqual(_provider_error_category(stderr), expected)
        for stderr in ("401", "业务网络项目", "network rate limit", "server error"):
            with self.subTest(stderr=stderr):
                self.assertEqual(_provider_error_category(stderr), "unknown")

    def test_bundle_write_failure_leaves_local_only_audit_signal(self) -> None:
        provider = self.provider("unknown_error")
        with (
            patch(
                "archeos.representation_information._private_diagnostic_write",
                side_effect=OSError("synthetic write failure"),
            ),
            self.assertLogs("archeos.representation_information", "WARNING") as logs,
            self.assertRaisesRegex(Exception, "未产生可验证"),
        ):
            provider.analyze(self.batch())
        self.assertEqual(provider.execution_records[0].failure_category, "runtime_nonzero_exit")
        self.assertIn("本机失败诊断材料写入失败", "\n".join(logs.output))

    def test_nonzero_result_file_is_truthed_after_process_cleanup(self) -> None:
        _provider, record = self._run_failure("nonzero_with_result")
        self.assertEqual(record.failure_category, "runtime_nonzero_exit")
        self.assertTrue(record.result_file_present)
        self.assertGreater(record.result_size_bytes, 0)
        self.assertEqual(record.provider_error_category, "provider_internal_error")

    def test_runtime_start_failure_is_auditable(self) -> None:
        root = self.root / "diagnostics"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1",
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError(f"{_SYNTHETIC_BODY} {_SYNTHETIC_UNIT_ID}")
            ),
            diagnostic_root=root,
        )
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(self.batch())
        record = provider.execution_records[0]
        self.assertEqual(record.failure_category, "runtime_start_failure")
        self.assertEqual(record.process_cleanup_status, "not_started")
        self.assert_content_free_bundle(root / record.processing_run_id)

    def test_process_cleanup_failure_is_auditable(self) -> None:
        provider = self.provider("nonzero")
        with (
            patch(
                "archeos.representation_information.os.killpg",
                side_effect=PermissionError,
            ),
            self.assertRaisesRegex(Exception, "未产生可验证"),
        ):
            provider.analyze(self.batch())
        record = provider.execution_records[0]
        self.assertEqual(record.failure_category, "process_cleanup_failure")
        self.assertEqual(record.process_cleanup_status, "failed")
        bundle = provider.diagnostic_root / record.processing_run_id
        self.assert_content_free_bundle(bundle)

    def test_unexpired_historical_v1_bundle_is_not_rewritten(self) -> None:
        root = self.root / "diagnostics"
        legacy = root / "run_legacy"
        legacy.mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
        created_at = datetime.now(UTC)
        legacy_files = {
            "metadata.json": json.dumps(
                {
                    "diagnostic_schema_version": "external-agent-diagnostics/1.0",
                    "created_at": created_at.isoformat().replace("+00:00", "Z"),
                    "expires_at": (created_at + timedelta(hours=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
                sort_keys=True,
            ).encode(),
            "stdout.tail": b"historical synthetic stdout",
            "stderr.tail": b"historical synthetic stderr",
        }
        for name, value in legacy_files.items():
            path = legacy / name
            path.write_bytes(value)
            os.chmod(path, 0o600)

        self._run_failure("unknown_error")

        self.assertEqual(
            {path.name: path.read_bytes() for path in legacy.iterdir()},
            legacy_files,
        )

    def test_expired_bundle_is_purged_and_explicit_cleanup_removes_all(self) -> None:
        root = self.root / "diagnostics"
        expired = root / "run_expired"
        expired.mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
        (expired / "metadata.json").write_text(
            json.dumps(
                {
                    "created_at": (
                        datetime.now(UTC)
                        - timedelta(seconds=24 * 60 * 60 + 1)
                    ).isoformat().replace("+00:00", "Z"),
                    "expires_at": (
                        datetime.now(UTC) + timedelta(days=365)
                    ).isoformat().replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(expired / "metadata.json", 0o600)
        provider, record = self._run_failure("unknown_error")
        self.assertFalse(expired.exists())
        self.assertTrue((root / record.processing_run_id).is_dir())
        self.assertTrue(provider.cleanup_failure_diagnostics())
        self.assertFalse(root.exists())

    def test_malformed_metadata_cannot_extend_bundle_retention(self) -> None:
        root = self.root / "diagnostics"
        expired = root / "run_malformed"
        expired.mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
        (expired / "metadata.json").write_text(
            json.dumps(
                {
                    "created_at": "not-a-timestamp",
                    "expires_at": (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat().replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(expired / "metadata.json", 0o600)
        self._run_failure("unknown_error")
        self.assertFalse(expired.exists())


if __name__ == "__main__":
    unittest.main()
