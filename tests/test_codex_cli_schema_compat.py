from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "docs/experiments/codex-cli-schema-compat/v0.1.0/run_schema_compat.py"
SPEC = importlib.util.spec_from_file_location("codex_cli_schema_compat", HARNESS)
assert SPEC and SPEC.loader
compat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compat
SPEC.loader.exec_module(compat)


class FakeProcess:
    def __init__(self, command, **_kwargs):
        self.command = command
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        result = Path(self.command[self.command.index("--output-last-message") + 1])
        schema = json.loads(Path(self.command[self.command.index("--output-schema") + 1]).read_text())
        if set(schema["properties"]) == {"answer"}:
            payload = {"answer": "SYNTHETIC_OK"}
        else:
            request = json.loads(str(input).rsplit("\n", 1)[-1])
            support = request["context_support_units"][0]["unit_id"]
            payload = {
                "protocol_version": request["protocol_version"],
                "input_fingerprint": request["input_fingerprint"],
                "candidates": [
                    {
                        "statement": "Synthetic anchor is retained with bounded context.",
                        "semantic_type": "observation",
                        "concerns": ["synthetic"],
                        "evidence_unit_ids": [anchor["unit_id"], support],
                        "context": "Synthetic bounded context is explicitly cited.",
                        "confidence": 1.0,
                    }
                    for anchor in request["anchor_units"]
                ],
                "residue": [],
            }
        result.write_text(json.dumps(payload), encoding="utf-8")
        return "", ""


class NonzeroSchemaProcess(FakeProcess):
    def communicate(self, input=None, timeout=None):
        self.returncode = 2
        return "", "invalid_json_schema: type key required"


class InvalidContextProcess(FakeProcess):
    def communicate(self, input=None, timeout=None):
        stdout, stderr = super().communicate(input=input, timeout=timeout)
        result = Path(self.command[self.command.index("--output-last-message") + 1])
        payload = json.loads(result.read_text())
        if "candidates" in payload:
            payload["candidates"][0]["evidence_unit_ids"] = ["unknown-unit"]
            result.write_text(json.dumps(payload), encoding="utf-8")
        return stdout, stderr


class CodexCliSchemaCompatibilityTest(unittest.TestCase):
    def test_preflight_accepts_tracked_compatible_schemas(self):
        value = compat.preflight()
        self.assertEqual(value["semantic_schema_preflight"], "passed")
        self.assertTrue(value["candidate_residue_schema_preserved"])

    def test_preflight_recurses_and_requires_matching_json_type(self):
        valid = {"type": "object", "properties": {"nested": {"type": "array", "items": {"type": "boolean", "const": True}}}}
        compat.preflight_const_types(valid)
        for invalid in (
            {"const": "x"},
            {"type": "number", "const": "x"},
            {"type": "integer", "const": 1},
            {"type": "boolean", "const": None},
        ):
            with self.assertRaises(compat.SchemaCompatibilityError):
                compat.preflight_const_types(invalid)

    def test_incompatible_preflight_stops_before_a_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            broken = Path(temporary) / "broken.schema.json"
            broken.write_text(json.dumps({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"answer": {"const": "SYNTHETIC_OK"}},
            }), encoding="utf-8")
            with patch.object(compat, "SIMPLE_SCHEMA_PATH", broken), patch.object(
                compat.subprocess, "run", side_effect=AssertionError("version/model path must not start")
            ):
                with self.assertRaises(compat.SchemaCompatibilityError):
                    compat.run_experiment(codex_bin="synthetic-codex", timeout=1, popen=FakeProcess)

    def test_tracked_schema_is_exactly_corrected_existing_contract(self):
        tracked = compat.load_schema(compat.SEMANTIC_SCHEMA_PATH)
        self.assertEqual(tracked, compat.semantic_schema())
        protocol = tracked["properties"]["protocol_version"]
        self.assertEqual(protocol, {"type": "string", "const": compat.PROTOCOL_VERSION})
        self.assertFalse(tracked["additionalProperties"])

    def test_synthetic_batches_preserve_two_and_nineteen_anchor_shape(self):
        small, large = compat.synthetic_batch(2), compat.synthetic_batch(19)
        self.assertEqual(len(small.anchor_units), 2)
        self.assertEqual(len(large.anchor_units), 19)
        self.assertEqual(len(large.context_support_units), 1)
        support = large.context_support_units[0].unit_id
        self.assertTrue(all(unit.context_support_unit_ids == (support,) for unit in large.anchor_units))

    def test_fake_runs_recover_strict_baseline_and_context_evidence(self):
        with patch.object(compat.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "codex-cli synthetic", "")):
            report = compat.run_experiment(codex_bin="synthetic-codex", timeout=1, popen=FakeProcess)
        self.assertEqual(report["model_calls"], 3)
        self.assertEqual(report["strict_execution_baseline"], "recovered")
        last = report["runs"][-1]
        self.assertEqual(last["anchor_units"], 19)
        self.assertEqual(last["unaccounted_anchor_units"], 0)
        self.assertEqual(last["context_evidence_validator_status"], "passed")

    def test_first_failure_stops_budget_without_retry(self):
        with patch.object(compat.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "codex-cli synthetic", "")):
            report = compat.run_experiment(codex_bin="synthetic-codex", timeout=1, popen=NonzeroSchemaProcess)
        self.assertEqual(report["model_calls"], 1)
        self.assertEqual(report["failure_category"], "structured_output_schema_failure")
        self.assertEqual(len(report["runs"]), 1)

    def test_context_evidence_error_fails_closed(self):
        spec = compat.runs()[1]
        result = compat.run_one(spec, codex_bin="synthetic-codex", timeout=1, popen=InvalidContextProcess)
        self.assertEqual(result["strict_schema_status"], "failed")
        self.assertEqual(result["unaccounted_anchor_units"], 2)
        self.assertEqual(result["failure_category"], "structured_output_schema_failure")

    def test_report_must_remain_outside_repo(self):
        with self.assertRaises(ValueError):
            compat.write_report(ROOT / "local-report.json", {})
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            compat.write_report(report, {"synthetic_only": True})
            self.assertEqual(json.loads(report.read_text()), {"synthetic_only": True})
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_redacts_credential_like_diagnostics_and_limits_child_environment(self):
        self.assertNotIn("secret-value", compat.redact("token=secret-value"))
        with patch.dict(compat.os.environ, {"PATH": "/bin", "HOME": "/tmp/home", "SECRET": "no"}, clear=True):
            environment = compat.safe_environment()
        self.assertEqual(environment["PATH"], "/bin")
        self.assertNotIn("SECRET", environment)

    def test_experiment_never_opens_real_input_or_writes_long_term_stores(self):
        source = HARNESS.read_text(encoding="utf-8")
        for forbidden in ("LocalManagedSourceRepository", "LocalRepresentationRepository", "AtomicInformation", "WorldModel", "MARKER_PATH"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
