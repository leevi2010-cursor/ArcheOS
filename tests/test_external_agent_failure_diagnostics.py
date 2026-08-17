from __future__ import annotations

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
    _MAX_DIAGNOSTIC_STREAM_BYTES,
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisBatch,
    RepresentationAnalysisUnit,
    _bounded_redacted_tail,
    _provider_error_category,
)


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
                output="partial stdout ",
                stderr="Authorization: Bearer secret-value partial stderr ",
            )
        if self.mode == "timeout":
            self.returncode = -signal.SIGTERM
            return "drained stdout", "drained stderr"
        if self.mode == "nonzero":
            self.returncode = 9
            return "X" * (_MAX_DIAGNOSTIC_STREAM_BYTES + 100), (
                "token=private-token " + "Y" * (_MAX_DIAGNOSTIC_STREAM_BYTES + 100)
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
        from tests.test_semantic_handoff import FakeRunner

        root = self.root / "diagnostics"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="synthetic-1", runner=FakeRunner(), diagnostic_root=root
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
        self.assertGreater(record.stdout_bytes, len("partial stdout"))
        self.assertGreater(record.stderr_bytes, len("partial stderr"))
        self.assertIn("drained stdout", (bundle / "stdout.tail").read_text())
        self.assertNotIn("secret-value", (bundle / "stderr.tail").read_text())
        metadata = json.loads((bundle / "metadata.json").read_text())
        self.assertEqual(metadata["model"], "gpt-5.6")
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

    def test_nonzero_bundle_is_bounded_redacted_and_private(self) -> None:
        provider, record = self._run_failure("nonzero")
        bundle = provider.diagnostic_root / record.processing_run_id
        self.assertEqual(record.failure_category, "runtime_nonzero_exit")
        self.assertLessEqual((bundle / "stdout.tail").stat().st_size, _MAX_DIAGNOSTIC_STREAM_BYTES)
        self.assertLessEqual((bundle / "stderr.tail").stat().st_size, _MAX_DIAGNOSTIC_STREAM_BYTES)
        self.assertNotIn("private-token", (bundle / "stderr.tail").read_text())
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        for child in bundle.iterdir():
            self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o600)

    def test_credential_redaction_covers_json_quoted_and_basic_forms(self) -> None:
        raw = (
            '{"access_token":"token-one","api_key":"key-two"} '
            'password="password-three" Authorization: Basic basic-four'
        )
        tail = _bounded_redacted_tail(raw)
        for secret in ("token-one", "key-two", "password-three", "basic-four"):
            self.assertNotIn(secret, tail)
        self.assertGreaterEqual(tail.count("[REDACTED]"), 4)

    def test_multibyte_tail_never_exceeds_64_kib_after_encoding(self) -> None:
        tail = _bounded_redacted_tail("前" + "界" * _MAX_DIAGNOSTIC_STREAM_BYTES)
        self.assertLessEqual(len(tail.encode("utf-8")), _MAX_DIAGNOSTIC_STREAM_BYTES)

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
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic")),
            diagnostic_root=root,
        )
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(self.batch())
        record = provider.execution_records[0]
        self.assertEqual(record.failure_category, "runtime_start_failure")
        self.assertEqual(record.process_cleanup_status, "not_started")
        self.assertTrue((root / record.processing_run_id / "metadata.json").is_file())
        self.assertIn(
            "synthetic",
            (root / record.processing_run_id / "stderr.tail").read_text(encoding="utf-8"),
        )

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
