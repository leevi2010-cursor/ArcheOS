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
HARNESS = ROOT / "docs/experiments/codex-cli-diagnosis/v0.1.0/run_diagnosis.py"
SPEC = importlib.util.spec_from_file_location("codex_cli_diagnosis", HARNESS)
assert SPEC and SPEC.loader
diagnosis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = diagnosis
SPEC.loader.exec_module(diagnosis)


def candidate(unit_id: str) -> dict[str, object]:
    return {"statement": "Synthetic fact.", "semantic_type": "observation", "concerns": ["Synthetic"],
            "evidence_unit_ids": [unit_id], "context": "Synthetic context.", "confidence": 1.0}


class FakeProcess:
    def __init__(self, command, *, stdin=None, stdout=None, stderr=None, text=None, start_new_session=None, env=None):
        self.command = command
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        if "--output-schema" not in self.command:
            return "SYNTHETIC_OK", ""
        result = Path(self.command[self.command.index("--output-last-message") + 1])
        if "simple-result.schema.json" in " ".join(self.command):
            payload = {"answer": "SYNTHETIC_OK"}
        else:
            request = json.loads(input.rsplit("\n", 1)[-1])
            units = request["analysis_package"]["units"] if request["protocol_version"] == "external-agent-handoff/1.0" else request["anchor_units"]
            payload = {"protocol_version": request["protocol_version"], "input_fingerprint": request["input_fingerprint"],
                       "candidates": [candidate(item["unit_id"]) for item in units], "residue": []}
        result.write_text(json.dumps(payload), encoding="utf-8")
        return "", ""


class NonzeroProcess(FakeProcess):
    def communicate(self, input=None, timeout=None):
        self.returncode = 2
        return "", "error: unknown option --output-schema"


class CodexCliDiagnosisTest(unittest.TestCase):
    def test_matrix_has_fixed_a_to_f_budget(self):
        self.assertEqual([case.case_id for case in diagnosis.cases()], list("ABCDEF"))
        self.assertEqual(diagnosis.MAX_MODEL_CALLS, 6)

    def test_same_and_split_layout_are_distinct(self):
        values = {case.case_id: case for case in diagnosis.cases()}
        self.assertEqual(values["C"].layout, "same")
        self.assertEqual(values["D"].layout, "split")

    def test_synthetic_fake_matrix_recovers_baseline_without_model(self):
        with patch.object(diagnosis.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "codex-cli test", "")):
            report = diagnosis.run_matrix(codex_bin="synthetic-codex", timeout=1, popen=FakeProcess)
        self.assertEqual(report["model_calls"], 6)
        self.assertEqual(report["execution_baseline"], "recovered")
        self.assertTrue(all(case["strict_validation_status"] == "passed" for case in report["cases"]))

    def test_nonzero_preserves_actionable_classification_and_stops(self):
        with patch.object(diagnosis.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "codex-cli test", "")):
            report = diagnosis.run_matrix(codex_bin="synthetic-codex", timeout=1, popen=NonzeroProcess)
        self.assertEqual(report["model_calls"], 1)
        self.assertEqual(report["root_cause_classification"], "cli_flag_or_version_incompatibility")
        self.assertIn("unknown option", report["cases"][0]["stderr_tail"])

    def test_redacts_credential_like_diagnostics(self):
        safe = diagnosis.redact("Authorization: Bearer synthetic-token-value token=[synthetic]")
        self.assertNotIn("synthetic-token-value", safe)
        self.assertNotIn("[synthetic]", safe)
        self.assertIn("REDACTED_CREDENTIAL", safe)

    def test_definitive_schema_rejection_outranks_nonfatal_auth_sidecar(self):
        category = diagnosis.classify(exit_code=1, timed_out=False,
                                      stderr="AuthRequired warning; invalid_json_schema: type key required",
                                      result_present=False, json_status="not_present", strict_status="not_run")
        self.assertEqual(category, "structured_output_schema_failure")

    def test_child_environment_is_allowlisted(self):
        with patch.dict(diagnosis.os.environ, {"PATH": "/bin", "HOME": "/tmp/home", "UNRELATED_SECRET": "nope"}, clear=True):
            environment = diagnosis.safe_environment()
        self.assertEqual(environment["PATH"], "/bin")
        self.assertNotIn("UNRELATED_SECRET", environment)

    def test_experiment_never_imports_or_writes_production_runtime(self):
        source = HARNESS.read_text(encoding="utf-8")
        self.assertNotIn("from archeos", source)
        self.assertNotIn("AtomicInformation", source)
        self.assertNotIn("WorldModel", source)
        self.assertNotIn("run_quality_gate", source)


if __name__ == "__main__":
    unittest.main()
