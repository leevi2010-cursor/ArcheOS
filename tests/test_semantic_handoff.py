from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
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
    EXTERNAL_AGENT_PROTOCOL_VERSION,
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisBatch,
    RepresentationAnalysisUnit,
    RepresentationInformationError,
    RepresentationInformationService,
    _analysis_batches,
    _canonical_fingerprint,
    _external_agent_request,
    _units_from_representation,
    external_agent_representation_analysis_schema,
    validate_representation_information_package,
)
from archeos.semantic_handoff import (
    ExternalAgentSemanticHandoffService,
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
            is_v3 = request["protocol_version"] == EXTERNAL_AGENT_PROTOCOL_VERSION
            result = {
                "protocol_version": request["protocol_version"],
                "input_fingerprint": request["input_fingerprint"],
                "anchor_accounting": [
                    {
                        "anchor_unit_id": unit["unit_id"],
                        "accounted_as": "candidate",
                    }
                    for unit in request["anchor_units"]
                ],
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
                    for item in result["anchor_accounting"]:
                        item["accounted_as"] = "residue"
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
                    for item in result["anchor_accounting"][midpoint:]:
                        item["accounted_as"] = "residue"
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
                    result["anchor_accounting"][-1]["anchor_unit_id"] = anchors[0][
                        "unit_id"
                    ]
                elif self.mode == "candidate_no_anchor":
                    result["candidates"][0]["anchor_unit_ids"] = []
                elif self.mode == "candidate_duplicate_anchor":
                    anchor_id = result["candidates"][0]["anchor_unit_ids"][0]
                    result["candidates"][0]["anchor_unit_ids"] = [
                        anchor_id,
                        anchor_id,
                    ]
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
                    result["anchor_accounting"][0]["accounted_as"] = "residue"
                elif self.mode == "accounting_missing_anchor":
                    result["anchor_accounting"] = []
                elif self.mode == "accounting_unknown_anchor":
                    result["anchor_accounting"][0]["anchor_unit_id"] = (
                        "unit_" + "f" * 64
                    )
                elif self.mode == "accounting_wrong_outcome":
                    result["anchor_accounting"][0]["accounted_as"] = "residue"
                elif self.mode == "accounting_context_only":
                    result["anchor_accounting"][0]["anchor_unit_id"] = (
                        "unit_" + "e" * 64
                    )
                result_path.write_text(json.dumps(result), encoding="utf-8")
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
        self.root = Path(self.temp.name)

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
        schema = external_agent_representation_analysis_schema(batch=batch)
        request, fingerprint = _external_agent_request(batch, result_schema=schema)
        self.assertEqual(
            request["result_schema_fingerprint"],
            _canonical_fingerprint(schema),
        )
        changed = json.loads(json.dumps(schema))
        changed["properties"]["anchor_accounting"]["maxItems"] = 39
        changed_request, changed_fingerprint = _external_agent_request(
            batch,
            result_schema=changed,
        )
        self.assertNotEqual(fingerprint, changed_fingerprint)
        self.assertNotEqual(
            request["result_schema_fingerprint"],
            changed_request["result_schema_fingerprint"],
        )

    def test_v31_schema_exactly_bounds_a_40_anchor_batch(self) -> None:
        batch = RepresentationAnalysisBatch(self.units(40), self.units(3, start=101))
        schema = external_agent_representation_analysis_schema(batch=batch)
        accounting = schema["properties"]["anchor_accounting"]
        self.assertEqual(accounting["minItems"], 40)
        self.assertEqual(accounting["maxItems"], 40)
        candidate = schema["properties"]["candidates"]["items"]["properties"]
        residue = schema["properties"]["residue"]["items"]["properties"]
        self.assertEqual(candidate["anchor_unit_ids"]["maxItems"], 40)
        self.assertEqual(residue["anchor_unit_ids"]["maxItems"], 40)
        self.assertEqual(candidate["supporting_evidence_unit_ids"]["maxItems"], 3)

    def test_v31_accepts_40_anchors_all_as_candidates(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner(),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(len(result.candidates), 40)
        self.assertEqual(result.residue, ())
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v31_accepts_40_anchors_all_as_residue(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("all_residue"),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.residue), 40)
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v31_accepts_a_mixed_40_anchor_assignment(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("mixed"),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(len(result.candidates), 20)
        self.assertEqual(len(result.residue), 20)
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v31_records_content_free_40_anchor_coverage_diagnostics(self) -> None:
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
        self.assertEqual(record.accounting_item_count, 40)
        self.assertEqual(record.candidate_anchor_ref_count, 21)
        self.assertEqual(record.residue_anchor_ref_count, 16)
        self.assertEqual(record.duplicate_anchor_ref_count, 1)
        self.assertEqual(record.duplicate_accounting_count, 1)
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

    def test_current_v31_producer_passes_the_shared_audit_validator(self) -> None:
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
                provider_version="0.147.0", runner=replay_runner
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
                provider_version="0.147.0", runner=replay_runner
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 1)
        self.assertEqual(replay_runner.calls, [])

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

    def test_replay_accepts_pre_diagnostics_processing_run_audit(self) -> None:
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
        for field in (
            "contract_failure_detail",
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
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 1)

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
        self.assertEqual(audit["duplicate_accounting_count"], 1)
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
