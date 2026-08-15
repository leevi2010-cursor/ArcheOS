from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from archeos import atomic_information, filesystem, representation_information
from archeos.representation_information import (
    RepresentationAnalysisBatch,
    RepresentationAnalysisUnit,
)
from tests.test_wechat_conversation import (
    build_synthetic_representation,
    synthetic_multi_batch_export,
)

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


def quality_export() -> dict[str, object]:
    payload = synthetic_multi_batch_export()
    payload["messages"] = [
        f"[2026-08-15 09:{index:02d}] Sender: " + ("Synthetic message." if index < 19 else "[图片] local_id=1")
        for index in range(50)
    ]
    return payload


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
    def test_full_path_runtime_write_guards_are_never_called(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("forbidden production write path was called")

        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            sources, representations, representation = build_synthetic_representation(root, quality_export())
            batch_value = gate.build_real_preflight(representation.representation_id, representations, sources)
            response = result_for(batch_value)
            response["input_fingerprint"] = gate.provider_request(batch_value)[1]
            stack.enter_context(patch.object(representation_information.RepresentationInformationService, "extract", forbidden))
            stack.enter_context(patch.object(representation_information, "_output_records", forbidden))
            stack.enter_context(patch.object(representation_information, "_manifest", forbidden))
            stack.enter_context(patch.object(filesystem, "publish_directory_no_replace", forbidden))
            stack.enter_context(patch.object(representation_information, "publish_directory_no_replace", forbidden))
            stack.enter_context(patch.object(atomic_information, "ingest_processing_package", forbidden))
            gate.run_authorized_representation(
                representation.representation_id, representations, sources,
                marker_path=root / "marker.json", review_root=root / "review",
                runner=lambda *args, **kwargs: FakeProcess(*args, payload=json.dumps(response), **kwargs),
            )
            self.assertFalse((root / "03_information").exists())
            self.assertFalse((root / "04_core").exists())

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
            with self.subTest(raw=raw), self.assertRaises(gate.GateError):
                gate.parse_and_validate(raw, value, fingerprint)

    def test_unknown_duplicate_no_anchor_and_incomplete_coverage_fail_closed(self):
        value = batch(); fingerprint = gate.input_fingerprint(gate.provider_input(value)); payload = result_for(value)
        payload["candidates"][0]["evidence_unit_ids"] = ["unknown"]
        with self.assertRaises((gate.GateError, gate.RepresentationInformationError)): gate.parse_and_validate(json.dumps(payload), value, fingerprint)
        payload = result_for(value); payload["candidates"][0]["evidence_unit_ids"] *= 2
        with self.assertRaises((gate.GateError, gate.RepresentationInformationError)): gate.parse_and_validate(json.dumps(payload), value, fingerprint)
        payload = result_for(value); payload["candidates"][0]["evidence_unit_ids"] = [unit(30).unit_id]
        with self.assertRaises((gate.GateError, gate.RepresentationInformationError)): gate.parse_and_validate(json.dumps(payload), value, fingerprint)
        payload = result_for(value); payload["candidates"] = payload["candidates"][:-1]
        with self.assertRaises((gate.GateError, gate.RepresentationInformationError)): gate.parse_and_validate(json.dumps(payload), value, fingerprint)

    def test_eligible_context_can_be_explicit_evidence_but_non_evidence_cannot(self):
        capable, incapable = unit(20, eligible=True), unit(21, eligible=False)
        for context, should_pass in (((capable,), True), ((incapable,), False)):
            value = batch(context=context); payload = result_for(value)
            payload["candidates"][0]["evidence_unit_ids"].append(context[0].unit_id)
            if should_pass:
                gate.parse_and_validate(json.dumps(payload), value, gate.input_fingerprint(gate.provider_input(value)))
            else:
                with self.assertRaises((gate.GateError, gate.RepresentationInformationError)): gate.parse_and_validate(json.dumps(payload), value, gate.input_fingerprint(gate.provider_input(value)))
        value = batch(context=(capable,)); payload = result_for(value)
        payload["candidates"][0]["evidence_unit_ids"] = [capable.unit_id]
        with self.assertRaises((gate.GateError, gate.RepresentationInformationError)):
            gate.parse_and_validate(json.dumps(payload), value, gate.input_fingerprint(gate.provider_input(value)))

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
        value = batch(); response = result_for(value)
        response["input_fingerprint"] = gate.provider_request(value)[1]
        payload = json.dumps(response)
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "marker.json"
            gate.run_real_call(value, marker_path=marker, runner=lambda *a, **kw: FakeProcess(*a, payload=payload, **kw))
            self.assertEqual(json.loads(marker.read_text())["status"], "completed")
            with self.assertRaises(gate.GateError): gate.run_real_call(value, marker_path=marker)
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "marker.json"
            with self.assertRaises(gate.GateError): gate.run_real_call(value, marker_path=marker, runner=lambda *a, **kw: FakeProcess(*a, code=1, **kw))
            self.assertEqual(json.loads(marker.read_text())["status"], "failed")

    def test_explicit_representation_preflight_reuses_canonical_wechat_units(self):
        with tempfile.TemporaryDirectory() as temp:
            sources, representations, representation = build_synthetic_representation(Path(temp), quality_export())
            value = gate.build_real_preflight(representation.representation_id, representations, sources)
            self.assertEqual(len(value.anchor_units), 19)
            self.assertEqual(len(gate.require_one_batch(gate._units_from_representation(representation, representations)).context_support_units) >= 0, True)

    def test_authorized_route_sends_bound_request_and_writes_local_packet(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sources, representations, representation = build_synthetic_representation(root, quality_export())
            value = gate.build_real_preflight(representation.representation_id, representations, sources)
            request, fingerprint = gate.provider_request(value)
            self.assertEqual(request["input_fingerprint"], fingerprint)
            self.assertEqual(gate.input_fingerprint({key: value for key, value in request.items() if key != "input_fingerprint"}), fingerprint)
            self.assertIn("Residue", " ".join(request["rules"]))
            payload = result_for(value)
            payload["input_fingerprint"] = fingerprint
            marker, review = root / "marker.json", root / "local-review-packet"
            gate.run_authorized_representation(representation.representation_id, representations, sources, marker_path=marker,
                                              review_root=review, runner=lambda *a, **kw: FakeProcess(*a, payload=json.dumps(payload), **kw))
            self.assertTrue((review / "review-packet.json").is_file())
            self.assertEqual(json.loads(marker.read_text())["status"], "completed")

    def test_marker_rejects_partial_and_symlink_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); marker = root / "marker.json"; marker.write_text("{", encoding="utf-8")
            with self.assertRaises(gate.GateError): gate.consume_marker(marker, "sha256:" + "0" * 64)
            target, linked = root / "target", root / "linked"
            target.mkdir(); linked.symlink_to(target, target_is_directory=True)
            with self.assertRaises(gate.GateError): gate.consume_marker(linked / "marker.json", "sha256:" + "0" * 64)

    def test_review_packet_is_local_private_and_uses_canonical_units(self):
        value = batch()
        parsed = gate.parse_and_validate(json.dumps(result_for(value)), value, gate.input_fingerprint(gate.provider_input(value)))
        with tempfile.TemporaryDirectory() as temp:
            path = gate.write_local_review_packet(value, parsed, Path(temp) / "packet")
            packet = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(packet["anchor_view"]), 19)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(gate.review_packet_readback(path, value, parsed))
            original = json.loads(path.read_text(encoding="utf-8"))
            mutations = (
                ("anchor_view", 0, "accounting", []),
                ("anchor_view", 0, "context_support_unit_ids", ["wrong"]),
                ("candidate_view", 0, "statement", "wrong"),
                ("candidate_view", 0, "canonical_evidence", []),
                ("context_support_view", None, None, [{}]),
            )
            for section, index, field, replacement in mutations:
                mutated = json.loads(json.dumps(original))
                if index is None:
                    mutated[section] = replacement
                else:
                    mutated[section][index][field] = replacement
                path.write_text(json.dumps(mutated), encoding="utf-8")
                self.assertFalse(gate.review_packet_readback(path, value, parsed))
            path = gate.write_local_review_packet(value, parsed, path.parent)
            gate.cleanup_local_review_packet(path.parent)
            self.assertFalse(path.exists())

    def test_synthetic_mode_never_writes_marker_and_tracked_outputs_are_anonymous(self):
        self.assertFalse(gate.synthetic_status()["real_marker_written"])
        tracked = [ROOT / "docs/experiments/semantic-quality-wechat/v0.1.0" / item for item in ("manifest.json", "RESULTS.md", "RECOMMENDATION.md")]
        forbidden = ("synthetic-source", "synthetic-representation", "unit_", "raw result", "sender")
        self.assertTrue(all(token not in path.read_text(encoding="utf-8") for path in tracked for token in forbidden))
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "marker.json"
            run = subprocess.run([sys.executable, str(HARNESS), "--synthetic", "--marker-path", str(marker)], cwd=ROOT, check=True, capture_output=True, text=True)
        status = json.loads(run.stdout)
        self.assertEqual(status["provider_calls"], 0)
        self.assertFalse(status["real_marker_written"])
        self.assertFalse(marker.exists())

    def test_synthetic_cli_never_starts_fake_codex_or_writes_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); binary = root / "bin"; binary.mkdir(); sentinel = root / "codex-started"
            codex = binary / "codex"
            codex.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 99\n", encoding="utf-8")
            codex.chmod(0o700)
            marker = root / "marker.json"
            environment = {"PATH": str(binary), "HOME": str(root / "home")}
            run = subprocess.run([sys.executable, str(HARNESS), "--synthetic", "--marker-path", str(marker)], cwd=ROOT, check=True, capture_output=True, text=True, env=environment)
            self.assertEqual(json.loads(run.stdout)["provider_calls"], 0)
            self.assertFalse(sentinel.exists())
            self.assertFalse(marker.exists())

    def test_tracked_issue76_diff_privacy_scan_rejects_real_identifier_canary(self):
        names = subprocess.run(["git", "diff", "--name-only", "origin/main...HEAD", "--", "docs/experiments/semantic-quality-wechat"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()
        diff = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in names)
        banned = ("src_" + r"[0-9a-f]{32}", "repr_" + r"[0-9a-f]{32}", "/" + "Users/", "Candidate" + " statement", "Evidence" + " excerpt")
        self.assertTrue(all(re.search(pattern, diff, re.IGNORECASE) is None for pattern in banned))
        self.assertIsNotNone(re.search(r"src_[0-9a-f]{32}", "src_" + "a" * 32))

    def test_esrch_means_absent_and_packet_wrapper_tamper_fails_marker(self):
        process = FakeProcess(["fake"])
        with patch.object(gate.os, "killpg", side_effect=ProcessLookupError):
            self.assertIsNone(gate._cleanup_process_group(process))
        value = batch(); response = result_for(value); response["input_fingerprint"] = gate.provider_request(value)[1]
        original = gate.write_local_review_packet
        def tamper(*args, **kwargs):
            path = original(*args, **kwargs)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["anchor_view"][0]["accounting"] = []
            path.write_text(json.dumps(payload), encoding="utf-8")
            return path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); marker = root / "marker.json"
            with patch.object(gate, "write_local_review_packet", side_effect=tamper), self.assertRaises(gate.GateError):
                gate.run_real_call(value, marker_path=marker, review_root=root / "review", runner=lambda *args, **kwargs: FakeProcess(*args, payload=json.dumps(response), **kwargs))
            self.assertEqual(json.loads(marker.read_text())["status"], "failed")

    def test_permission_error_and_packet_tamper_fail_closed_and_consume_marker(self):
        value = batch(); response = result_for(value)
        response["input_fingerprint"] = gate.provider_request(value)[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); marker = root / "marker.json"
            with patch.object(gate.os, "killpg", side_effect=PermissionError), self.assertRaises(gate.GateError):
                gate.run_real_call(value, marker_path=marker, runner=lambda *args, **kwargs: FakeProcess(*args, payload=json.dumps(response), **kwargs))
            self.assertEqual(json.loads(marker.read_text())["status"], "failed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); marker = root / "marker.json"
            with patch.object(gate, "review_packet_readback", return_value=False), self.assertRaises(gate.GateError):
                gate.run_real_call(value, marker_path=marker, review_root=root / "review", runner=lambda *args, **kwargs: FakeProcess(*args, payload=json.dumps(response), **kwargs))
            self.assertEqual(json.loads(marker.read_text())["status"], "failed")

    def test_production_wechat_gate_remains_covered_by_existing_test(self):
        existing = (ROOT / "tests/test_wechat_conversation.py").read_text(encoding="utf-8")
        self.assertIn("test_information_extract_fails_before_provider_or_durable_write", existing)


if __name__ == "__main__":
    unittest.main()
