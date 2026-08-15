from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from archeos.representation_information import RepresentationAnalysisBatch, RepresentationAnalysisUnit


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "docs/experiments/semantic-quality-wechat/v0.1.0/run_quality_gate.py"
spec = importlib.util.spec_from_file_location("issue76_gate", HARNESS)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def unit(number: int, *, eligible: bool = True) -> RepresentationAnalysisUnit:
    return RepresentationAnalysisUnit(
        unit_id=f"unit_{number:064d}", representation_id="synthetic-representation",
        source_id="synthetic-source", source_content_hash="0" * 64,
        representation_kind="wechat_conversation", kind="message", content=f"Synthetic {number}",
        structured_value=None, locator={"message": number}, context="Synthetic context",
        artifact_id="synthetic", artifact_locator="synthetic.json", analysis_eligible=eligible,
        exclusion_reason=None if eligible else "SYNTHETIC_CONTEXT_ONLY",
    )


def batch(*, context: tuple[RepresentationAnalysisUnit, ...] = ()) -> RepresentationAnalysisBatch:
    return RepresentationAnalysisBatch(tuple(unit(index) for index in range(1, 20)), context)


def result_for(value: RepresentationAnalysisBatch) -> dict[str, object]:
    payload = gate.provider_input(value)
    return {"protocol_version": gate.PROTOCOL_VERSION, "input_fingerprint": gate.input_fingerprint(payload),
            "candidates": [{"statement": "Synthetic statement", "semantic_type": "observation", "concerns": ["Synthetic"],
                            "evidence_unit_ids": [item.unit_id], "context": "Synthetic", "confidence": 1.0}
                           for item in value.anchor_units], "residue": []}


class FakeProcess:
    def __init__(self, command, *, payload: str | None = None, timeout: bool = False, code: int = 0, **kwargs):
        self.command, self.payload, self.timeout, self.returncode, self.pid = command, payload, timeout, code, 31337
        if payload is not None:
            Path(command[command.index("--output-last-message") + 1]).write_text(payload, encoding="utf-8")
    def communicate(self, input=None, timeout=None):
        if self.timeout:
            self.timeout = False
            raise subprocess.TimeoutExpired(self.command, timeout)
        return "", ""


class SemanticQualityWechatGateTest(unittest.TestCase):
    def test_no_extract_or_production_write_symbols(self):
        source = HARNESS.read_text(encoding="utf-8")
        self.assertNotIn(".extract(", source)
        self.assertNotIn("publish_directory_no_replace", source)
        self.assertNotIn("AtomicInformationStore", source)
        self.assertNotIn("WorldModel", source)

    def test_uses_canonical_batch_and_validator(self):
        source = HARNESS.read_text(encoding="utf-8")
        self.assertIn("_analysis_batches", source)
        self.assertIn("RepresentationInformationService._validate_batch_result", source)
        self.assertEqual(len(gate.require_one_batch(tuple(unit(i) for i in range(1, 20))).anchor_units), 19)

    def test_valid_bound_result(self):
        value = batch()
        parsed = gate.parse_and_validate(json.dumps(result_for(value)), value, result_for(value)["input_fingerprint"])
        self.assertEqual(len(parsed.candidates), 19)

    def test_tracked_schema_matches_the_runtime_strict_schema(self):
        schema = ROOT / "docs/experiments/semantic-quality-wechat/v0.1.0/schemas/result.schema.json"
        self.assertEqual(json.loads(schema.read_text(encoding="utf-8")), gate.strict_schema())

    def test_wrong_protocol_and_fingerprint_fail_closed(self):
        value = batch(); payload = result_for(value)
        payload["protocol_version"] = "wrong"
        with self.assertRaises(gate.ResultBindingError): gate.parse_and_validate(json.dumps(payload), value, "x")
        payload = result_for(value); payload["input_fingerprint"] = "sha256:" + "0" * 64
        with self.assertRaises(gate.ResultBindingError): gate.parse_and_validate(json.dumps(payload), value, gate.input_fingerprint(gate.provider_input(value)))

    def test_no_invalid_and_schema_invalid_output_fail_closed(self):
        value = batch(); fingerprint = gate.input_fingerprint(gate.provider_input(value))
        for raw in (None, "", "[", json.dumps({"protocol_version": gate.PROTOCOL_VERSION, "input_fingerprint": fingerprint, "candidates": [], "residue": [], "extra": 1}),
                    json.dumps({"protocol_version": gate.PROTOCOL_VERSION, "input_fingerprint": fingerprint, "candidates": [{"statement": "x"}], "residue": []})):
            with self.subTest(raw=raw):
                with self.assertRaises(gate.GateError): gate.parse_and_validate(raw, value, fingerprint)

    def test_unknown_duplicate_no_anchor_and_incomplete_coverage_fail_closed(self):
        value = batch(); fingerprint = gate.input_fingerprint(gate.provider_input(value)); payload = result_for(value)
        payload["candidates"][0]["evidence_unit_ids"] = ["unknown"]
        with self.assertRaises(Exception): gate.parse_and_validate(json.dumps(payload), value, fingerprint)
        payload = result_for(value); payload["candidates"][0]["evidence_unit_ids"] *= 2
        with self.assertRaises(Exception): gate.parse_and_validate(json.dumps(payload), value, fingerprint)
        payload = result_for(value); payload["candidates"][0]["evidence_unit_ids"] = [unit(30).unit_id]
        with self.assertRaises(Exception): gate.parse_and_validate(json.dumps(payload), value, fingerprint)
        payload = result_for(value); payload["candidates"] = payload["candidates"][:-1]
        with self.assertRaises(Exception): gate.parse_and_validate(json.dumps(payload), value, fingerprint)

    def test_eligible_context_can_be_explicit_evidence_but_non_evidence_cannot(self):
        capable, incapable = unit(20, eligible=True), unit(21, eligible=False)
        for context, should_pass in (((capable,), True), ((incapable,), False)):
            value = batch(context=context); payload = result_for(value)
            payload["candidates"][0]["evidence_unit_ids"].append(context[0].unit_id)
            if should_pass:
                gate.parse_and_validate(json.dumps(payload), value, gate.input_fingerprint(gate.provider_input(value)))
            else:
                with self.assertRaises(Exception): gate.parse_and_validate(json.dumps(payload), value, gate.input_fingerprint(gate.provider_input(value)))

    def test_timeout_and_nonzero_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            schema, output = Path(temp) / "schema.json", Path(temp) / "result.json"
            schema.write_text("{}", encoding="utf-8")
            original_killpg = gate.os.killpg
            gate.os.killpg = lambda *_args: None
            try:
                with self.assertRaises(gate.GateError): gate.run_codex_cli("x", schema, output, runner=lambda *a, **kw: FakeProcess(*a, timeout=True, **kw))
            finally:
                gate.os.killpg = original_killpg
            with self.assertRaises(gate.GateError): gate.run_codex_cli("x", schema, output, runner=lambda *a, **kw: FakeProcess(*a, code=9, **kw))

    def test_real_marker_is_one_shot_and_failure_consumes_it(self):
        value = batch(); payload = json.dumps(result_for(value))
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "marker.json"
            gate.run_real_call(value, marker_path=marker, runner=lambda *a, **kw: FakeProcess(*a, payload=payload, **kw))
            self.assertEqual(json.loads(marker.read_text())["status"], "completed")
            with self.assertRaises(gate.GateError): gate.run_real_call(value, marker_path=marker)
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "marker.json"
            with self.assertRaises(gate.GateError): gate.run_real_call(value, marker_path=marker, runner=lambda *a, **kw: FakeProcess(*a, code=1, **kw))
            self.assertEqual(json.loads(marker.read_text())["status"], "failed")

    def test_review_packet_is_local_private_and_uses_canonical_units(self):
        value = batch()
        parsed = gate.parse_and_validate(json.dumps(result_for(value)), value, gate.input_fingerprint(gate.provider_input(value)))
        with tempfile.TemporaryDirectory() as temp:
            path = gate.write_local_review_packet(value, parsed, Path(temp) / "packet")
            packet = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(packet["anchor_view"]), 19)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_synthetic_mode_never_writes_marker_and_tracked_outputs_are_anonymous(self):
        self.assertFalse(gate.synthetic_status()["real_marker_written"])
        tracked = [ROOT / "docs/experiments/semantic-quality-wechat/v0.1.0" / item for item in ("manifest.json", "RESULTS.md", "RECOMMENDATION.md")]
        forbidden = ("synthetic-source", "synthetic-representation", "unit_", "raw result", "sender")
        self.assertTrue(all(token not in path.read_text(encoding="utf-8") for path in tracked for token in forbidden))

    def test_production_wechat_gate_remains_covered_by_existing_test(self):
        existing = (ROOT / "tests/test_wechat_conversation.py").read_text(encoding="utf-8")
        self.assertIn("test_information_extract_fails_before_provider_or_durable_write", existing)


if __name__ == "__main__":
    unittest.main()
