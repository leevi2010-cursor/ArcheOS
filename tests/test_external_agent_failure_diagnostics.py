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
        raise AssertionError(f"unexpected mode {self.mode}")


class _Runner:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def __call__(self, command, **_kwargs):
        return _Process(self.mode, list(command))


class ExternalAgentFailureDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

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
        self.assertEqual(provider.execution_records[0].diagnostic_cleanup_status, "verified")

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
        (expired / "metadata.json").write_text(
            json.dumps(
                {
                    "expires_at": (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat().replace("+00:00", "Z")
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


if __name__ == "__main__":
    unittest.main()
