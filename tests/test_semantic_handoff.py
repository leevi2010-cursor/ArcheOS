from __future__ import annotations

import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from archeos.atomic_information import JsonlAtomicInformationStore
from archeos.representation import (
    AdapterArtifact,
    AdapterBuildResult,
    LocalRepresentationRepository,
    RepresentationService,
    WechatConversationRepresentationAdapter,
)
from archeos.representation_information import (
    EXTERNAL_AGENT_PROTOCOL_V1,
    EXTERNAL_AGENT_PROTOCOL_V2,
    EXTERNAL_AGENT_PROTOCOL_V3,
    EXTERNAL_AGENT_PROTOCOL_V3_1,
    EXTERNAL_AGENT_PROTOCOL_V3_2,
    EXTERNAL_AGENT_PROTOCOL_VERSION,
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationAnalysisUnit,
    RepresentationCandidateDraft,
    RepresentationInformationError,
    RepresentationInformationService,
    RepresentationResidueDraft,
    _analysis_batches,
    _canonical_fingerprint,
    _external_agent_request,
    _parse_external_agent_result,
    _units_from_representation,
    external_agent_representation_analysis_schema,
    validate_representation_information_package,
)
from archeos.semantic_handoff import (
    ExternalAgentSemanticHandoffService,
    SemanticHandoffError,
    SemanticPrivacyBinding,
    _package_fingerprint,
    validate_completed_published_audits,
)
from archeos.source import LocalManagedSourceRepository

_CONTRACT_DIAGNOSTIC_FIELDS = (
    "contract_failure_stage",
    "candidate_item_count",
    "residue_item_count",
    "accounting_item_count",
    "candidate_anchor_ref_count",
    "residue_anchor_ref_count",
    "duplicate_anchor_ref_count",
    "duplicate_accounting_count",
    "dual_assignment_count",
    "missing_anchor_count",
    "unknown_anchor_ref_count",
)
_AUDIT_DIAGNOSTIC_FIELDS = (
    "diagnostic_schema_version",
    "elapsed_ms",
    "deadline_ms",
    "exit_code",
    "termination_signal",
    "timeout_phase",
    "provider_error_category",
    "result_file_present",
    "result_size_bytes",
    "stdout_bytes",
    "stderr_bytes",
    "process_cleanup_status",
)


class JsonAdapter:
    name = "synthetic"
    version = "1.0"
    kind = "markdown_blocks"
    supported_media_types = ("application/synthetic",)

    def __init__(self, blocks: int = 1) -> None:
        self.blocks = blocks

    def build(self, _source, _materialized, staging_dir, _configuration):
        artifact = staging_dir / "artifacts" / "synthetic.json"
        artifact.write_text(
            json.dumps(
                {
                    "blocks": [
                        {
                            "kind": "paragraph",
                            "raw": f"Synthetic business input {index}.",
                            "source_locator": {"line": index},
                        }
                        for index in range(1, self.blocks + 1)
                    ]
                }
            ),
            encoding="utf-8",
        )
        return AdapterBuildResult(
            self.kind,
            (
                AdapterArtifact(
                    "structure", "artifacts/synthetic.json", "application/json"
                ),
            ),
            1.0,
        )


class FakeProcess:
    def __init__(self, command, *, mode: str, calls: list[list[str]]):
        self.command = list(command)
        self.mode = mode
        self.calls = calls
        self.pid = 99999999
        self.returncode: int | None = None
        calls.append(self.command)

    def communicate(self, *, input: str | None = None, timeout: float | None = None):
        del timeout
        if self.mode == "nonzero":
            self.returncode = 7
            return "", "synthetic nonzero"
        if self.mode == "timeout":
            from subprocess import TimeoutExpired

            raise TimeoutExpired(self.command, 0.01)
        assert input is not None
        request = json.loads(input.split("Request:\n", 1)[1])
        if self.mode == "wrong_binding":
            request["input_fingerprint"] = "sha256:" + "0" * 64
        result_path = Path(
            self.command[self.command.index("--output-last-message") + 1]
        )
        if self.mode != "no_result":
            is_v3 = request["protocol_version"] in {
                EXTERNAL_AGENT_PROTOCOL_V3,
                EXTERNAL_AGENT_PROTOCOL_V3_1,
                EXTERNAL_AGENT_PROTOCOL_V3_2,
            }
            is_v32 = (
                request["protocol_version"] == EXTERNAL_AGENT_PROTOCOL_V3_2
            )
            anchor_ids = [unit["unit_id"] for unit in request["anchor_units"]]
            result = {
                "protocol_version": request["protocol_version"],
                "input_fingerprint": request["input_fingerprint"],
                "anchor_accounting": (
                    {unit_id: "candidate" for unit_id in anchor_ids}
                    if is_v32
                    else [
                        {
                            "anchor_unit_id": unit_id,
                            "accounted_as": "candidate",
                        }
                        for unit_id in anchor_ids
                    ]
                ),
                "candidates": [
                    {
                        "statement": "Synthetic statement.",
                        "semantic_type": "observation",
                        "concerns": ["Synthetic"],
                        **(
                            {
                                "anchor_unit_ids": [unit["unit_id"]],
                                "supporting_evidence_unit_ids": [],
                            }
                            if is_v3
                            else {"evidence_unit_ids": [unit["unit_id"]]}
                        ),
                        "context": "Synthetic context.",
                        "confidence": 0.9,
                    }
                    for unit in request["anchor_units"]
                ],
                "residue": [],
            }

            def set_accounting(index: int, value: str) -> None:
                if is_v32:
                    result["anchor_accounting"][anchor_ids[index]] = value
                else:
                    result["anchor_accounting"][index]["accounted_as"] = value

            if self.mode == "invalid_json":
                result_path.write_text("{synthetic", encoding="utf-8")
            else:
                if self.mode == "top_level_missing":
                    result.pop("residue")
                elif self.mode == "top_level_extra":
                    result["unexpected"] = True
                elif self.mode == "candidate_shape":
                    result["candidates"][0].pop("statement")
                elif self.mode == "candidate_semantic":
                    result["candidates"][0]["semantic_type"] = "unsupported"
                elif self.mode == "candidate_confidence":
                    result["candidates"][0]["confidence"] = 2
                elif self.mode == "residue_shape":
                    result["residue"] = [
                        {"anchor_unit_ids": []}
                        if is_v3
                        else {"evidence_unit_ids": []}
                    ]
                elif self.mode == "candidate_unknown_reference":
                    field = "anchor_unit_ids" if is_v3 else "evidence_unit_ids"
                    result["candidates"][0][field] = ["unit_" + "f" * 64]
                elif self.mode == "residue_unknown_reference":
                    result["residue"] = [
                        {
                            (
                                "anchor_unit_ids"
                                if is_v3
                                else "evidence_unit_ids"
                            ): ["unit_" + "f" * 64],
                            "reason_not_absorbed": "Synthetic residue.",
                            "future_value_or_uncertainty": "Synthetic uncertainty.",
                        }
                    ]
                elif self.mode == "anchor_uncovered":
                    result["candidates"] = []
                elif self.mode == "dual_assignment":
                    result["residue"] = [
                        {
                            "anchor_unit_ids": [anchor_ids[0]],
                            "reason_not_absorbed": "Synthetic conflict.",
                            "future_value_or_uncertainty": "Synthetic uncertainty.",
                        }
                    ]
                elif self.mode == "all_residue":
                    result["candidates"] = []
                    result["residue"] = [
                        {
                            (
                                "anchor_unit_ids"
                                if is_v3
                                else "evidence_unit_ids"
                            ): [unit["unit_id"]],
                            "reason_not_absorbed": "Synthetic unresolved input.",
                            "future_value_or_uncertainty": "Synthetic future value.",
                        }
                        for unit in request["anchor_units"]
                    ]
                    for index in range(len(anchor_ids)):
                        set_accounting(index, "residue")
                elif self.mode == "mixed":
                    midpoint = len(request["anchor_units"]) // 2
                    result["candidates"] = result["candidates"][:midpoint]
                    result["residue"] = [
                        {
                            "anchor_unit_ids": [unit["unit_id"]],
                            "reason_not_absorbed": "Synthetic unresolved input.",
                            "future_value_or_uncertainty": "Synthetic future value.",
                        }
                        for unit in request["anchor_units"][midpoint:]
                    ]
                    for index in range(midpoint, len(anchor_ids)):
                        set_accounting(index, "residue")
                elif self.mode == "coverage_summary":
                    anchors = request["anchor_units"]
                    result["candidates"] = result["candidates"][:20]
                    result["candidates"].append(
                        {
                            **result["candidates"][0],
                            "statement": "Synthetic duplicate assignment.",
                        }
                    )
                    result["residue"] = [
                        {
                            "anchor_unit_ids": [unit["unit_id"]],
                            "reason_not_absorbed": "Synthetic unresolved input.",
                            "future_value_or_uncertainty": "Synthetic future value.",
                        }
                        for unit in anchors[19:35]
                    ]
                    if is_v32:
                        result["anchor_accounting"].pop(anchor_ids[-1])
                    else:
                        result["anchor_accounting"][-1]["anchor_unit_id"] = (
                            anchors[0]["unit_id"]
                        )
                elif self.mode == "candidate_no_anchor":
                    result["candidates"][0]["anchor_unit_ids"] = []
                elif self.mode == "candidate_duplicate_anchor":
                    anchor_id = result["candidates"][0]["anchor_unit_ids"][0]
                    result["candidates"][0]["anchor_unit_ids"] = [
                        anchor_id,
                        anchor_id,
                    ]
                elif self.mode == "same_anchor_multiple_candidates":
                    result["candidates"].append(
                        {
                            **result["candidates"][0],
                            "statement": "Second synthetic statement.",
                        }
                    )
                elif self.mode == "candidate_context_as_anchor":
                    result["candidates"][0]["anchor_unit_ids"] = [
                        request["context_support_units"][0]["unit_id"]
                    ]
                elif self.mode == "candidate_anchor_as_support":
                    result["candidates"][0]["supporting_evidence_unit_ids"] = [
                        request["anchor_units"][0]["unit_id"]
                    ]
                elif self.mode in {
                    "candidate_non_evidence_context",
                    "candidate_evidence_context",
                }:
                    result["candidates"][0]["supporting_evidence_unit_ids"] = [
                        request["context_support_units"][0]["unit_id"]
                    ]
                elif self.mode == "residue_context_reference":
                    result["candidates"] = []
                    result["residue"] = [
                        {
                            "anchor_unit_ids": [
                                request["context_support_units"][0]["unit_id"]
                            ],
                            "reason_not_absorbed": "Synthetic unresolved input.",
                            "future_value_or_uncertainty": "Synthetic future value.",
                        }
                    ]
                    set_accounting(0, "residue")
                elif self.mode == "accounting_missing_anchor":
                    if is_v32:
                        result["anchor_accounting"].pop(anchor_ids[-1])
                    else:
                        result["anchor_accounting"] = []
                elif self.mode == "accounting_extra_anchor":
                    if is_v32:
                        result["anchor_accounting"]["unit_" + "f" * 64] = (
                            "candidate"
                        )
                elif self.mode == "accounting_unknown_anchor":
                    if is_v32:
                        value = result["anchor_accounting"].pop(anchor_ids[0])
                        result["anchor_accounting"]["unit_" + "f" * 64] = value
                    else:
                        result["anchor_accounting"][0]["anchor_unit_id"] = (
                            "unit_" + "f" * 64
                        )
                elif self.mode == "accounting_wrong_outcome":
                    set_accounting(0, "residue")
                elif self.mode == "accounting_wrong_enum":
                    set_accounting(0, "unsupported")
                elif self.mode == "accounting_context_only":
                    if is_v32:
                        value = result["anchor_accounting"].pop(anchor_ids[0])
                        result["anchor_accounting"]["unit_" + "e" * 64] = value
                    else:
                        result["anchor_accounting"][0]["anchor_unit_id"] = (
                            "unit_" + "e" * 64
                        )
                raw_result = json.dumps(result)
                if self.mode == "accounting_duplicate_key" and is_v32:
                    key = json.dumps(anchor_ids[0])
                    value = json.dumps(result["anchor_accounting"][anchor_ids[0]])
                    marker = f'"anchor_accounting": {{{key}: {value}'
                    replacement = (
                        f'"anchor_accounting": {{{key}: {value}, {key}: {value}'
                    )
                    raw_result = raw_result.replace(marker, replacement, 1)
                result_path.write_text(raw_result, encoding="utf-8")
        self.returncode = 0
        return "", ""


class FakeRunner:
    def __init__(self, mode: str = "valid"):
        self.mode = mode
        self.calls: list[list[str]] = []
        self.schemas: list[dict[str, object]] = []

    def __call__(self, command, **_kwargs):
        command = list(command)
        self.schemas.append(
            json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        )
        return FakeProcess(command, mode=self.mode, calls=self.calls)


class SequenceRunner(FakeRunner):
    def __init__(self, *modes: str):
        super().__init__()
        self.modes = list(modes)

    def __call__(self, command, **_kwargs):
        if not self.modes:
            raise AssertionError("unexpected extra External Agent call")
        self.mode = self.modes.pop(0)
        return super().__call__(command, **_kwargs)


class SemanticHandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def unit(self) -> RepresentationAnalysisUnit:
        return RepresentationAnalysisUnit(
            unit_id="unit_" + "a" * 64,
            representation_id="repr_" + "b" * 64,
            source_id="src_" + "c" * 32,
            source_content_hash="sha256:" + "d" * 64,
            representation_kind="markdown_blocks",
            kind="block",
            content="Synthetic provider input.",
            structured_value=None,
            locator={"line": 1},
            context="Synthetic context.",
            artifact_id="artifact_" + "e" * 64,
            artifact_locator="artifacts/synthetic.json",
            analysis_eligible=True,
        )

    def units(self, count: int, *, start: int = 1) -> tuple[RepresentationAnalysisUnit, ...]:
        return tuple(
            replace(
                self.unit(),
                unit_id=f"unit_{index:064x}",
                content=f"Synthetic provider input {index}.",
                locator={"line": index},
            )
            for index in range(start, start + count)
        )

    def test_codex_cli_provider_preserves_strict_binding_and_schema_shape(self) -> None:
        runner = FakeRunner()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=runner
        )
        result = provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        self.assertEqual(result.candidates[0].evidence_unit_ids, (self.unit().unit_id,))
        schema = runner.schemas[0]
        self.assertEqual(
            schema["properties"]["protocol_version"],
            {"type": "string", "const": EXTERNAL_AGENT_PROTOCOL_VERSION},
        )
        self.assertIn("anchor_accounting", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        candidate_properties = schema["properties"]["candidates"]["items"][
            "properties"
        ]
        self.assertNotIn("evidence_unit_ids", candidate_properties)
        self.assertEqual(
            candidate_properties["anchor_unit_ids"]["items"]["enum"],
            [self.unit().unit_id],
        )
        self.assertEqual(
            provider.execution_records[0].strict_validation_status, "passed"
        )
        self.assertIsNone(provider.execution_records[0].contract_failure_detail)
        command = runner.calls[0]
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--strict-config", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-terra")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="medium"',
        )
        record = provider.execution_records[0]
        self.assertEqual(record.model, "gpt-5.6-terra")
        self.assertEqual(record.reasoning_effort, "medium")
        self.assertEqual(record.fallback_policy, "none")

    def test_v1_request_fingerprint_and_schema_remain_exactly_readable(self) -> None:
        request, fingerprint = _external_agent_request(
            RepresentationAnalysisBatch((self.unit(),)),
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V1,
        )
        self.assertEqual(
            fingerprint,
            "sha256:5d2fd27e78f30ac37577df3216926cf770819c09d97b0320e1baac51c97cac91",
        )
        self.assertNotIn("anchor_accounting", request)
        schema = external_agent_representation_analysis_schema(
            EXTERNAL_AGENT_PROTOCOL_V1
        )
        self.assertNotIn("anchor_accounting", schema["properties"])
        self.assertEqual(
            schema["properties"]["protocol_version"],
            {"type": "string", "const": EXTERNAL_AGENT_PROTOCOL_V1},
        )

    def test_v2_request_fingerprint_and_schema_remain_exactly_readable(self) -> None:
        request, fingerprint = _external_agent_request(
            RepresentationAnalysisBatch((self.unit(),)),
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V2,
        )
        self.assertEqual(
            fingerprint,
            "sha256:67ac01137cd702dc561a544920c883bece975eca54c643f0fdcb6287a2b588a1",
        )
        self.assertIn("anchor_accounting", request["rules"][2])
        schema = external_agent_representation_analysis_schema(
            EXTERNAL_AGENT_PROTOCOL_V2
        )
        candidate_properties = schema["properties"]["candidates"]["items"][
            "properties"
        ]
        self.assertIn("evidence_unit_ids", candidate_properties)
        self.assertNotIn("anchor_unit_ids", candidate_properties)
        self.assertIn("anchor_accounting", schema["properties"])

    def test_v3_request_fingerprint_remains_exactly_readable(self) -> None:
        request, fingerprint = _external_agent_request(
            RepresentationAnalysisBatch((self.unit(),)),
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3,
        )
        self.assertEqual(
            fingerprint,
            "sha256:9bca6a7775cea3f69aec21075bb43acdb729d7074ff672a3be15051fd005d43b",
        )
        self.assertNotIn("result_schema_fingerprint", request)
        schema = external_agent_representation_analysis_schema(
            EXTERNAL_AGENT_PROTOCOL_V3,
            batch=RepresentationAnalysisBatch((self.unit(),)),
        )
        accounting = schema["properties"]["anchor_accounting"]
        self.assertEqual(accounting["minItems"], 1)
        self.assertEqual(accounting["maxItems"], 1)
        candidate = schema["properties"]["candidates"]["items"]["properties"]
        self.assertNotIn("maxItems", candidate["anchor_unit_ids"])

    def test_v31_fingerprint_binds_the_exact_dynamic_schema(self) -> None:
        batch = RepresentationAnalysisBatch(self.units(40), self.units(3, start=101))
        schema = external_agent_representation_analysis_schema(
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            batch=batch,
        )
        request, fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
            result_schema=schema,
        )
        self.assertEqual(
            request["result_schema_fingerprint"],
            _canonical_fingerprint(schema),
        )
        changed = json.loads(json.dumps(schema))
        changed["properties"]["anchor_accounting"]["maxItems"] = 39
        changed_request, changed_fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
            result_schema=changed,
        )
        self.assertNotEqual(fingerprint, changed_fingerprint)
        self.assertNotEqual(
            request["result_schema_fingerprint"],
            changed_request["result_schema_fingerprint"],
        )

    def test_v31_schema_exactly_bounds_a_40_anchor_batch(self) -> None:
        batch = RepresentationAnalysisBatch(self.units(40), self.units(3, start=101))
        schema = external_agent_representation_analysis_schema(
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            batch=batch,
        )
        accounting = schema["properties"]["anchor_accounting"]
        self.assertEqual(accounting["minItems"], 40)
        self.assertEqual(accounting["maxItems"], 40)
        candidate = schema["properties"]["candidates"]["items"]["properties"]
        residue = schema["properties"]["residue"]["items"]["properties"]
        self.assertEqual(candidate["anchor_unit_ids"]["maxItems"], 40)
        self.assertEqual(residue["anchor_unit_ids"]["maxItems"], 40)
        self.assertEqual(candidate["supporting_evidence_unit_ids"]["maxItems"], 3)

    def test_v32_schema_uses_an_exact_accounting_map_for_1_and_40_anchors(
        self,
    ) -> None:
        for count in (1, 40):
            with self.subTest(count=count):
                batch = RepresentationAnalysisBatch(
                    self.units(count), self.units(3, start=101)
                )
                schema = external_agent_representation_analysis_schema(
                    EXTERNAL_AGENT_PROTOCOL_V3_2,
                    batch=batch,
                )
                accounting = schema["properties"]["anchor_accounting"]
                anchor_ids = [unit.unit_id for unit in batch.anchor_units]
                self.assertEqual(accounting["type"], "object")
                self.assertEqual(list(accounting["properties"]), anchor_ids)
                self.assertEqual(accounting["required"], anchor_ids)
                self.assertFalse(accounting["additionalProperties"])
                self.assertTrue(
                    all(
                        value == {
                            "type": "string",
                            "enum": ["candidate", "residue"],
                        }
                        for value in accounting["properties"].values()
                    )
                )
                self.assertNotIn("uniqueItems", json.dumps(schema))

    def test_v32_fingerprint_binds_the_exact_accounting_map_schema(self) -> None:
        batch = RepresentationAnalysisBatch(self.units(40))
        schema = external_agent_representation_analysis_schema(
            EXTERNAL_AGENT_PROTOCOL_V3_2,
            batch=batch,
        )
        request, fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2,
            result_schema=schema,
        )
        self.assertEqual(
            request["result_schema_fingerprint"],
            _canonical_fingerprint(schema),
        )
        changed = json.loads(json.dumps(schema))
        changed["properties"]["anchor_accounting"]["required"] = [
            *changed["properties"]["anchor_accounting"]["required"][:-1]
        ]
        changed_request, changed_fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2,
            result_schema=changed,
        )
        self.assertNotEqual(fingerprint, changed_fingerprint)
        self.assertNotEqual(
            request["result_schema_fingerprint"],
            changed_request["result_schema_fingerprint"],
        )

    def test_v32_accepts_40_anchors_all_as_candidates(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner(),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(len(result.candidates), 40)
        self.assertEqual(result.residue, ())
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v32_accepts_40_anchors_all_as_residue(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("all_residue"),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.residue), 40)
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v32_accepts_a_mixed_40_anchor_assignment(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("mixed"),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(len(result.candidates), 20)
        self.assertEqual(len(result.residue), 20)
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v32_allows_one_anchor_to_support_multiple_candidates(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("same_anchor_multiple_candidates"),
        )
        result = provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.residue, ())
        self.assertEqual(provider.execution_records[0].covered_units, 1)

    def test_v32_rejects_accounting_map_attacks(self) -> None:
        cases = {
            "accounting_missing_anchor": "anchor_coverage",
            "accounting_extra_anchor": "anchor_coverage",
            "accounting_unknown_anchor": "anchor_coverage",
            "accounting_wrong_outcome": "anchor_accounting",
            "accounting_wrong_enum": "anchor_accounting",
            "accounting_duplicate_key": "anchor_accounting",
            "dual_assignment": "anchor_accounting",
            "anchor_uncovered": "anchor_coverage",
        }
        for mode, detail in cases.items():
            with self.subTest(mode=mode):
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner(mode)
                )
                with self.assertRaisesRegex(Exception, "未产生可验证"):
                    provider.analyze(
                        RepresentationAnalysisBatch((self.unit(),))
                    )
                record = provider.execution_records[0]
                self.assertEqual(record.failure_category, "result_contract_failure")
                self.assertEqual(record.contract_failure_detail, detail)
                if mode == "accounting_duplicate_key":
                    self.assertEqual(record.duplicate_accounting_count, 1)
                    self.assertEqual(
                        record.contract_failure_stage,
                        "accounting_cross_check",
                    )
                    self.assertRegex(
                        record.result_fingerprint or "",
                        r"^sha256:[0-9a-f]{64}$",
                    )

    def test_v32_rejects_context_only_accounting(self) -> None:
        context = replace(self.unit(), unit_id="unit_" + "e" * 64)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner("accounting_context_only")
        )
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(
                RepresentationAnalysisBatch((self.unit(),), (context,))
            )
        self.assertEqual(
            provider.execution_records[0].contract_failure_detail,
            "anchor_coverage",
        )

    def test_v32_rejects_duplicate_or_colliding_batch_identity_before_call(
        self,
    ) -> None:
        for batch in (
            RepresentationAnalysisBatch((self.unit(), self.unit())),
            RepresentationAnalysisBatch((self.unit(),), (self.unit(),)),
        ):
            with self.subTest(batch=batch), self.assertRaisesRegex(
                ValueError, "batch identity"
            ):
                _external_agent_request(
                    batch, protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2
                )

    def test_literal_v31_array_result_remains_strictly_readable(self) -> None:
        batch = RepresentationAnalysisBatch((self.unit(),))
        _, fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
        )
        payload = {
            "protocol_version": EXTERNAL_AGENT_PROTOCOL_V3_1,
            "input_fingerprint": fingerprint,
            "anchor_accounting": [
                {
                    "anchor_unit_id": self.unit().unit_id,
                    "accounted_as": "candidate",
                }
            ],
            "candidates": [
                {
                    "statement": "Synthetic statement.",
                    "semantic_type": "observation",
                    "concerns": ["Synthetic"],
                    "anchor_unit_ids": [self.unit().unit_id],
                    "supporting_evidence_unit_ids": [],
                    "context": "Synthetic context.",
                    "confidence": 0.9,
                }
            ],
            "residue": [],
        }
        result = _parse_external_agent_result(
            json.dumps(payload),
            batch,
            fingerprint,
            expected_protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
        )
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.residue, ())

    def test_v32_contract_failures_publish_no_package_or_atomic_information(
        self,
    ) -> None:
        for mode in (
            "accounting_missing_anchor",
            "accounting_extra_anchor",
            "accounting_wrong_enum",
            "accounting_duplicate_key",
            "dual_assignment",
            "anchor_uncovered",
        ):
            with self.subTest(mode=mode):
                root = self.root / mode
                representation, service = self.build_service(root=root)
                atomic_path = root / "atomic.jsonl"
                with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
                    ExternalAgentSemanticHandoffService(
                        service,
                        JsonlAtomicInformationStore(atomic_path),
                        root / "audits",
                    ).execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0",
                            runner=FakeRunner(mode),
                        ),
                    )
                self.assertFalse(
                    (root / "information" / representation.representation_id).exists()
                )
                self.assertFalse(atomic_path.exists())

    def test_v32_records_content_free_40_anchor_coverage_diagnostics(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("coverage_summary"),
            diagnostic_root=self.root.resolve() / "diagnostics",
        )
        batch = RepresentationAnalysisBatch(self.units(40))
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(batch)
        record = provider.execution_records[0]
        self.assertEqual(record.contract_failure_detail, "anchor_coverage")
        self.assertEqual(record.contract_failure_stage, "coverage")
        self.assertEqual(record.covered_units, 35)
        self.assertEqual(record.candidate_item_count, 21)
        self.assertEqual(record.residue_item_count, 16)
        self.assertEqual(record.accounting_item_count, 39)
        self.assertEqual(record.candidate_anchor_ref_count, 21)
        self.assertEqual(record.residue_anchor_ref_count, 16)
        self.assertEqual(record.duplicate_anchor_ref_count, 1)
        self.assertEqual(record.duplicate_accounting_count, 0)
        self.assertEqual(record.dual_assignment_count, 1)
        self.assertEqual(record.missing_anchor_count, 5)
        self.assertRegex(record.result_fingerprint or "", r"^sha256:[0-9a-f]{64}$")
        metadata_path = (
            provider.diagnostic_root / record.processing_run_id / "metadata.json"
        )
        metadata_text = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_text)
        self.assertEqual(
            metadata["diagnostic_schema_version"],
            "external-agent-diagnostics/2.0",
        )
        self.assertEqual(metadata["covered_units"], 35)
        self.assertEqual(metadata["missing_anchor_count"], 5)
        self.assertNotIn("anchor_unit_ids", metadata)
        self.assertNotIn(batch.anchor_units[0].unit_id, metadata_text)
        self.assertNotIn(batch.anchor_units[0].content, metadata_text)

    def test_v3_schema_binds_anchor_and_evidence_context_enums(self) -> None:
        evidence_context = replace(
            self.unit(), unit_id="unit_" + "e" * 64
        )
        non_evidence_context = replace(
            self.unit(),
            unit_id="unit_" + "f" * 64,
            analysis_eligible=False,
            exclusion_reason="synthetic context only",
        )
        schema = external_agent_representation_analysis_schema(
            batch=RepresentationAnalysisBatch(
                (self.unit(),), (evidence_context, non_evidence_context)
            )
        )
        candidate = schema["properties"]["candidates"]["items"]["properties"]
        self.assertEqual(
            candidate["anchor_unit_ids"]["items"]["enum"],
            [self.unit().unit_id],
        )
        self.assertEqual(
            candidate["supporting_evidence_unit_ids"]["items"]["enum"],
            [evidence_context.unit_id],
        )
        residue = schema["properties"]["residue"]["items"]["properties"]
        self.assertEqual(
            residue["anchor_unit_ids"]["items"]["enum"],
            [self.unit().unit_id],
        )
        for references in (
            candidate["anchor_unit_ids"],
            candidate["supporting_evidence_unit_ids"],
            residue["anchor_unit_ids"],
        ):
            self.assertNotIn("uniqueItems", references)

    def test_v3_rejects_invalid_anchor_and_context_accounting(self) -> None:
        evidence_context = replace(
            self.unit(), unit_id="unit_" + "e" * 64
        )
        non_evidence_context = replace(
            self.unit(),
            unit_id="unit_" + "f" * 64,
            analysis_eligible=False,
            exclusion_reason="synthetic context only",
        )
        cases = {
            "candidate_no_anchor": "candidate_schema",
            "candidate_duplicate_anchor": "candidate_schema",
            "candidate_context_as_anchor": "evidence_reference",
            "candidate_anchor_as_support": "evidence_reference",
            "residue_context_reference": "evidence_reference",
        }
        for mode, detail in cases.items():
            with self.subTest(mode=mode):
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner(mode)
                )
                with self.assertRaisesRegex(Exception, "未产生可验证"):
                    provider.analyze(
                        RepresentationAnalysisBatch(
                            (self.unit(),), (evidence_context,)
                        )
                    )
                self.assertEqual(
                    provider.execution_records[0].contract_failure_detail,
                    detail,
                )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("candidate_non_evidence_context"),
        )
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(
                RepresentationAnalysisBatch(
                    (self.unit(),), (non_evidence_context,)
                )
            )
        self.assertEqual(
            provider.execution_records[0].contract_failure_detail,
            "evidence_reference",
        )

    def test_v3_maps_valid_supporting_context_into_canonical_evidence(self) -> None:
        evidence_context = replace(self.unit(), unit_id="unit_" + "e" * 64)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("candidate_evidence_context"),
        )
        result = provider.analyze(
            RepresentationAnalysisBatch((self.unit(),), (evidence_context,))
        )
        self.assertEqual(
            result.candidates[0].evidence_unit_ids,
            (self.unit().unit_id, evidence_context.unit_id),
        )

    def test_provider_may_account_every_anchor_as_residue(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner("all_residue")
        )
        result = provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        self.assertEqual(result.candidates, ())
        self.assertEqual(
            result.residue[0].evidence_unit_ids, (self.unit().unit_id,)
        )
        self.assertEqual(
            provider.execution_records[0].strict_validation_status, "passed"
        )

    def test_context_support_accounting_cannot_replace_the_anchor(self) -> None:
        context = replace(self.unit(), unit_id="unit_" + "e" * 64)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("accounting_context_only"),
        )
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(
                RepresentationAnalysisBatch((self.unit(),), (context,))
            )
        record = provider.execution_records[0]
        self.assertEqual(record.failure_category, "result_contract_failure")
        self.assertEqual(record.contract_failure_detail, "anchor_coverage")

    def test_explicit_execution_profile_override_is_bound_to_command(self) -> None:
        runner = FakeRunner()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            runner=runner,
        )
        provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        command = runner.calls[0]
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="high"',
        )
        self.assertEqual(provider.execution_records[0].reasoning_effort, "high")

    def test_invalid_execution_profile_fails_before_provider_call(self) -> None:
        runner = FakeRunner()
        for arguments in (
            {"model": "unsafe model"},
            {"reasoning_effort": "unsupported"},
            {"fallback_policy": "automatic"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=runner, **arguments
                )
        self.assertEqual(runner.calls, [])

    def test_historical_request_fingerprints_remain_exactly_readable(self) -> None:
        expected = {
            EXTERNAL_AGENT_PROTOCOL_V1: (
                "sha256:5d2fd27e78f30ac37577df3216926cf770819c09d97b0320e1baac51c97cac91"
            ),
            EXTERNAL_AGENT_PROTOCOL_V2: (
                "sha256:67ac01137cd702dc561a544920c883bece975eca54c643f0fdcb6287a2b588a1"
            ),
            EXTERNAL_AGENT_PROTOCOL_V3: (
                "sha256:9bca6a7775cea3f69aec21075bb43acdb729d7074ff672a3be15051fd005d43b"
            ),
            EXTERNAL_AGENT_PROTOCOL_V3_1: (
                "sha256:5afea47585cd10c55f0d60ca0a8ffa2c4d0c3c72e3fc15e6c332e8e66e27c187"
            ),
            EXTERNAL_AGENT_PROTOCOL_V3_2: (
                "sha256:1014f3b4e797203817ec9b68b13b2509b2aa6f4679b6175ce60ff0203b146384"
            ),
        }
        batch = RepresentationAnalysisBatch((self.unit(),))
        for protocol_version, fingerprint in expected.items():
            with self.subTest(protocol=protocol_version):
                self.assertEqual(
                    _external_agent_request(
                        batch, protocol_version=protocol_version
                    )[1],
                    fingerprint,
                )

    def test_current_v32_producer_passes_the_shared_audit_validator(self) -> None:
        representation, service = self.build_service()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        audit_root = self.root / "audits"
        result = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).execute(representation.representation_id, provider)
        manifest, _ = validate_representation_information_package(
            result.package
        )
        self.assertEqual(
            manifest["provider"],
            {
                "name": provider.name,
                "provider_version": provider.provider_version,
                "model": provider.model,
                "reasoning_effort": provider.reasoning_effort,
                "fallback_policy": provider.fallback_policy,
            },
        )
        self.assertEqual(
            validate_completed_published_audits(
                representation_service=service,
                representation_id=representation.representation_id,
                manifest=manifest,
                audit_root=audit_root,
                package_fingerprint=_package_fingerprint(result.package),
                provider=provider,
            ),
            result.audit_paths,
        )

    def test_contract_failure_details_are_allowlisted(self) -> None:
        cases = {
            "top_level_missing": "top_level_schema",
            "top_level_extra": "top_level_schema",
            "candidate_shape": "candidate_schema",
            "candidate_semantic": "candidate_schema",
            "candidate_confidence": "candidate_schema",
            "residue_shape": "residue_schema",
            "candidate_unknown_reference": "evidence_reference",
            "residue_unknown_reference": "evidence_reference",
            "anchor_uncovered": "anchor_coverage",
            "accounting_missing_anchor": "anchor_coverage",
            "accounting_unknown_anchor": "anchor_coverage",
            "accounting_wrong_outcome": "anchor_accounting",
            "accounting_context_only": "anchor_coverage",
        }
        for mode, detail in cases.items():
            with self.subTest(mode=mode):
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner(mode)
                )
                with self.assertRaisesRegex(Exception, "未产生可验证"):
                    provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
                record = provider.execution_records[0]
                self.assertEqual(record.failure_category, "result_contract_failure")
                self.assertEqual(record.contract_failure_detail, detail)

    def test_unmapped_contract_validation_error_is_recorded_as_unknown(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        with (
            patch.object(
                RepresentationInformationService,
                "_validate_batch_result",
                side_effect=RepresentationInformationError("synthetic internal detail"),
            ),
            self.assertRaisesRegex(Exception, "未产生可验证"),
        ):
            provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        record = provider.execution_records[0]
        self.assertEqual(record.failure_category, "result_contract_failure")
        self.assertEqual(record.contract_failure_detail, "unknown")

    def test_non_contract_failures_do_not_receive_contract_detail(self) -> None:
        for mode, category in (
            ("invalid_json", "invalid_json"),
            ("wrong_binding", "result_binding_failure"),
            ("timeout", "timeout"),
            ("nonzero", "runtime_nonzero_exit"),
            ("no_result", "no_result"),
        ):
            with self.subTest(mode=mode):
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner(mode)
                )
                with self.assertRaisesRegex(Exception, "未产生可验证"):
                    provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
                record = provider.execution_records[0]
                self.assertEqual(record.failure_category, category)
                self.assertIsNone(record.contract_failure_detail)

    def test_failure_is_fail_closed_and_does_not_echo_input(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner("nonzero")
        )
        with self.assertRaisesRegex(Exception, "未产生可验证") as error:
            provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        self.assertNotIn(self.unit().content, str(error.exception))
        self.assertEqual(
            provider.execution_records[0].failure_category, "runtime_nonzero_exit"
        )

    def test_wrong_protocol_binding_is_fail_closed_before_package_creation(
        self,
    ) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner("wrong_binding")
        )
        with self.assertRaisesRegex(Exception, "未产生可验证"):
            provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        self.assertEqual(
            provider.execution_records[0].failure_category, "result_binding_failure"
        )

    def test_nonzero_parent_with_live_group_is_terminated_before_failure(self) -> None:
        from archeos import representation_information

        class NonzeroProcess:
            pid = 12345
            returncode = 7

            def communicate(self, **_kwargs):
                return "", "synthetic nonzero"

        signals: list[int] = []

        def kill_group(_pid: int, signal: int) -> None:
            signals.append(signal)
            if signal == 0 and signals.count(0) > 1:
                raise ProcessLookupError

        with patch.object(representation_information.os, "killpg", kill_group):
            outcome = representation_information._run_external_agent_once(
                ["synthetic"],
                "synthetic",
                1,
                lambda *_args, **_kwargs: NonzeroProcess(),
            )
        self.assertEqual(outcome.failure_category, "runtime_nonzero_exit")
        self.assertIn(representation_information.signal.SIGTERM, signals)

    def build_service(self, *, blocks: int = 1, root: Path | None = None):
        root = self.root if root is None else root
        root.mkdir(parents=True, exist_ok=True)
        external = root / "synthetic.txt"
        external.write_text("synthetic", encoding="utf-8")
        sources = LocalManagedSourceRepository(
            root / "managed",
            id_factory=lambda: "src_" + "1" * 32,
            clock=lambda: "2026-08-15T00:00:00.000Z",
        )
        source = sources.admit(
            external, metadata={"media_type": "application/synthetic"}
        ).source
        representations = LocalRepresentationRepository(root / "representations")
        representation = (
            RepresentationService(sources, representations)
            .build(source.source_id, JsonAdapter(blocks))
            .representation
        )
        service = RepresentationInformationService(
            sources,
            representations,
            root / "information",
            clock=lambda: "2026-08-15T00:00:00.000Z",
        )
        return representation, service

    def privacy_binding(self) -> SemanticPrivacyBinding:
        return SemanticPrivacyBinding(
            policy="synthetic-local-privacy-gate",
            policy_version="1.0",
            route="approved",
            receipt_fingerprint="sha256:" + "9" * 64,
        )

    @staticmethod
    def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
        if not os.path.lexists(root):
            return ()
        paths = [root, *sorted(root.rglob("*"))]
        snapshot: list[tuple[object, ...]] = []
        for path in paths:
            metadata = path.lstat()
            relative = "." if path == root else path.relative_to(root).as_posix()
            if path.is_symlink():
                value: object = ("symlink", os.readlink(path))
            elif path.is_file():
                value = ("file", path.read_bytes())
            else:
                value = ("directory",)
            snapshot.append((relative, stat.S_IMODE(metadata.st_mode), value))
        return tuple(snapshot)

    def build_wechat_service(self):
        root = self.root / "wechat"
        source_path = root / "private.json"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            json.dumps(
                {
                    "chat": "Synthetic Chat",
                    "username": "wxid_synthetic",
                    "is_group": False,
                    "count": 2,
                    "offset": 0,
                    "limit": 50,
                    "start_time": None,
                    "end_time": None,
                    "type": None,
                    "messages": [
                        "[2026-08-15 09:00] Sender_A: Synthetic message one.",
                        "[2026-08-15 09:01] Sender_B: Synthetic message two.",
                    ],
                    "failures": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sources = LocalManagedSourceRepository(
            root / "01_inbox", id_factory=lambda: "src_" + "e" * 32
        )
        source = sources.admit(
            source_path, metadata={"media_type": "application/json"}
        ).source
        representations = LocalRepresentationRepository(root / "representations")
        representation = RepresentationService(sources, representations).build(
            source.source_id, WechatConversationRepresentationAdapter(), {}
        ).representation
        return representation, RepresentationInformationService(
            sources, representations, root / "information"
        )

    def test_handoff_writes_auditable_package_and_idempotent_store_replay(self) -> None:
        representation, service = self.build_service()
        runner = FakeRunner()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=runner
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            self.root / "audits",
        )
        first = handoff.execute(representation.representation_id, provider)
        self.assertEqual(first.ingestion.created, 1)
        self.assertFalse(first.replayed_existing_package)
        audit = json.loads(first.audit_paths[0].read_text())
        self.assertEqual(audit["execution_status"], "succeeded")
        self.assertIsNone(audit["contract_failure_detail"])
        self.assertTrue(audit["package_published"])
        self.assertTrue(audit["information_ingested"])
        self.assertEqual(audit["durable_ingestion_status"], "completed")
        self.assertEqual(audit["unaccounted_units"], 0)
        self.assertEqual(audit["audit_readback_status"], "verified")
        self.assertEqual(audit["model"], "gpt-5.6-terra")
        self.assertEqual(audit["reasoning_effort"], "medium")
        self.assertEqual(audit["fallback_policy"], "none")
        self.assertNotIn("chain_of_thought", audit)
        self.assertNotIn("reasoning_content", audit)
        manifest = json.loads((first.package / "manifest.json").read_text())
        self.assertEqual(
            manifest["provider"],
            {
                "name": "external-agent-codex-cli",
                "provider_version": "0.147.0",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "fallback_policy": "none",
            },
        )
        self.assertNotIn("Synthetic business input.", first.audit_paths[0].read_text())
        replay_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        second = handoff.execute(representation.representation_id, replay_provider)
        self.assertTrue(second.replayed_existing_package)
        self.assertEqual(second.ingestion.existing, 1)
        self.assertEqual(replay_provider.execution_records, [])

        changed_runner = FakeRunner()
        changed_profile = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            reasoning_effort="high",
            runner=changed_runner,
        )
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            handoff.execute(representation.representation_id, changed_profile)
        self.assertEqual(changed_runner.calls, [])

    def test_v1_package_and_audit_replay_without_a_provider_call(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        first = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        units = _units_from_representation(
            representation, service.representation_repository
        )
        batch = _analysis_batches(units, service.batch_size)[0]
        _, v1_fingerprint = _external_agent_request(
            batch, protocol_version=EXTERNAL_AGENT_PROTOCOL_V1
        )
        audit_path = first.audit_paths[0]
        historical = json.loads(audit_path.read_text(encoding="utf-8"))
        historical["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V1
        historical["input_fingerprint"] = v1_fingerprint
        historical["diagnostic_schema_version"] = "external-agent-diagnostics/1.0"
        for field in _CONTRACT_DIAGNOSTIC_FIELDS:
            historical.pop(field)
        audit_path.write_text(json.dumps(historical), encoding="utf-8")

        replay_runner = FakeRunner()
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                reasoning_effort="high",
                runner=replay_runner,
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 1)
        self.assertEqual(replay_runner.calls, [])

    def test_v2_package_and_audit_replay_without_a_provider_call(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        first = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        units = _units_from_representation(
            representation, service.representation_repository
        )
        batch = _analysis_batches(units, service.batch_size)[0]
        _, v2_fingerprint = _external_agent_request(
            batch, protocol_version=EXTERNAL_AGENT_PROTOCOL_V2
        )
        audit_path = first.audit_paths[0]
        historical = json.loads(audit_path.read_text(encoding="utf-8"))
        historical["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V2
        historical["input_fingerprint"] = v2_fingerprint
        historical["diagnostic_schema_version"] = "external-agent-diagnostics/1.0"
        for field in _CONTRACT_DIAGNOSTIC_FIELDS:
            historical.pop(field)
        audit_path.write_text(json.dumps(historical), encoding="utf-8")

        replay_runner = FakeRunner()
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                reasoning_effort="high",
                runner=replay_runner,
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 1)
        self.assertEqual(replay_runner.calls, [])

    def test_replay_rejects_profiled_v2_prediagnostics_mixed_audit(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        units = _units_from_representation(
            representation, service.representation_repository
        )
        batch = _analysis_batches(units, service.batch_size)[0]
        _, v2_fingerprint = _external_agent_request(
            batch, protocol_version=EXTERNAL_AGENT_PROTOCOL_V2
        )
        audit_path = next(audit_root.glob("*/processing-run-audit.json"))
        impossible = json.loads(audit_path.read_text(encoding="utf-8"))
        impossible["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V2
        impossible["input_fingerprint"] = v2_fingerprint
        for field in (
            "contract_failure_detail",
            *_AUDIT_DIAGNOSTIC_FIELDS,
            *_CONTRACT_DIAGNOSTIC_FIELDS,
        ):
            impossible.pop(field)
        audit_path.write_text(json.dumps(impossible), encoding="utf-8")

        replay_runner = FakeRunner()
        replay_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=replay_runner
        )
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            handoff.execute(representation.representation_id, replay_provider)
        self.assertEqual(replay_runner.calls, [])
        self.assertEqual(replay_provider.execution_records, [])

    def test_v3_package_and_audit_replay_without_a_provider_call(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        first = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                runner=FakeRunner(),
            ),
        )
        units = _units_from_representation(
            representation,
            service.representation_repository,
        )
        batch = _analysis_batches(units, service.batch_size)[0]
        _, v3_fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3,
        )
        audit_path = first.audit_paths[0]
        historical = json.loads(audit_path.read_text(encoding="utf-8"))
        historical["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V3
        historical["input_fingerprint"] = v3_fingerprint
        historical["diagnostic_schema_version"] = "external-agent-diagnostics/1.0"
        for field in _CONTRACT_DIAGNOSTIC_FIELDS:
            historical.pop(field)
        audit_path.write_text(json.dumps(historical), encoding="utf-8")

        replay_runner = FakeRunner()
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                runner=replay_runner,
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 1)
        self.assertEqual(replay_runner.calls, [])

    def test_v31_package_and_audit_replay_without_a_provider_call(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        first = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                runner=FakeRunner(),
            ),
        )
        units = _units_from_representation(
            representation,
            service.representation_repository,
        )
        batch = _analysis_batches(units, service.batch_size)[0]
        _, v31_fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
        )
        audit_path = first.audit_paths[0]
        historical = json.loads(audit_path.read_text(encoding="utf-8"))
        historical["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V3_1
        historical["input_fingerprint"] = v31_fingerprint
        audit_path.write_text(json.dumps(historical), encoding="utf-8")

        replay_runner = FakeRunner()
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                runner=replay_runner,
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 1)
        self.assertEqual(replay_runner.calls, [])

    def test_replay_accepts_literal_name_only_prediagnostics_v1_package(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        package = service.output_root / representation.representation_id
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provider"] = {"name": "external-agent-codex-cli"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        package_fingerprint = _package_fingerprint(package)
        audit_path = next(audit_root.glob("*/processing-run-audit.json"))
        legacy = json.loads(audit_path.read_text(encoding="utf-8"))
        units = _units_from_representation(
            representation, service.representation_repository
        )
        batch = _analysis_batches(units, service.batch_size)[0]
        _, v1_fingerprint = _external_agent_request(
            batch, protocol_version=EXTERNAL_AGENT_PROTOCOL_V1
        )
        legacy["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V1
        legacy["input_fingerprint"] = v1_fingerprint
        legacy["package_fingerprint"] = package_fingerprint
        for field in (
            "contract_failure_detail",
            "model",
            "reasoning_effort",
            "fallback_policy",
            "diagnostic_schema_version",
            "elapsed_ms",
            "deadline_ms",
            "exit_code",
            "termination_signal",
            "timeout_phase",
            "provider_error_category",
            "result_file_present",
            "result_size_bytes",
            "stdout_bytes",
            "stderr_bytes",
            "process_cleanup_status",
            *_CONTRACT_DIAGNOSTIC_FIELDS,
        ):
            legacy.pop(field)
        audit_path.write_text(json.dumps(legacy), encoding="utf-8")
        replay_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        replay = handoff.execute(
            representation.representation_id,
            replay_provider,
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 1)
        self.assertEqual(replay_provider.execution_records, [])

        changed_runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.148.0", runner=changed_runner
                ),
            )
        self.assertEqual(changed_runner.calls, [])

    def test_wechat_representation_uses_the_production_handoff_and_exact_replay(self) -> None:
        representation, service = self.build_wechat_service()
        store_path = self.root / "wechat" / "atomic.jsonl"
        audit_root = self.root / "wechat" / "audits"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )

        first = handoff.execute(representation.representation_id, provider)
        self.assertEqual(first.ingestion.created, 2)
        self.assertFalse(first.replayed_existing_package)
        self.assertEqual(len(first.audit_paths), 1)
        audit = json.loads(first.audit_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(audit["eligible_units"], 2)
        self.assertEqual(audit["covered_units"], 2)
        self.assertEqual(audit["unaccounted_units"], 0)
        self.assertEqual(audit["durable_ingestion_status"], "completed")
        self.assertEqual(audit["handoff_status"], "completed")
        manifest = json.loads((first.package / "manifest.json").read_text())
        self.assertEqual(manifest["representation"]["kind"], "wechat_conversation")
        self.assertEqual(manifest["downstream"]["world_model_write"], "not_performed")
        self.assertFalse((self.root / "wechat" / "04_core").exists())

        replay_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        replay = handoff.execute(representation.representation_id, replay_provider)
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 2)
        self.assertEqual(replay_provider.execution_records, [])

    def test_handoff_failure_writes_audit_without_package_or_information(self) -> None:
        representation, service = self.build_service()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner("nonzero")
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            self.root / "audits",
        )
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            handoff.execute(representation.representation_id, provider)
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())
        audits = list((self.root / "audits").glob("*/processing-run-audit.json"))
        self.assertEqual(len(audits), 1)
        self.assertEqual(
            json.loads(audits[0].read_text())["execution_status"], "failed"
        )
        audit_text = audits[0].read_text(encoding="utf-8")
        audit = json.loads(audit_text)
        self.assertNotIn("synthetic nonzero", audit_text)
        self.assertEqual(len(provider.runner.calls), 1)
        self.assertEqual(audit["model"], "gpt-5.6-terra")
        self.assertEqual(audit["reasoning_effort"], "medium")
        self.assertEqual(audit["fallback_policy"], "none")
        self.assertEqual(audit["diagnostic_schema_version"], "external-agent-diagnostics/2.0")
        self.assertGreaterEqual(audit["stdout_bytes"], 0)
        self.assertGreater(audit["stderr_bytes"], 0)
        self.assertEqual(audit["provider_error_category"], "unknown")
        self.assertNotIn("diagnostic_cleanup_status", audit)
        self.assertNotIn("stdout", set(audit) - {"stdout_bytes"})
        self.assertNotIn("stderr", set(audit) - {"stderr_bytes"})

    def test_contract_failure_audit_is_read_back_without_internal_error_text(self) -> None:
        representation, service = self.build_service()
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            self.root / "audits",
        )
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner("candidate_shape")
                ),
            )
        audit_path = next((self.root / "audits").glob("*/processing-run-audit.json"))
        audit_text = audit_path.read_text(encoding="utf-8")
        audit = json.loads(audit_text)
        self.assertEqual(audit["failure_category"], "result_contract_failure")
        self.assertEqual(audit["contract_failure_detail"], "candidate_schema")
        self.assertFalse(audit["package_published"])
        self.assertFalse(audit["information_ingested"])
        self.assertNotIn("candidate does not match", audit_text)

    def test_partial_coverage_audit_preserves_only_content_free_counts(self) -> None:
        representation, service = self.build_service(blocks=40)
        audit_root = self.root / "audits"
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                audit_root,
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    runner=FakeRunner("coverage_summary"),
                    diagnostic_root=self.root.resolve() / "diagnostics",
                ),
            )
        audit_path = next(audit_root.glob("*/processing-run-audit.json"))
        audit_text = audit_path.read_text(encoding="utf-8")
        audit = json.loads(audit_text)
        self.assertEqual(audit["contract_failure_stage"], "coverage")
        self.assertEqual(audit["eligible_units"], 40)
        self.assertEqual(audit["covered_units"], 35)
        self.assertEqual(audit["unaccounted_units"], 5)
        self.assertEqual(audit["duplicate_anchor_ref_count"], 1)
        self.assertEqual(audit["duplicate_accounting_count"], 0)
        self.assertEqual(audit["dual_assignment_count"], 1)
        self.assertEqual(audit["missing_anchor_count"], 5)
        self.assertRegex(audit["result_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("Synthetic business input", audit_text)

    def test_replay_rechecks_managed_source_before_store_write(self) -> None:
        representation, service = self.build_service()
        store_path = self.root / "atomic.jsonl"
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), self.root / "audits"
        )
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        original = store_path.read_text(encoding="utf-8")
        managed = (
            self.root
            / "managed"
            / "sources"
            / representation.source_id
            / "original.txt"
        )
        managed.write_text("mutated synthetic source", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
            )
        self.assertEqual(store_path.read_text(encoding="utf-8"), original)

    def test_pending_audit_is_completed_by_exact_replay_after_finalize_failure(
        self,
    ) -> None:
        representation, service = self.build_service()
        store_path = self.root / "atomic.jsonl"
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )
        import archeos.semantic_handoff as handoff_module

        original_write = handoff_module._private_json_write
        failed = False

        def fail_final_write(path, payload):
            nonlocal failed
            if (
                payload.get("durable_ingestion_status") == "completed"
                and payload.get("audit_readback_status") == "verified"
                and not failed
            ):
                failed = True
                raise OSError("synthetic final audit failure")
            original_write(path, payload)

        with (
            patch.object(handoff_module, "_private_json_write", fail_final_write),
            self.assertRaisesRegex(Exception, "审计仍为待完成"),
        ):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
            )
        pending = json.loads(
            next(audit_root.glob("*/processing-run-audit.json")).read_text()
        )
        self.assertEqual(pending["durable_ingestion_status"], "completed")
        self.assertEqual(pending["audit_readback_status"], "pending")
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        completed = json.loads(replay.audit_paths[0].read_text())
        self.assertEqual(completed["durable_ingestion_status"], "completed")
        self.assertEqual(replay.ingestion.existing, 1)

    def test_multibatch_failure_preserves_each_processing_run_result(self) -> None:
        representation, service = self.build_service(blocks=2)
        service.batch_size = 1
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            self.root / "audits",
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=SequenceRunner("valid", "nonzero")
        )
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            handoff.execute(representation.representation_id, provider)
        audits = sorted((self.root / "audits").glob("*/processing-run-audit.json"))
        self.assertEqual(len(audits), 2)
        payloads = [json.loads(path.read_text()) for path in audits]
        successful = next(
            item for item in payloads if item["execution_status"] == "succeeded"
        )
        failed = next(item for item in payloads if item["execution_status"] == "failed")
        self.assertIsNone(successful["failure_category"])
        self.assertEqual(failed["failure_category"], "runtime_nonzero_exit")
        self.assertEqual(successful["handoff_status"], "failed")
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())

    def test_recovery_preflight_does_not_treat_old_success_audit_as_result(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        historical = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=SequenceRunner("valid", "nonzero"),
        )
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            handoff.execute(representation.representation_id, historical)
        self.assertEqual(len(historical.execution_records), 2)

        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.total_batches, 3)
        self.assertEqual(preflight.replayable_batches, 0)
        self.assertEqual(preflight.required_new_calls, 3)
        self.assertEqual(preflight.conservatively_counted_attempts, 0)
        self.assertEqual(provider.execution_records, [])
        self.assertFalse(any(audit_root.glob("semantic_run_*")))

    def write_v31_attempt_fixture(
        self,
        recovery,
        receipt: dict[str, object],
        *,
        nonce: str = "f" * 64,
    ) -> Path:
        for batch in receipt["batches"]:
            batch.pop("batch_contract_fingerprint", None)
            batch["batch_contract_fingerprint"] = _canonical_fingerprint(batch)
        receipt.pop("contract_fingerprint", None)
        receipt.pop("run_receipt_fingerprint", None)
        receipt["contract_fingerprint"] = _canonical_fingerprint(receipt)
        receipt["run_receipt_fingerprint"] = _canonical_fingerprint(receipt)
        batch_receipt = receipt["batches"][0]
        attempt = recovery._expected_attempt_payload(
            semantic_run_id=receipt["semantic_run_id"],
            contract_fingerprint=receipt["contract_fingerprint"],
            batch_receipt=batch_receipt,
            ordinal=1,
            attempt_nonce=nonce,
        )
        legacy_run = recovery.audit_root / receipt["semantic_run_id"]
        legacy_attempts = legacy_run / "attempts"
        legacy_attempts.mkdir(parents=True, mode=0o700)
        os.chmod(legacy_run, 0o700)
        os.chmod(legacy_attempts, 0o700)
        (legacy_run / "run-receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        (legacy_attempts / "batch_0001.json").write_text(
            json.dumps(attempt), encoding="utf-8"
        )
        for path in legacy_run.rglob("*.json"):
            os.chmod(path, 0o600)
        return legacy_run

    def test_v31_attempt_is_counted_preserved_and_isolated_from_v32_run(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(blocks=1)
        audit_root = self.root / "audits"
        audit_root.mkdir(mode=0o700)
        os.chmod(audit_root, 0o700)
        runner = FakeRunner()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", timeout_seconds=300, runner=runner
        )
        recovery = handoff_module._SemanticRecoveryRun(
            service,
            audit_root,
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        receipt = json.loads(
            json.dumps(recovery.expected_historical_v31_run_receipt)
        )
        self.assertEqual(receipt["protocol_version"], EXTERNAL_AGENT_PROTOCOL_V3_1)
        self.assertEqual(receipt["provider_route"], "codex-cli")
        self.assertEqual(
            receipt["provider"],
            {
                "name": "external-agent-codex-cli",
                "provider_version": "0.147.0",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "fallback_policy": "none",
            },
        )
        self.assertEqual(receipt["execution_deadline_ms"], 300000)
        self.assertEqual(receipt["semantic_batch_size"], 40)
        self.assertEqual(
            receipt["local_validator_contract_version"],
            "external-agent-local-validator/3.1",
        )
        self.assertEqual(
            receipt["prompt_template_fingerprint"],
            "sha256:31d354c57fbbe5a4eee17dd8995a2a15e02a5e243b3d9d4d7218455cd55282dd",
        )
        self.assertEqual(
            receipt["batches"][0]["input_fingerprint"],
            "sha256:ec25ae7e9eacbcd2c9eefdb2451b82c319433219ceaed717a7cb9b2b38fc12a4",
        )
        self.assertEqual(
            receipt["batches"][0]["result_schema_fingerprint"],
            "sha256:6235b2a2c192196db4bc856c973649c5a9f8ff2d2777e3b06d4d643fa587d6ab",
        )
        legacy_run = self.write_v31_attempt_fixture(recovery, receipt)
        before = self.tree_snapshot(legacy_run)

        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.historical_counted_attempts, 1)
        self.assertEqual(preflight.conservatively_counted_attempts, 0)
        self.assertEqual(preflight.required_new_calls, 1)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.tree_snapshot(legacy_run), before)

        result = handoff.execute(
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
        current_runs = [
            path for path in audit_root.glob("semantic_run_*") if path != legacy_run
        ]
        self.assertEqual(len(current_runs), 1)
        self.assertEqual(self.tree_snapshot(legacy_run), before)
        self.assertEqual(result.ingestion.created, 1)

    def test_self_consistent_v31_authority_drift_is_never_counted(self) -> None:
        import archeos.semantic_handoff as handoff_module

        attacks = {
            "source": lambda receipt: receipt["source"].update(
                {"content_hash": "sha256:" + "0" * 64}
            ),
            "representation": lambda receipt: receipt[
                "representation"
            ].update({"manifest_fingerprint": "sha256:" + "0" * 64}),
            "privacy": lambda receipt: receipt["privacy"].update(
                {"receipt_fingerprint": "sha256:" + "0" * 64}
            ),
            "route": lambda receipt: receipt.update(
                {"provider_route": "external-agent-codex-cli"}
            ),
            "provider": lambda receipt: receipt["provider"].update(
                {"provider_version": "0.148.0"}
            ),
            "deadline": lambda receipt: receipt.update(
                {"execution_deadline_ms": 301000}
            ),
            "batch_size": lambda receipt: receipt.update(
                {"semantic_batch_size": 20}
            ),
            "anchors": lambda receipt: receipt[
                "ordered_eligible_unit_ids"
            ].reverse(),
            "context": lambda receipt: receipt["batches"][0][
                "context_support_unit_ids"
            ].append("unit_" + "0" * 64),
            "prompt": lambda receipt: receipt.update(
                {"prompt_template_fingerprint": "sha256:" + "0" * 64}
            ),
            "schema": lambda receipt: receipt["batches"][0].update(
                {"result_schema_fingerprint": "sha256:" + "0" * 64}
            ),
            "input": lambda receipt: receipt["batches"][0].update(
                {"input_fingerprint": "sha256:" + "0" * 64}
            ),
            "path": lambda receipt: receipt.update(
                {"semantic_run_id": "semantic_run_" + "0" * 32}
            ),
        }
        for attack, mutate in attacks.items():
            with self.subTest(attack=attack):
                root = self.root / attack
                representation, service = self.build_service(blocks=2, root=root)
                audit_root = root / "audits"
                audit_root.mkdir(mode=0o700)
                os.chmod(audit_root, 0o700)
                runner = FakeRunner()
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                )
                recovery = handoff_module._SemanticRecoveryRun(
                    service,
                    audit_root,
                    representation.representation_id,
                    provider,
                    self.privacy_binding(),
                )
                receipt = json.loads(
                    json.dumps(recovery.expected_historical_v31_run_receipt)
                )
                mutate(receipt)
                self.write_v31_attempt_fixture(recovery, receipt)
                before = self.tree_snapshot(audit_root)

                with self.assertRaises(SemanticHandoffError):
                    ExternalAgentSemanticHandoffService(
                        service,
                        JsonlAtomicInformationStore(root / "atomic.jsonl"),
                        audit_root,
                    ).recovery_preflight(
                        representation.representation_id,
                        provider,
                        self.privacy_binding(),
                    )
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.tree_snapshot(audit_root), before)
                self.assertFalse((root / "atomic.jsonl").exists())

    def test_v31_version_specific_inventory_rejects_impossible_entries(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        attacks = (
            "run-commit.json",
            "run-commit-unknown.json",
            "extra_file",
            "extra_directory",
            "commit_symlink",
            "result-commit.json",
            "result-commit-unknown.json",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                root = self.root / attack
                representation, service = self.build_service(root=root)
                audit_root = root / "audits"
                audit_root.mkdir(mode=0o700)
                os.chmod(audit_root, 0o700)
                runner = FakeRunner()
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                )
                recovery = handoff_module._SemanticRecoveryRun(
                    service,
                    audit_root,
                    representation.representation_id,
                    provider,
                    self.privacy_binding(),
                )
                receipt = json.loads(
                    json.dumps(recovery.expected_historical_v31_run_receipt)
                )
                legacy_run = self.write_v31_attempt_fixture(recovery, receipt)
                if attack in {"run-commit.json", "run-commit-unknown.json"}:
                    unexpected = legacy_run / attack
                    unexpected.write_text('{"state":"looks-valid"}', encoding="utf-8")
                    os.chmod(unexpected, 0o600)
                elif attack == "extra_file":
                    unexpected = legacy_run / "unexpected.json"
                    unexpected.write_text("{}", encoding="utf-8")
                    os.chmod(unexpected, 0o600)
                elif attack == "extra_directory":
                    unexpected = legacy_run / "unexpected"
                    unexpected.mkdir(mode=0o700)
                    os.chmod(unexpected, 0o700)
                elif attack == "commit_symlink":
                    (legacy_run / "run-commit.json").symlink_to(
                        "run-receipt.json"
                    )
                else:
                    result = legacy_run / "results" / "batch_0001"
                    result.mkdir(parents=True, mode=0o700)
                    os.chmod(result.parent, 0o700)
                    os.chmod(result, 0o700)
                    for name in (
                        "result.json",
                        "result-receipt.json",
                        "phase-post-strict-pending.json",
                        attack,
                    ):
                        path = result / name
                        path.write_text("{}", encoding="utf-8")
                        os.chmod(path, 0o600)
                before = self.tree_snapshot(audit_root)

                with self.assertRaises(SemanticHandoffError):
                    ExternalAgentSemanticHandoffService(
                        service,
                        JsonlAtomicInformationStore(root / "atomic.jsonl"),
                        audit_root,
                    ).recovery_preflight(
                        representation.representation_id,
                        provider,
                        self.privacy_binding(),
                    )
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.tree_snapshot(audit_root), before)
                self.assertFalse((root / "atomic.jsonl").exists())

    def test_v31_version_specific_pending_and_committed_inventory_is_counted(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        for committed in (False, True):
            with self.subTest(committed=committed):
                root = self.root / str(committed)
                representation, service = self.build_service(root=root)
                audit_root = root / "audits"
                audit_root.mkdir(mode=0o700)
                os.chmod(audit_root, 0o700)
                runner = FakeRunner()
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                )
                recovery = handoff_module._SemanticRecoveryRun(
                    service,
                    audit_root,
                    representation.representation_id,
                    provider,
                    self.privacy_binding(),
                )
                receipt = json.loads(
                    json.dumps(recovery.expected_historical_v31_run_receipt)
                )
                legacy_run = self.write_v31_attempt_fixture(recovery, receipt)
                result = legacy_run / "results" / "batch_0001"
                result.mkdir(parents=True, mode=0o700)
                os.chmod(result.parent, 0o700)
                os.chmod(result, 0o700)
                names = [
                    "result.json",
                    "result-receipt.json",
                    "phase-post-strict-pending.json",
                ]
                if committed:
                    names.append("phase-committed.json")
                for name in names:
                    path = result / name
                    path.write_text("{}", encoding="utf-8")
                    os.chmod(path, 0o600)
                before = self.tree_snapshot(audit_root)

                preflight = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    audit_root,
                ).recovery_preflight(
                    representation.representation_id,
                    provider,
                    self.privacy_binding(),
                )
                self.assertEqual(preflight.historical_counted_attempts, 1)
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.tree_snapshot(audit_root), before)

    def test_v32_recovery_identity_is_stable_and_binds_execution_contract(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(blocks=41)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        first = handoff_module._SemanticRecoveryRun(
            service,
            self.root / "audits",
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        same = handoff_module._SemanticRecoveryRun(
            service,
            self.root / "audits",
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        identity_keys = {
            "source",
            "representation",
            "privacy",
            "protocol_version",
            "provider_route",
            "provider",
            "execution_deadline_ms",
            "semantic_batch_size",
            "ordered_eligible_unit_ids",
            "prompt_template_fingerprint",
            "local_validator_contract_version",
            "batches",
        }
        identity = {
            key: first.expected_run_receipt[key] for key in identity_keys
        }
        self.assertEqual(first.semantic_run_id, same.semantic_run_id)
        self.assertEqual(
            first.execution_identity_fingerprint,
            _canonical_fingerprint(identity),
        )
        self.assertEqual(
            first.semantic_run_id,
            "semantic_run_"
            + first.execution_identity_fingerprint.removeprefix("sha256:")[:32],
        )
        self.assertEqual(identity["protocol_version"], EXTERNAL_AGENT_PROTOCOL_V3_2)
        self.assertEqual(identity["semantic_batch_size"], 40)
        self.assertEqual(len(identity["batches"]), 2)
        self.assertTrue(
            all(
                {
                    "result_schema_fingerprint",
                    "input_fingerprint",
                    "batch_contract_fingerprint",
                }
                <= set(batch)
                for batch in identity["batches"]
            )
        )

        changed_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            reasoning_effort="high",
            timeout_seconds=301,
            runner=FakeRunner(),
        )
        changed = handoff_module._SemanticRecoveryRun(
            service,
            self.root / "audits",
            representation.representation_id,
            changed_provider,
            self.privacy_binding(),
        )
        self.assertNotEqual(first.semantic_run_id, changed.semantic_run_id)

    def test_recovery_root_attacks_fail_closed_before_provider(self) -> None:
        for attack in (
            "mode_0755",
            "mode_0777",
            "file",
            "symlink",
            "unexpected",
            "parent_symlink",
        ):
            with self.subTest(attack=attack):
                root = self.root / attack
                representation, service = self.build_service(blocks=1, root=root)
                audit_root = root / "audits"
                if attack == "mode_0755":
                    audit_root.mkdir(mode=0o755)
                    os.chmod(audit_root, 0o755)
                elif attack == "mode_0777":
                    audit_root.mkdir(mode=0o777)
                    os.chmod(audit_root, 0o777)
                elif attack == "file":
                    audit_root.write_text("synthetic", encoding="utf-8")
                elif attack == "symlink":
                    target = root / "real-audits"
                    target.mkdir(mode=0o700)
                    audit_root.symlink_to(target, target_is_directory=True)
                elif attack == "unexpected":
                    audit_root.mkdir(mode=0o700)
                    os.chmod(audit_root, 0o700)
                    unexpected = audit_root / "unexpected.json"
                    unexpected.write_text("{}", encoding="utf-8")
                    os.chmod(unexpected, 0o600)
                else:
                    real_parent = root / "real-parent"
                    real_parent.mkdir(mode=0o700)
                    linked_parent = root / "linked-parent"
                    linked_parent.symlink_to(real_parent, target_is_directory=True)
                    audit_root = linked_parent / "audits"
                before = self.tree_snapshot(root)
                runner = FakeRunner()
                with self.assertRaises(SemanticHandoffError):
                    ExternalAgentSemanticHandoffService(
                        service,
                        JsonlAtomicInformationStore(root / "atomic.jsonl"),
                        audit_root,
                    ).execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", runner=runner
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=1,
                    )
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.tree_snapshot(root), before)
                self.assertFalse(
                    (root / "information" / representation.representation_id).exists()
                )
                self.assertFalse((root / "atomic.jsonl").exists())

    def test_run_root_fsync_failure_is_zero_call_locally_converged(self) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(blocks=1)
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        original_fsync = handoff_module._fsync_directory

        def fail_run_root(path):
            if path == audit_root and any(audit_root.glob("semantic_run_*")):
                raise OSError("synthetic run root fsync failure")
            return original_fsync(path)

        runner = FakeRunner()
        with (
            patch.object(handoff_module, "_fsync_directory", fail_run_root),
            self.assertRaises(OSError),
        ):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertEqual(runner.calls, [])
        run = next(audit_root.glob("semantic_run_*"))
        self.assertFalse((run / "run-commit.json").exists())
        next_runner = FakeRunner()
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=next_runner
            ),
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 0)
        self.assertEqual(preflight.required_new_calls, 1)
        self.assertEqual(preflight.conservatively_counted_attempts, 0)
        self.assertEqual(next_runner.calls, [])

    def test_recovery_2_receipts_bind_nonce_fingerprints_and_phases(self) -> None:
        representation, service = self.build_service(blocks=1)
        audit_root = self.root / "audits"
        ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
        run = next(audit_root.glob("semantic_run_*"))
        run_receipt = json.loads((run / "run-receipt.json").read_text())
        attempt = json.loads(
            (run / "attempts" / "batch_0001.json").read_text()
        )
        result = run / "results" / "batch_0001"
        result_receipt = json.loads((result / "result-receipt.json").read_text())
        pending = json.loads(
            (result / "phase-post-strict-pending.json").read_text()
        )
        committed = json.loads((result / "phase-committed.json").read_text())
        self.assertEqual(
            run_receipt["schema_version"],
            "semantic-handoff-run-receipt/2.0",
        )
        self.assertTrue(run_receipt["run_receipt_fingerprint"].startswith("sha256:"))
        self.assertEqual(
            attempt["schema_version"],
            "semantic-handoff-attempt-receipt/2.0",
        )
        self.assertEqual(len(attempt["attempt_nonce"]), 64)
        self.assertEqual(set(attempt["attempt_nonce"]) <= set("0123456789abcdef"), True)
        self.assertTrue(attempt["attempt_receipt_fingerprint"].startswith("sha256:"))
        self.assertEqual(
            result_receipt["schema_version"],
            "semantic-handoff-batch-result-receipt/2.0",
        )
        self.assertEqual(
            result_receipt["attempt_nonce"], attempt["attempt_nonce"]
        )
        self.assertEqual(
            result_receipt["attempt_receipt_fingerprint"],
            attempt["attempt_receipt_fingerprint"],
        )
        self.assertTrue(
            result_receipt["result_receipt_fingerprint"].startswith("sha256:")
        )
        self.assertEqual(
            pending["schema_version"],
            "semantic-handoff-batch-result-phase/1.0",
        )
        self.assertEqual(pending["phase"], "post_strict_pending")
        self.assertEqual(committed["phase"], "committed")
        self.assertEqual(
            pending["result_receipt_fingerprint"],
            result_receipt["result_receipt_fingerprint"],
        )
        self.assertEqual(
            committed["attempt_nonce"], attempt["attempt_nonce"]
        )

    def test_post_strict_bundle_failures_converge_without_provider(self) -> None:
        import archeos.semantic_handoff as handoff_module

        class SimulatedSigkill(BaseException):
            pass

        for attack in (
            "result_parent_fsync",
            "phase_write_oserror",
            "phase_write_baseexception",
            "phase_parent_fsync",
            "final_readback_baseexception",
        ):
            with self.subTest(attack=attack):
                root = self.root / attack
                representation, service = self.build_service(blocks=1, root=root)
                audit_root = root / "audits"
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    audit_root,
                )
                original_fsync = handoff_module._fsync_directory
                original_marker = handoff_module._publish_private_json_marker
                original_read = handoff_module._private_bytes_read

                def fail_fsync(
                    path, current_attack=attack, delegate=original_fsync
                ):
                    if (
                        current_attack == "result_parent_fsync"
                        and path.name == "results"
                    ):
                        raise OSError("synthetic result parent fsync failure")
                    if (
                        current_attack == "phase_parent_fsync"
                        and path.name == "batch_0001"
                        and (path / "phase-committed.json").exists()
                    ):
                        raise OSError("synthetic phase parent fsync failure")
                    return delegate(path)

                def fail_marker(
                    path,
                    payload,
                    current_attack=attack,
                    delegate=original_marker,
                ):
                    if path.name == "phase-committed.json":
                        if current_attack == "phase_write_oserror":
                            raise OSError("synthetic phase write failure")
                        if current_attack == "phase_write_baseexception":
                            raise SimulatedSigkill("synthetic process crash")
                    return delegate(path, payload)

                def fail_read(
                    path,
                    current_attack=attack,
                    delegate=original_read,
                ):
                    if (
                        current_attack == "final_readback_baseexception"
                        and path.name == "phase-committed.json"
                    ):
                        raise SimulatedSigkill("synthetic final readback crash")
                    return delegate(path)

                runner = FakeRunner()
                expected_error = (
                    SimulatedSigkill
                    if attack
                    in {
                        "phase_write_baseexception",
                        "final_readback_baseexception",
                    }
                    else Exception
                )
                with (
                    patch.object(handoff_module, "_fsync_directory", fail_fsync),
                    patch.object(
                        handoff_module,
                        "_publish_private_json_marker",
                        fail_marker,
                    ),
                    patch.object(handoff_module, "_private_bytes_read", fail_read),
                    self.assertRaises(expected_error),
                ):
                    handoff.execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", runner=runner
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=1,
                    )
                self.assertEqual(len(runner.calls), 1)
                self.assertFalse(
                    (root / "information" / representation.representation_id).exists()
                )
                self.assertFalse((root / "atomic.jsonl").exists())
                next_runner = FakeRunner()
                preflight = handoff.recovery_preflight(
                    representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.147.0", runner=next_runner
                    ),
                    self.privacy_binding(),
                )
                self.assertEqual(preflight.replayable_batches, 1)
                self.assertEqual(preflight.required_new_calls, 0)
                self.assertEqual(preflight.conservatively_counted_attempts, 0)
                result = handoff.execute(
                    representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.147.0", runner=next_runner
                    ),
                    privacy_binding=self.privacy_binding(),
                    new_call_authority=0,
                )
                self.assertEqual(next_runner.calls, [])
                self.assertEqual(result.ingestion.created, 1)

    def test_post_strict_refsync_failure_remains_locally_retryable(self) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(blocks=1)
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        original_marker = handoff_module._publish_private_json_marker

        def stop_before_committed_phase(path, payload):
            if path.name == "phase-committed.json":
                raise OSError("synthetic pending boundary")
            return original_marker(path, payload)

        with (
            patch.object(
                handoff_module,
                "_publish_private_json_marker",
                stop_before_committed_phase,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        original_fsync_file = handoff_module._fsync_file

        def fail_local_refsync(path):
            if path.name == "result.json":
                raise OSError("synthetic local refsync failure")
            return original_fsync_file(path)

        next_runner = FakeRunner()
        with (
            patch.object(handoff_module, "_fsync_file", fail_local_refsync),
            self.assertRaises(OSError),
        ):
            handoff.recovery_preflight(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=next_runner
                ),
                self.privacy_binding(),
            )
        self.assertEqual(next_runner.calls, [])
        self.assertFalse((self.root / "atomic.jsonl").exists())
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=next_runner
            ),
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 1)
        self.assertEqual(preflight.required_new_calls, 0)
        self.assertEqual(next_runner.calls, [])

    def test_pending_batch_one_converges_then_83_resumes_only_two_calls(self) -> None:
        import archeos.semantic_handoff as handoff_module

        class SimulatedSigkill(BaseException):
            pass

        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        store_path = self.root / "atomic.jsonl"
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )
        original_refsync = handoff_module._SemanticRecoveryRun._refsync_result_bundle

        def crash_after_final_rename(run, ordinal):
            if ordinal == 1:
                raise SimulatedSigkill("synthetic SIGKILL after final rename")
            return original_refsync(run, ordinal)

        first_runner = FakeRunner()
        with (
            patch.object(
                handoff_module._SemanticRecoveryRun,
                "_refsync_result_bundle",
                crash_after_final_rename,
            ),
            self.assertRaises(SimulatedSigkill),
        ):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=first_runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )
        self.assertEqual(len(first_runner.calls), 1)
        self.assertFalse(store_path.exists())
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        resume_runner = SequenceRunner("valid", "valid")
        resume_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=resume_runner
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            resume_provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 1)
        self.assertEqual(preflight.required_new_calls, 2)
        result = handoff.execute(
            representation.representation_id,
            resume_provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=2,
        )
        self.assertEqual(len(resume_runner.calls), 2)
        self.assertEqual(result.ingestion.created, 83)

    def test_final_all_batch_revalidation_rejects_earlier_batch_mutation(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        store_path = self.root / "atomic.jsonl"

        class MutatingThirdRunner(FakeRunner):
            def __call__(inner_self, command, **kwargs):
                if len(inner_self.calls) == 2:
                    first = next(
                        audit_root.glob(
                            "semantic_run_*/results/batch_0001/result.json"
                        )
                    )
                    os.chmod(first, 0o644)
                return super().__call__(command, **kwargs)

        runner = MutatingThirdRunner()
        with self.assertRaises(SemanticHandoffError):
            ExternalAgentSemanticHandoffService(
                service, JsonlAtomicInformationStore(store_path), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )
        self.assertEqual(len(runner.calls), 3)
        self.assertFalse(store_path.exists())
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )

    def test_pre_publish_revalidation_rejects_post_finalize_mutation(self) -> None:
        import archeos.representation_information as information_module

        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        store_path = self.root / "atomic.jsonl"
        original_output_records = information_module._output_records
        mutated = False

        def mutate_after_final_disk_reload(*args, **kwargs):
            nonlocal mutated
            output = original_output_records(*args, **kwargs)
            if not mutated:
                first = next(
                    audit_root.glob(
                        "semantic_run_*/results/batch_0001/result.json"
                    )
                )
                os.chmod(first, 0o644)
                mutated = True
            return output

        runner = FakeRunner()
        with (
            patch.object(
                information_module,
                "_output_records",
                mutate_after_final_disk_reload,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            ExternalAgentSemanticHandoffService(
                service, JsonlAtomicInformationStore(store_path), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )
        self.assertTrue(mutated)
        self.assertEqual(len(runner.calls), 3)
        self.assertFalse(store_path.exists())
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )

    def test_ordinary_provider_cannot_duck_type_an_output_finalizer(self) -> None:
        representation, service = self.build_service(blocks=1)

        class DuckFinalizingProvider:
            name = "synthetic-duck-finalizer"

            def __init__(inner_self) -> None:
                inner_self.finalize_called = False

            def analyze(
                inner_self, batch: RepresentationAnalysisBatch
            ) -> RepresentationAnalysisResult:
                anchor = batch.anchor_units[0]
                return RepresentationAnalysisResult(
                    candidates=(
                        RepresentationCandidateDraft(
                            statement="Synthetic retained statement.",
                            semantic_type="observation",
                            concerns=("Synthetic",),
                            evidence_unit_ids=(anchor.unit_id,),
                            context=anchor.context,
                            confidence=1.0,
                        ),
                    ),
                    residue=(),
                )

            def finalize_results(inner_self, _outputs):
                inner_self.finalize_called = True
                raise AssertionError(
                    "ordinary providers cannot replace validated outputs"
                )

        provider = DuckFinalizingProvider()
        package = service.extract(representation.representation_id, provider)
        self.assertFalse(provider.finalize_called)
        self.assertTrue(package.is_dir())

    def test_recovered_finalized_outputs_are_strictly_revalidated_zero_call(
        self,
    ) -> None:
        import archeos.representation_information as information_module
        import archeos.semantic_handoff as handoff_module

        def attacked_outputs(mode, outputs):
            first = outputs[0]
            candidate = first.candidates[0]
            if mode == "dual_assignment":
                changed = replace(
                    first,
                    residue=(
                        RepresentationResidueDraft(
                            evidence_unit_ids=(candidate.evidence_unit_ids[0],),
                            reason_not_absorbed="Synthetic conflicting residue.",
                            future_value_or_uncertainty="Synthetic future value.",
                        ),
                    ),
                )
                return (changed,)
            if mode == "duplicate_candidate":
                return (replace(first, candidates=(*first.candidates, candidate)),)
            if mode == "wrong_evidence":
                changed_candidate = replace(
                    candidate,
                    evidence_unit_ids=("unit_" + "f" * 64,),
                )
                return (replace(first, candidates=(changed_candidate,)),)
            if mode == "anchor_omission":
                return (RepresentationAnalysisResult(candidates=(), residue=()),)
            if mode == "missing_output":
                return outputs[:-1]
            if mode == "extra_output":
                return (*outputs, first)
            if mode == "shape_drift":
                return (object(),)
            raise AssertionError(mode)

        for mode in (
            "dual_assignment",
            "duplicate_candidate",
            "wrong_evidence",
            "anchor_omission",
            "missing_output",
            "extra_output",
            "shape_drift",
        ):
            with self.subTest(mode=mode):
                root = self.root / mode
                representation, service = self.build_service(blocks=1, root=root)
                audit_root = root / "audits"
                store_path = root / "atomic.jsonl"
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(store_path),
                    audit_root,
                )
                first_runner = FakeRunner()
                original_package_publish = (
                    information_module.publish_directory_no_replace
                )

                expected_package = (
                    service.output_root / representation.representation_id
                )

                def fail_package_publish(
                    staging,
                    final,
                    expected=expected_package,
                    delegate=original_package_publish,
                ):
                    if final == expected:
                        raise OSError("synthetic package publish interruption")
                    return delegate(staging, final)

                with (
                    patch.object(
                        information_module,
                        "publish_directory_no_replace",
                        fail_package_publish,
                    ),
                    self.assertRaises(SemanticHandoffError),
                ):
                    handoff.execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0",
                            runner=first_runner,
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=1,
                    )
                self.assertEqual(len(first_runner.calls), 1)
                self.assertFalse(store_path.exists())
                self.assertFalse(
                    (
                        service.output_root / representation.representation_id
                    ).exists()
                )

                original_finalize = (
                    handoff_module._RecoveryAwareProvider.finalize_results
                )

                @contextmanager
                def malicious_finalize(
                    instance,
                    early_outputs,
                    current_mode=mode,
                    delegate=original_finalize,
                ):
                    with delegate(instance, early_outputs) as finalized:
                        yield replace(
                            finalized,
                            outputs=attacked_outputs(
                                current_mode, finalized.outputs
                            ),
                        )

                resume_runner = FakeRunner()
                with (
                    patch.object(
                        handoff_module._RecoveryAwareProvider,
                        "finalize_results",
                        malicious_finalize,
                    ),
                    self.assertRaises(SemanticHandoffError),
                ):
                    handoff.execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0",
                            runner=resume_runner,
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=0,
                    )
                self.assertEqual(resume_runner.calls, [])
                self.assertFalse(store_path.exists())
                self.assertFalse(
                    (
                        service.output_root / representation.representation_id
                    ).exists()
                )

    def test_recovery_2_receipt_attacks_and_legacy_1_fail_closed(self) -> None:
        import archeos.semantic_handoff as handoff_module

        for attack in (
            "attempt_nonce",
            "pending_phase",
            "nlink",
            "staging",
            "legacy_1",
            "failure_audit_conflict",
        ):
            with self.subTest(attack=attack):
                root = self.root / attack
                representation, service = self.build_service(blocks=1, root=root)
                audit_root = root / "audits"
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    audit_root,
                )
                original_marker = handoff_module._publish_private_json_marker

                def leave_pending(path, payload, delegate=original_marker):
                    if path.name == "phase-committed.json":
                        raise RuntimeError("synthetic pending result")
                    return delegate(path, payload)

                with (
                    patch.object(
                        handoff_module,
                        "_publish_private_json_marker",
                        leave_pending,
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    handoff.execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", runner=FakeRunner()
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=1,
                    )
                run = next(audit_root.glob("semantic_run_*"))
                attempt_path = run / "attempts" / "batch_0001.json"
                result = run / "results" / "batch_0001"
                if attack == "attempt_nonce":
                    attempt = json.loads(attempt_path.read_text())
                    attempt["attempt_nonce"] = "0" * 64
                    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
                elif attack == "pending_phase":
                    phase_path = result / "phase-post-strict-pending.json"
                    phase = json.loads(phase_path.read_text())
                    phase["phase"] = "committed"
                    phase_path.write_text(json.dumps(phase), encoding="utf-8")
                elif attack == "nlink":
                    os.link(result / "result.json", root / "result-hardlink.json")
                elif attack == "staging":
                    for child in result.iterdir():
                        child.unlink()
                    result.rmdir()
                    staging = run / "results" / ".batch_0002.staging"
                    staging.mkdir(mode=0o700)
                elif attack == "legacy_1":
                    receipt_path = run / "run-receipt.json"
                    receipt = json.loads(receipt_path.read_text())
                    receipt["schema_version"] = "semantic-handoff-run-receipt/1.0"
                    receipt.pop("run_receipt_fingerprint")
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                else:
                    result_receipt = json.loads(
                        (result / "result-receipt.json").read_text()
                    )
                    processing_run_id = result_receipt["processing_run_id"]
                    conflict_dir = audit_root / processing_run_id
                    conflict_dir.mkdir(mode=0o700)
                    conflict = conflict_dir / "processing-run-audit.json"
                    conflict.write_text(
                        json.dumps(
                            {
                                "processing_run_id": processing_run_id,
                                "execution_status": "failed",
                                "failure_category": "runtime_execution_failure",
                                "strict_validation_status": "failed",
                            }
                        ),
                        encoding="utf-8",
                    )
                    os.chmod(conflict, 0o600)
                next_runner = FakeRunner()
                with self.assertRaises(SemanticHandoffError):
                    handoff.recovery_preflight(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", runner=next_runner
                        ),
                        self.privacy_binding(),
                    )
                self.assertEqual(next_runner.calls, [])
                self.assertFalse((root / "atomic.jsonl").exists())
                self.assertFalse(
                    (root / "information" / representation.representation_id).exists()
                )

    @unittest.skipUnless(
        sys.platform in {"darwin", "linux"} and hasattr(os, "fork"),
        "real fork/SIGKILL durability matrix requires macOS or Linux",
    )
    def test_real_fork_sigkill_durable_recovery_matrix(self) -> None:
        import archeos.semantic_handoff as handoff_module

        convergable = {
            "final_rename",
            "phase_visible",
            "phase_parent_fsync",
            "final_readback",
        }
        hard_blocked = {
            "run_staging",
            "attempt_staging",
            "result_staging",
        }
        boundaries = (
            "run_staging",
            "run_root_fsync",
            "attempt_staging",
            "attempt_visible",
            "result_staging",
            "final_rename",
            "phase_visible",
            "phase_parent_fsync",
            "final_readback",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                root = self.root / boundary
                representation, service = self.build_service(blocks=1, root=root)
                audit_root = root / "audits"
                store_path = root / "atomic.jsonl"
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(store_path),
                    audit_root,
                )
                pid = os.fork()
                if pid == 0:
                    original_directory_publish = (
                        handoff_module.publish_directory_no_replace
                    )
                    original_file_publish = handoff_module.publish_file_no_replace
                    original_fsync = handoff_module._fsync_directory
                    original_load = handoff_module._SemanticRecoveryRun._load_result

                    def kill_now() -> None:
                        os.kill(os.getpid(), 9)

                    def directory_publish(
                        staging,
                        final,
                        current_boundary=boundary,
                        delegate=original_directory_publish,
                    ):
                        if (
                            current_boundary == "run_staging"
                            and final.name.startswith("semantic_run_")
                        ) or (
                            current_boundary == "result_staging"
                            and final.name == "batch_0001"
                        ):
                            kill_now()
                        output = delegate(staging, final)
                        if (
                            current_boundary == "final_rename"
                            and final.name == "batch_0001"
                        ):
                            kill_now()
                        return output

                    def file_publish(
                        staging,
                        final,
                        current_boundary=boundary,
                        delegate=original_file_publish,
                    ):
                        if (
                            current_boundary == "attempt_staging"
                            and final.name == "batch_0001.json"
                            and final.parent.name == "attempts"
                        ):
                            kill_now()
                        output = delegate(staging, final)
                        if (
                            current_boundary == "phase_visible"
                            and final.name == "phase-committed.json"
                        ):
                            kill_now()
                        return output

                    def fsync_directory(
                        path,
                        current_boundary=boundary,
                        current_audit_root=audit_root,
                        delegate=original_fsync,
                    ):
                        if (
                            current_boundary == "run_root_fsync"
                            and path == current_audit_root
                            and any(current_audit_root.glob("semantic_run_*"))
                        ) or (
                            current_boundary == "phase_parent_fsync"
                            and path.name == "batch_0001"
                            and (path / "phase-committed.json").exists()
                        ):
                            kill_now()
                        return delegate(path)

                    def load_result(
                        run,
                        ordinal,
                        contract,
                        attempt,
                        current_boundary=boundary,
                        delegate=original_load,
                    ):
                        loaded = delegate(run, ordinal, contract, attempt)
                        if (
                            current_boundary == "final_readback"
                            and ordinal == 1
                            and (
                                run._result_path(ordinal)
                                / "phase-committed.json"
                            ).exists()
                        ):
                            kill_now()
                        return loaded

                    class ChildRunner(FakeRunner):
                        def __call__(
                            inner_self,
                            command,
                            current_boundary=boundary,
                            **kwargs,
                        ):
                            if current_boundary == "attempt_visible":
                                kill_now()
                            return super().__call__(command, **kwargs)

                    patchers = (
                        patch.object(
                            handoff_module,
                            "publish_directory_no_replace",
                            directory_publish,
                        ),
                        patch.object(
                            handoff_module,
                            "publish_file_no_replace",
                            file_publish,
                        ),
                        patch.object(
                            handoff_module,
                            "_fsync_directory",
                            fsync_directory,
                        ),
                        patch.object(
                            handoff_module._SemanticRecoveryRun,
                            "_load_result",
                            load_result,
                        ),
                    )
                    for patcher in patchers:
                        patcher.start()
                    try:
                        handoff.execute(
                            representation.representation_id,
                            CodexCliRepresentationAnalysisProvider(
                                provider_version="0.147.0",
                                runner=ChildRunner(),
                            ),
                            privacy_binding=self.privacy_binding(),
                            new_call_authority=1,
                        )
                    finally:
                        os._exit(91)
                waited_pid, status = os.waitpid(pid, 0)
                self.assertEqual(waited_pid, pid)
                self.assertTrue(os.WIFSIGNALED(status), status)
                self.assertEqual(os.WTERMSIG(status), 9)
                next_runner = FakeRunner()
                next_provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=next_runner
                )
                if boundary in convergable:
                    preflight = handoff.recovery_preflight(
                        representation.representation_id,
                        next_provider,
                        self.privacy_binding(),
                    )
                    self.assertEqual(preflight.replayable_batches, 1)
                    self.assertEqual(preflight.required_new_calls, 0)
                    result = handoff.execute(
                        representation.representation_id,
                        next_provider,
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=0,
                    )
                    self.assertEqual(result.ingestion.created, 1)
                    self.assertEqual(next_runner.calls, [])
                    self.assertTrue(result.package.is_dir())
                elif boundary == "run_root_fsync":
                    preflight = handoff.recovery_preflight(
                        representation.representation_id,
                        next_provider,
                        self.privacy_binding(),
                    )
                    self.assertEqual(preflight.replayable_batches, 0)
                    self.assertEqual(preflight.required_new_calls, 1)
                    self.assertEqual(next_runner.calls, [])
                elif boundary == "attempt_visible":
                    preflight = handoff.recovery_preflight(
                        representation.representation_id,
                        next_provider,
                        self.privacy_binding(),
                    )
                    self.assertEqual(preflight.conservatively_counted_attempts, 1)
                    with self.assertRaisesRegex(Exception, "LEAD_DECISION_REQUIRED"):
                        handoff.execute(
                            representation.representation_id,
                            next_provider,
                            privacy_binding=self.privacy_binding(),
                            new_call_authority=1,
                        )
                    self.assertEqual(next_runner.calls, [])
                else:
                    self.assertIn(boundary, hard_blocked)
                    with self.assertRaises(SemanticHandoffError):
                        handoff.recovery_preflight(
                            representation.representation_id,
                            next_provider,
                            self.privacy_binding(),
                        )
                    self.assertEqual(next_runner.calls, [])
                if boundary not in convergable:
                    self.assertFalse(store_path.exists())
                    self.assertFalse(
                        (
                            root
                            / "information"
                            / representation.representation_id
                        ).exists()
                    )

    def test_recovery_resumes_40_40_3_after_first_result_receipt(self) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        store_path = self.root / "atomic.jsonl"
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )
        first_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        original_publish = handoff_module._SemanticRecoveryRun.publish_result

        def crash_after_first_receipt(run, ordinal, raw_result, record):
            loaded = original_publish(run, ordinal, raw_result, record)
            if ordinal == 1:
                raise OSError("synthetic crash after durable batch receipt")
            return loaded

        with (
            patch.object(
                handoff_module._SemanticRecoveryRun,
                "publish_result",
                crash_after_first_receipt,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            handoff.execute(
                representation.representation_id,
                first_provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )
        self.assertEqual(len(first_provider.execution_records), 1)
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse(store_path.exists())

        resume_runner = SequenceRunner("valid", "valid")
        resume_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=resume_runner
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            resume_provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 1)
        self.assertEqual(preflight.required_new_calls, 2)
        result = handoff.execute(
            representation.representation_id,
            resume_provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=2,
        )
        self.assertEqual(len(resume_runner.calls), 2)
        self.assertEqual(result.ingestion.created, 83)
        manifest = json.loads((result.package / "manifest.json").read_text())
        self.assertEqual(
            [len(batch["unit_ids"]) for batch in manifest["batches"]],
            [40, 40, 3],
        )
        recovery_run = next(audit_root.glob("semantic_run_*"))
        for directory in (
            recovery_run,
            recovery_run / "attempts",
            recovery_run / "results",
            *(recovery_run / "results").glob("batch_*"),
        ):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in recovery_run.rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_recovery_attempt_is_conservatively_counted_and_not_retried(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=SequenceRunner("valid", "nonzero"),
        )
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            handoff.execute(
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )
        self.assertEqual(len(provider.execution_records), 2)
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())

        next_runner = FakeRunner()
        next_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=next_runner
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            next_provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 1)
        self.assertEqual(preflight.required_new_calls, 2)
        self.assertEqual(preflight.conservatively_counted_attempts, 1)
        with self.assertRaisesRegex(Exception, "LEAD_DECISION_REQUIRED"):
            handoff.execute(
                representation.representation_id,
                next_provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=2,
            )
        self.assertEqual(next_runner.calls, [])

    def test_recovery_supports_candidate_residue_and_mixed_40_anchor_results(
        self,
    ) -> None:
        expected_counts = {
            "valid": (40, 0),
            "all_residue": (0, 40),
            "mixed": (20, 20),
        }
        for mode, expected in expected_counts.items():
            with self.subTest(mode=mode):
                root = self.root / mode
                representation, service = self.build_service(blocks=40, root=root)
                runner = FakeRunner(mode)
                result = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    root / "audits",
                ).execute(
                    representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.147.0", runner=runner
                    ),
                    privacy_binding=self.privacy_binding(),
                    new_call_authority=1,
                )
                manifest = json.loads((result.package / "manifest.json").read_text())
                self.assertEqual(
                    (
                        manifest["counts"]["atomic_information_candidates"],
                        manifest["counts"]["residue_items"],
                    ),
                    expected,
                )
                self.assertEqual(len(runner.calls), 1)

    def test_cross_batch_candidate_collision_stays_partial_and_zero_call_replays(
        self,
    ) -> None:
        import archeos.representation_information as information_module

        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        first_runner = FakeRunner()
        with (
            patch.object(
                information_module,
                "_candidate_id",
                return_value="candidate_" + "0" * 64,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=first_runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )
        self.assertEqual(len(first_runner.calls), 3)
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())
        replay_runner = FakeRunner()
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=replay_runner
            ),
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 3)
        self.assertEqual(preflight.required_new_calls, 0)
        with (
            patch.object(
                information_module,
                "_candidate_id",
                return_value="candidate_" + "0" * 64,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=replay_runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=0,
            )
        self.assertEqual(replay_runner.calls, [])

    def test_recovery_attempt_is_durable_before_provider_start(self) -> None:
        representation, service = self.build_service(blocks=40)
        audit_root = self.root / "audits"

        class StartFailRunner:
            calls = 0

            def __call__(inner_self, _command, **_kwargs):
                inner_self.calls += 1
                attempt = next(
                    audit_root.glob("semantic_run_*/attempts/batch_0001.json")
                )
                self.assertEqual(attempt.stat().st_mode & 0o777, 0o600)
                payload = json.loads(attempt.read_text())
                self.assertEqual(payload["state"], "started")
                raise OSError("synthetic process start failure")

        runner = StartFailRunner()
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertEqual(runner.calls, 1)
        next_runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "LEAD_DECISION_REQUIRED"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=next_runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertEqual(next_runner.calls, [])

    def test_recovery_tamper_and_profile_drift_fail_before_provider(self) -> None:
        import archeos.semantic_handoff as handoff_module

        for attack in (
            "raw",
            "mode",
            "symlink",
            "run_binding",
            "prompt",
            "schema",
            "partition",
            "privacy",
            "inventory",
            "batch_swap",
            "profile",
        ):
            with self.subTest(attack=attack):
                root = self.root / attack
                representation, service = self.build_service(blocks=83, root=root)
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    root / "audits",
                )
                original_publish = handoff_module._SemanticRecoveryRun.publish_result

                def crash(
                    run,
                    ordinal,
                    raw_result,
                    record,
                    publish=original_publish,
                ):
                    publish(run, ordinal, raw_result, record)
                    raise OSError("synthetic crash")

                with (
                    patch.object(
                        handoff_module._SemanticRecoveryRun,
                        "publish_result",
                        crash,
                    ),
                    self.assertRaises(SemanticHandoffError),
                ):
                    handoff.execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", runner=FakeRunner()
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=3,
                    )
                run = next((root / "audits").glob("semantic_run_*"))
                result = run / "results" / "batch_0001"
                if attack == "raw":
                    (result / "result.json").write_bytes(b"{}")
                elif attack == "mode":
                    os.chmod(result / "result.json", 0o644)
                elif attack == "symlink":
                    (result / "result.json").unlink()
                    (result / "result.json").symlink_to("result-receipt.json")
                elif attack in {
                    "run_binding",
                    "prompt",
                    "schema",
                    "partition",
                    "privacy",
                }:
                    receipt_path = run / "run-receipt.json"
                    receipt = json.loads(receipt_path.read_text())
                    if attack == "run_binding":
                        receipt["semantic_batch_size"] = 41
                    elif attack == "prompt":
                        receipt["prompt_template_fingerprint"] = "sha256:" + "0" * 64
                    elif attack == "schema":
                        receipt["batches"][0]["result_schema_fingerprint"] = (
                            "sha256:" + "0" * 64
                        )
                    elif attack == "partition":
                        receipt["batches"][0]["anchor_unit_ids"][:2] = reversed(
                            receipt["batches"][0]["anchor_unit_ids"][:2]
                        )
                    else:
                        receipt["privacy"]["receipt_fingerprint"] = (
                            "sha256:" + "0" * 64
                        )
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                elif attack == "inventory":
                    unexpected = run / "unexpected.json"
                    unexpected.write_text("{}", encoding="utf-8")
                    os.chmod(unexpected, 0o600)
                elif attack == "batch_swap":
                    (run / "results" / "batch_0001").rename(
                        run / "results" / "batch_0002"
                    )
                next_provider = CodexCliRepresentationAnalysisProvider(
                    provider_version=(
                        "0.148.0" if attack == "profile" else "0.147.0"
                    ),
                    runner=FakeRunner(),
                )
                with self.assertRaises(SemanticHandoffError):
                    handoff.recovery_preflight(
                        representation.representation_id,
                        next_provider,
                        self.privacy_binding(),
                    )
                self.assertEqual(next_provider.execution_records, [])

    def test_recovery_call_authority_is_checked_before_any_attempt(self) -> None:
        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "调用授权不足"):
            ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                audit_root,
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=2,
            )
        self.assertEqual(runner.calls, [])
        self.assertFalse(audit_root.exists())

    def test_concurrent_recovery_runner_cannot_duplicate_a_batch_call(self) -> None:
        representation, service = self.build_service(blocks=40)
        audit_root = self.root / "audits"
        started = threading.Event()
        release = threading.Event()

        class BlockingRunner(FakeRunner):
            def __call__(inner_self, command, **kwargs):
                started.set()
                if not release.wait(5):
                    raise AssertionError("synthetic concurrency release timed out")
                return super().__call__(command, **kwargs)

        winner_runner = BlockingRunner()
        winner_result: list[object] = []
        winner_error: list[BaseException] = []

        def run_winner() -> None:
            try:
                winner_result.append(
                    ExternalAgentSemanticHandoffService(
                        service,
                        JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                        audit_root,
                    ).execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", runner=winner_runner
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=1,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - thread evidence capture.
                winner_error.append(exc)

        thread = threading.Thread(target=run_winner)
        thread.start()
        self.assertTrue(started.wait(5))
        loser_runner = FakeRunner()
        try:
            with self.assertRaisesRegex(Exception, "LEAD_DECISION_REQUIRED"):
                ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                    audit_root,
                ).execute(
                    representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.147.0", runner=loser_runner
                    ),
                    privacy_binding=self.privacy_binding(),
                    new_call_authority=1,
                )
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(winner_error, [])
        self.assertEqual(len(winner_result), 1)
        self.assertEqual(len(winner_runner.calls), 1)
        self.assertEqual(loser_runner.calls, [])

    def test_result_publish_collision_stops_without_package_or_retry(self) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(blocks=40)
        audit_root = self.root / "audits"
        runner = FakeRunner()
        original_publish = handoff_module.publish_directory_no_replace

        def collide_on_batch(staging, final):
            if final.name == "batch_0001":
                raise FileExistsError("synthetic result collision")
            return original_publish(staging, final)

        with (
            patch.object(
                handoff_module,
                "publish_directory_no_replace",
                collide_on_batch,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                audit_root,
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())
        retry_runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "LEAD_DECISION_REQUIRED"):
            ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                audit_root,
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=retry_runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertEqual(retry_runner.calls, [])

    def test_success_raw_body_is_only_in_private_batch_artifact(self) -> None:
        representation, service = self.build_service(blocks=1)
        audit_root = self.root / "audits"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).execute(
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
        result_path = next(
            audit_root.glob("semantic_run_*/results/batch_0001/result.json")
        )
        self.assertIn("Synthetic statement.", result_path.read_text())
        for path in audit_root.glob("run_*/processing-run-audit.json"):
            self.assertNotIn("Synthetic statement.", path.read_text())
        self.assertEqual(provider._successful_results, [])

    def test_complete_package_publish_crash_recovers_without_provider_call(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=83)
        audit_root = self.root / "audits"
        store_path = self.root / "atomic.jsonl"
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        with (
            patch.object(
                ExternalAgentSemanticHandoffService,
                "_persist_audits",
                side_effect=OSError("synthetic pre-audit crash"),
            ),
            self.assertRaises(OSError),
        ):
            handoff.execute(
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )
        package = self.root / "information" / representation.representation_id
        self.assertTrue(package.is_dir())
        self.assertFalse(store_path.exists())
        self.assertEqual(len(provider.execution_records), 3)

        replay_runner = FakeRunner()
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=replay_runner
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=0,
        )
        self.assertEqual(replay_runner.calls, [])
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.created, 83)

    def test_historical_package_replay_ignores_absent_recovery_receipts(self) -> None:
        representation, service = self.build_service(blocks=2)
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        replay_runner = FakeRunner()
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=replay_runner
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=0,
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay_runner.calls, [])

    def test_smaller_default_partition_is_deterministic_and_replays_published_batches(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=81)
        units = _units_from_representation(
            representation, service.representation_repository
        )
        first_partition = _analysis_batches(units, service.batch_size)
        second_partition = _analysis_batches(units, service.batch_size)
        self.assertEqual(service.batch_size, 40)
        self.assertEqual(
            [len(batch.anchor_units) for batch in first_partition], [40, 40, 1]
        )
        self.assertEqual(
            [
                tuple(unit.unit_id for unit in batch.anchor_units)
                for batch in first_partition
            ],
            [
                tuple(unit.unit_id for unit in batch.anchor_units)
                for batch in second_partition
            ],
        )
        self.assertEqual(
            tuple(
                unit.unit_id
                for batch in first_partition
                for unit in batch.anchor_units
            ),
            tuple(unit.unit_id for unit in units if unit.analysis_eligible),
        )

        audit_root = self.root / "audits"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        first = handoff.execute(representation.representation_id, provider)
        manifest = json.loads((first.package / "manifest.json").read_text())
        self.assertEqual(
            [len(batch["unit_ids"]) for batch in manifest["batches"]], [40, 40, 1]
        )
        self.assertEqual(len(provider.execution_records), 3)

        service.batch_size = 100
        replay_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        replay = handoff.execute(representation.representation_id, replay_provider)
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay_provider.execution_records, [])
        self.assertEqual(replay.ingestion.existing, 81)

    def test_anchor_coverage_failure_in_later_smaller_batch_is_fail_closed(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=81)
        audit_root = self.root / "audits"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=SequenceRunner("valid", "anchor_uncovered"),
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )

        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            handoff.execute(representation.representation_id, provider)

        audits = sorted(audit_root.glob("*/processing-run-audit.json"))
        audit_payloads = [json.loads(path.read_text()) for path in audits]
        self.assertEqual(len(audits), 2)
        failed = next(
            payload
            for payload in audit_payloads
            if payload["execution_status"] == "failed"
        )
        self.assertEqual(failed["failure_category"], "result_contract_failure")
        self.assertEqual(failed["contract_failure_detail"], "anchor_coverage")
        self.assertEqual(failed["eligible_units"], 40)
        self.assertEqual(failed["covered_units"], 0)
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())
        self.assertFalse((self.root / "04_core").exists())

    def test_audit_is_read_back_before_durable_ingestion(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        case = self

        class InspectingStore(JsonlAtomicInformationStore):
            def ingest_batch(self, revisions):
                audits = list(audit_root.glob("*/processing-run-audit.json"))
                case.assertEqual(len(audits), 1)
                observed = json.loads(audits[0].read_text())
                case.assertEqual(observed["audit_readback_status"], "verified")
                case.assertEqual(
                    observed["durable_ingestion_status"], "write_attempt_started"
                )
                return super().ingest_batch(revisions)

        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service, InspectingStore(self.root / "atomic.jsonl"), audit_root
        )
        handoff.execute(representation.representation_id, provider)

    def test_replay_requires_the_complete_batch_audit_set_before_store_write(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=2)
        service.batch_size = 1
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "initial-atomic.jsonl"),
            audit_root,
        )
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=SequenceRunner("valid", "valid")
            ),
        )
        next(audit_root.glob("*/processing-run-audit.json")).unlink()
        replay_store = self.root / "replay-atomic.jsonl"
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            ExternalAgentSemanticHandoffService(
                service, JsonlAtomicInformationStore(replay_store), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
            )
        self.assertFalse(replay_store.exists())

    def test_replay_rejects_corrupt_batch_audit_before_store_write(self) -> None:
        representation, service = self.build_service(blocks=2)
        service.batch_size = 1
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "initial-atomic.jsonl"),
            audit_root,
        )
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=SequenceRunner("valid", "valid")
            ),
        )
        next(audit_root.glob("*/processing-run-audit.json")).write_text(
            "{synthetic corruption", encoding="utf-8"
        )
        replay_store = self.root / "replay-atomic.jsonl"
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            ExternalAgentSemanticHandoffService(
                service, JsonlAtomicInformationStore(replay_store), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
            )
        self.assertFalse(replay_store.exists())

    def test_store_readback_failure_keeps_truthful_recovery_audit(self) -> None:
        representation, service = self.build_service()
        store_path = self.root / "atomic.jsonl"
        audit_root = self.root / "audits"

        class ReadbackFailingStore(JsonlAtomicInformationStore):
            def get_current(self, _atomic_information_id):
                raise OSError("synthetic store readback failure")

        with self.assertRaisesRegex(Exception, "已写入或正在读回"):
            ExternalAgentSemanticHandoffService(
                service, ReadbackFailingStore(store_path), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
            )
        pending = json.loads(
            next(audit_root.glob("*/processing-run-audit.json")).read_text()
        )
        self.assertTrue(pending["information_ingested"])
        self.assertEqual(
            pending["durable_ingestion_status"], "written_readback_pending"
        )
        self.assertTrue(store_path.exists())
        replay = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        ).execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        self.assertEqual(replay.ingestion.existing, 1)
        completed = json.loads(replay.audit_paths[0].read_text())
        self.assertEqual(completed["durable_ingestion_status"], "completed")
        self.assertEqual(completed["audit_readback_status"], "verified")

    def test_replay_rejects_tampered_audit_bindings_before_store_write(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "initial-atomic.jsonl"),
            audit_root,
        ).execute(representation.representation_id, provider)
        audit_path = next(audit_root.glob("*/processing-run-audit.json"))
        original = json.loads(audit_path.read_text())
        tampered_values = {
            "protocol_version": "synthetic/invalid",
            "input_fingerprint": "sha256:" + "0" * 64,
            "provider_route": "synthetic-route",
            "provider_version": "0.0.0",
            "eligible_units": 99,
            "covered_units": 99,
            "unaccounted_units": 1,
            "failure_category": "synthetic_failure",
            "handoff_status": "synthetic_status",
            "result_readback_status": "pending",
        }
        for field, value in tampered_values.items():
            with self.subTest(field=field):
                payload = dict(original)
                payload[field] = value
                audit_path.write_text(json.dumps(payload), encoding="utf-8")
                replay_store = self.root / f"replay-{field}.jsonl"
                with self.assertRaisesRegex(Exception, "未能安全重放"):
                    ExternalAgentSemanticHandoffService(
                        service, JsonlAtomicInformationStore(replay_store), audit_root
                    ).execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", runner=FakeRunner()
                        ),
                    )
                self.assertFalse(replay_store.exists())
        audit_path.write_text(json.dumps(original), encoding="utf-8")

    def test_reused_provider_persists_only_current_package_records(self) -> None:
        first_representation, first_service = self.build_service(root=self.root / "first")
        second_representation, second_service = self.build_service(root=self.root / "second")
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=SequenceRunner("valid", "valid")
        )
        ExternalAgentSemanticHandoffService(
            first_service,
            JsonlAtomicInformationStore(self.root / "first-atomic.jsonl"),
            self.root / "first-audits",
        ).execute(first_representation.representation_id, provider)
        second = ExternalAgentSemanticHandoffService(
            second_service,
            JsonlAtomicInformationStore(self.root / "second-atomic.jsonl"),
            self.root / "second-audits",
        ).execute(second_representation.representation_id, provider)
        self.assertEqual(len(provider.execution_records), 2)
        self.assertEqual(len(second.audit_paths), 1)
        self.assertEqual(
            len(list((self.root / "second-audits").glob("*/processing-run-audit.json"))),
            1,
        )

    def test_written_pending_marker_is_recovered_by_exact_replay(self) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "audits"
        import archeos.semantic_handoff as handoff_module

        original_write = handoff_module._private_json_write
        failed = False

        def fail_written_verified_marker(path, payload):
            nonlocal failed
            if (
                payload.get("durable_ingestion_status") == "written_readback_pending"
                and payload.get("audit_readback_status") == "verified"
                and not failed
            ):
                failed = True
                raise OSError("synthetic written marker failure")
            original_write(path, payload)

        with (
            patch.object(
                handoff_module, "_private_json_write", fail_written_verified_marker
            ),
            self.assertRaisesRegex(Exception, "已写入或正在读回"),
        ):
            ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                audit_root,
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
            )
        pending = json.loads(
            next(audit_root.glob("*/processing-run-audit.json")).read_text()
        )
        self.assertEqual(
            pending["durable_ingestion_status"], "written_readback_pending"
        )
        self.assertEqual(pending["audit_readback_status"], "pending")
        replay = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        self.assertEqual(replay.ingestion.existing, 1)
        completed = json.loads(replay.audit_paths[0].read_text())
        self.assertEqual(completed["durable_ingestion_status"], "completed")
        self.assertEqual(completed["audit_readback_status"], "verified")

    def test_communicate_value_error_cleans_up_process_group(self) -> None:
        from archeos import representation_information

        class BrokenProcess:
            pid = 12345
            returncode = None

            def communicate(self, **_kwargs):
                raise ValueError("synthetic encoding failure")

        signals: list[int] = []

        def kill_group(_pid: int, signal: int) -> None:
            signals.append(signal)
            if signal == 0:
                raise ProcessLookupError

        with patch.object(representation_information.os, "killpg", kill_group):
            outcome = representation_information._run_external_agent_once(
                ["synthetic"],
                "synthetic",
                1,
                lambda *_args, **_kwargs: BrokenProcess(),
            )
        self.assertEqual(outcome.failure_category, "runtime_execution_failure")
        self.assertIn(representation_information.signal.SIGTERM, signals)

    def test_unknown_communicate_error_cleans_up_real_synthetic_group(self) -> None:
        from archeos import representation_information

        child_pid_path = self.root / "runtime-error-child.pid"
        script = (
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
            "time.sleep(30)\n"
        )

        class RuntimeErrorProcess:
            def __init__(self) -> None:
                self.inner = subprocess.Popen(
                    [sys.executable, "-c", script, str(child_pid_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 1
                while not child_pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not child_pid_path.exists():
                    self.inner.kill()
                    self.inner.wait(timeout=1)
                    raise AssertionError("synthetic child did not start")
                self.first_communicate = True

            @property
            def pid(self) -> int:
                return self.inner.pid

            @property
            def returncode(self) -> int | None:
                return self.inner.returncode

            def communicate(self, **kwargs):
                if self.first_communicate:
                    self.first_communicate = False
                    raise RuntimeError("synthetic unknown communicate error")
                return self.inner.communicate(**kwargs)

        outcome = representation_information._run_external_agent_once(
            ["synthetic"],
            "synthetic",
            1,
            lambda *_args, **_kwargs: RuntimeErrorProcess(),
        )
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            child_absent = True
        else:
            child_absent = False
            os.kill(child_pid, 9)
        self.assertEqual(outcome.failure_category, "runtime_execution_failure")
        self.assertTrue(child_absent)

    def test_nonzero_permission_error_is_cleanup_failure_not_exception(self) -> None:
        from archeos import representation_information

        class NonzeroProcess:
            pid = 12345
            returncode = 7

            def communicate(self, **_kwargs):
                return "", "synthetic nonzero"

        with patch.object(
            representation_information.os, "killpg", side_effect=PermissionError
        ):
            outcome = representation_information._run_external_agent_once(
                ["synthetic"],
                "synthetic",
                1,
                lambda *_args, **_kwargs: NonzeroProcess(),
            )
        self.assertEqual(outcome.failure_category, "process_cleanup_failure")

    def test_handoff_does_not_import_world_model_or_offer_fallback(self) -> None:
        import archeos.semantic_handoff as handoff_module

        source = inspect.getsource(handoff_module)
        self.assertNotIn("world_model", source)
        self.assertNotIn("fallback_provider", source.lower())
        self.assertNotIn("fallback_model", source.lower())
        self.assertNotIn("provider_switch", source.lower())


if __name__ == "__main__":
    unittest.main()
