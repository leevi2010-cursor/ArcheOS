from __future__ import annotations

import hashlib
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
    EXTERNAL_AGENT_PROTOCOL_V3_3,
    EXTERNAL_AGENT_PROTOCOL_V3_4,
    EXTERNAL_AGENT_PROTOCOL_VERSION,
    CodexCliRepresentationAnalysisProvider,
    ExternalAgentExecutionRecord,
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationAnalysisUnit,
    RepresentationCandidateDraft,
    RepresentationInformationError,
    RepresentationInformationService,
    RepresentationResidueDraft,
    _analysis_batches,
    _canonical_fingerprint,
    _contract_failure_diagnostics,
    _external_agent_request,
    _parse_external_agent_result,
    _units_from_representation,
    external_agent_representation_analysis_schema,
    validate_representation_information_package,
)
from archeos.semantic_handoff import (
    ExternalAgentSemanticHandoffService,
    SemanticCompletedWindowBinding,
    SemanticHandoffError,
    SemanticPrivacyBinding,
    SemanticWindowAuthorityBinding,
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
_GROUPING_DIAGNOSTIC_FIELDS = (
    "raw_record_count",
    "projected_record_count",
    "duplicate_exact_body_count",
    "grouping_collision_count",
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
    def __init__(
        self,
        command,
        *,
        mode: str,
        calls: list[list[str]],
        accounting_refs: tuple[str, ...] | None = None,
    ):
        self.command = list(command)
        self.mode = mode
        self.calls = calls
        self.accounting_refs = accounting_refs
        self.pid = 99999999
        self.returncode: int | None = None
        calls.append(self.command)

    def _v33_result(self, request: dict[str, object]) -> str:
        anchor_units = request["anchor_units"]
        anchor_ids = [unit["unit_id"] for unit in anchor_units]

        def candidate(record_id: str, *, statement: str = "Synthetic statement."):
            return {
                "result_record_id": record_id,
                "statement": statement,
                "semantic_type": "observation",
                "concerns": ["Synthetic"],
                "supporting_evidence_unit_ids": [],
                "context": "Synthetic context.",
                "confidence": 0.9,
            }

        def residue(record_id: str):
            return {
                "result_record_id": record_id,
                "reason_not_absorbed": "Synthetic unresolved input.",
                "future_value_or_uncertainty": "Synthetic future value.",
            }

        anchor_results = {
            anchor_id: {
                "classification": "candidate",
                "records": [candidate(f"record_{index:032x}")],
            }
            for index, anchor_id in enumerate(anchor_ids, start=1)
        }
        result: dict[str, object] = {
            "protocol_version": request["protocol_version"],
            "input_fingerprint": request["input_fingerprint"],
            "anchor_results": anchor_results,
        }
        mode = self.mode
        if mode == "top_level_missing":
            result.pop("anchor_results")
        elif mode == "top_level_extra":
            result["unexpected"] = True
        elif mode in {"anchor_uncovered", "accounting_missing_anchor"}:
            anchor_results.pop(anchor_ids[-1])
        elif mode == "accounting_extra_anchor":
            anchor_results["unit_" + "f" * 64] = {
                "classification": "candidate",
                "records": [candidate("record_" + "f" * 32)],
            }
        elif mode in {"accounting_unknown_anchor", "accounting_context_only"}:
            value = anchor_results.pop(anchor_ids[0])
            replacement = (
                (
                    request["context_support_units"][0]["unit_id"]
                    if request["context_support_units"]
                    else "unit_" + "e" * 64
                )
                if mode == "accounting_context_only"
                else "unit_" + "f" * 64
            )
            anchor_results[replacement] = value
        elif mode in {"accounting_wrong_enum", "wrong_branch"}:
            anchor_results[anchor_ids[0]]["classification"] = "unsupported"
        elif mode == "accounting_wrong_outcome":
            anchor_results[anchor_ids[0]]["records"].append(
                dict(anchor_results[anchor_ids[0]]["records"][0])
            )
        elif mode in {"empty_records", "candidate_no_anchor"}:
            anchor_results[anchor_ids[0]]["records"] = []
        elif mode == "candidate_shape":
            anchor_results[anchor_ids[0]]["records"][0].pop("statement")
        elif mode == "candidate_semantic":
            anchor_results[anchor_ids[0]]["records"][0][
                "semantic_type"
            ] = "unsupported"
        elif mode == "candidate_confidence":
            anchor_results[anchor_ids[0]]["records"][0]["confidence"] = 2
        elif mode == "residue_shape":
            anchor_results[anchor_ids[0]] = {
                "classification": "residue",
                "records": [{"result_record_id": "record_" + "a" * 32}],
            }
        elif mode in {
            "candidate_unknown_reference",
            "candidate_anchor_as_support",
        }:
            anchor_results[anchor_ids[0]]["records"][0][
                "supporting_evidence_unit_ids"
            ] = [
                anchor_ids[0]
                if mode == "candidate_anchor_as_support"
                else "unit_" + "f" * 64
            ]
        elif mode == "residue_unknown_reference":
            value = anchor_results.pop(anchor_ids[0])
            anchor_results["unit_" + "f" * 64] = value
        elif mode in {
            "candidate_non_evidence_context",
            "candidate_evidence_context",
        }:
            anchor_results[anchor_ids[0]]["records"][0][
                "supporting_evidence_unit_ids"
            ] = [request["context_support_units"][0]["unit_id"]]
        elif mode in {"candidate_context_as_anchor", "residue_context_reference"}:
            value = anchor_results.pop(anchor_ids[0])
            anchor_results[request["context_support_units"][0]["unit_id"]] = value
        elif mode == "all_residue":
            for index, anchor_id in enumerate(anchor_ids, start=1):
                anchor_results[anchor_id] = {
                    "classification": "residue",
                    "records": [residue(f"record_{index:032x}")],
                }
        elif mode == "mixed":
            midpoint = len(anchor_ids) // 2
            for index, anchor_id in enumerate(anchor_ids[midpoint:], start=midpoint):
                anchor_results[anchor_id] = {
                    "classification": "residue",
                    "records": [residue(f"record_{index + 1:032x}")],
                }
        elif mode == "same_anchor_multiple_candidates":
            anchor_results[anchor_ids[0]]["records"].append(
                candidate("record_" + "f" * 32, statement="Second statement.")
            )
        elif mode in {"shared_candidate", "shared_candidate_reversed"}:
            shared = candidate("record_" + "e" * 32)
            for anchor_id in anchor_ids:
                anchor_results[anchor_id] = {
                    "classification": "candidate",
                    "records": [dict(shared)],
                }
            if mode == "shared_candidate_reversed":
                result["anchor_results"] = dict(
                    reversed(tuple(anchor_results.items()))
                )
        elif mode in {"candidate_duplicate_anchor", "record_duplicate_id"}:
            anchor_results[anchor_ids[0]]["records"].append(
                dict(anchor_results[anchor_ids[0]]["records"][0])
            )
        elif mode == "record_body_drift":
            shared_id = "record_" + "d" * 32
            anchor_results[anchor_ids[0]]["records"] = [candidate(shared_id)]
            anchor_results[anchor_ids[1]]["records"] = [
                candidate(shared_id, statement="Drifted statement.")
            ]
        elif mode == "record_whitespace_drift":
            shared_id = "record_" + "b" * 32
            anchor_results[anchor_ids[0]]["records"] = [candidate(shared_id)]
            anchor_results[anchor_ids[1]]["records"] = [
                candidate(shared_id, statement="Synthetic statement. ")
            ]
        elif mode in {"dual_assignment", "cross_classification_id"}:
            shared_id = "record_" + "c" * 32
            if len(anchor_ids) == 1:
                anchor_results[anchor_ids[0]]["records"].append(
                    dict(anchor_results[anchor_ids[0]]["records"][0])
                )
            else:
                anchor_results[anchor_ids[0]]["records"] = [candidate(shared_id)]
                anchor_results[anchor_ids[1]] = {
                    "classification": "residue",
                    "records": [residue(shared_id)],
                }
        elif mode == "coverage_summary":
            for anchor_id in anchor_ids[20:35]:
                anchor_results[anchor_id] = {
                    "classification": "residue",
                    "records": [residue("record_" + anchor_id[-32:])],
                }
            for anchor_id in anchor_ids[35:]:
                anchor_results[anchor_id]["records"] = []

        raw = json.dumps(result)
        if mode in {"accounting_duplicate_key", "raw_duplicate_key"}:
            key = json.dumps(anchor_ids[0])
            value = json.dumps(anchor_results[anchor_ids[0]])
            marker = f'"anchor_results": {{{key}: {value}'
            replacement = f'"anchor_results": {{{key}: {value}, {key}: {value}'
            raw = raw.replace(marker, replacement, 1)
        elif mode == "raw_nested_duplicate_key":
            marker = '"statement": "Synthetic statement."'
            raw = raw.replace(
                marker,
                marker + ', "statement": "Synthetic statement."',
                1,
            )
        return raw

    def _v34_result(self, request: dict[str, object]) -> str:
        result = json.loads(self._v33_result(request))
        for anchor_result in result.get("anchor_results", {}).values():
            for record in anchor_result.get("records", []):
                record.pop("result_record_id", None)
        raw = json.dumps(result)
        if self.mode in {"accounting_duplicate_key", "raw_duplicate_key"}:
            anchor_ids = [unit["unit_id"] for unit in request["anchor_units"]]
            anchor_results = result["anchor_results"]
            key = json.dumps(anchor_ids[0])
            value = json.dumps(anchor_results[anchor_ids[0]])
            marker = f'"anchor_results": {{{key}: {value}'
            raw = raw.replace(
                marker,
                f'"anchor_results": {{{key}: {value}, {key}: {value}',
                1,
            )
        elif self.mode == "raw_nested_duplicate_key":
            marker = '"statement": "Synthetic statement."'
            raw = raw.replace(marker, marker + ", " + marker, 1)
        return raw

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
            if request["protocol_version"] in {
                EXTERNAL_AGENT_PROTOCOL_V3_3,
                EXTERNAL_AGENT_PROTOCOL_V3_4,
            }:
                if self.mode == "invalid_json":
                    result_path.write_text("{synthetic", encoding="utf-8")
                else:
                    result_path.write_text(
                        (
                            self._v34_result(request)
                            if request["protocol_version"]
                            == EXTERNAL_AGENT_PROTOCOL_V3_4
                            else self._v33_result(request)
                        ),
                        encoding="utf-8",
                    )
                self.returncode = 0
                return "", ""
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
                if self.accounting_refs is not None:
                    result["anchor_accounting"] = [
                        {
                            "anchor_unit_id": anchor_id,
                            "accounted_as": "candidate",
                        }
                        for anchor_id in self.accounting_refs
                    ]
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
        self.assertIn("anchor_results", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        anchor_result = schema["properties"]["anchor_results"]["properties"][
            self.unit().unit_id
        ]
        candidate_properties = anchor_result["anyOf"][0]["properties"][
            "records"
        ]["items"]["properties"]
        self.assertNotIn("anchor_unit_ids", candidate_properties)
        self.assertNotIn("result_record_id", candidate_properties)
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

    def test_v33_schema_is_an_exact_per_anchor_result_map(self) -> None:
        forbidden = (
            "oneOf",
            "uniqueItems",
            "contains",
            "if",
            "then",
            "else",
            "allOf",
            "not",
        )
        for count in (1, 38, 40):
            with self.subTest(count=count):
                batch = RepresentationAnalysisBatch(
                    self.units(count), self.units(3, start=101)
                )
                schema = external_agent_representation_analysis_schema(
                    EXTERNAL_AGENT_PROTOCOL_V3_3,
                    batch=batch,
                )
                anchor_ids = [unit.unit_id for unit in batch.anchor_units]
                self.assertEqual(
                    schema["required"],
                    ["protocol_version", "input_fingerprint", "anchor_results"],
                )
                self.assertNotIn("anyOf", schema)
                anchor_results = schema["properties"]["anchor_results"]
                self.assertEqual(list(anchor_results["properties"]), anchor_ids)
                self.assertEqual(anchor_results["required"], anchor_ids)
                self.assertFalse(anchor_results["additionalProperties"])
                for anchor_id in anchor_ids:
                    branches = anchor_results["properties"][anchor_id]["anyOf"]
                    self.assertEqual(len(branches), 2)
                    self.assertEqual(
                        [
                            branch["properties"]["classification"]["const"]
                            for branch in branches
                        ],
                        ["candidate", "residue"],
                    )
                    self.assertTrue(
                        all(
                            branch["properties"]["records"]["minItems"] == 1
                            for branch in branches
                        )
                    )
                encoded = json.dumps(schema)
                for keyword in forbidden:
                    self.assertNotIn(f'"{keyword}"', encoded)
                self.assertNotIn('"dependent', encoded)

    def test_v34_schema_removes_provider_record_ids(self) -> None:
        forbidden = (
            "oneOf",
            "uniqueItems",
            "contains",
            "if",
            "then",
            "else",
            "allOf",
            "not",
        )
        for count in (1, 4, 40):
            with self.subTest(count=count):
                batch = RepresentationAnalysisBatch(
                    self.units(count), self.units(2, start=101)
                )
                schema = external_agent_representation_analysis_schema(
                    EXTERNAL_AGENT_PROTOCOL_V3_4, batch=batch
                )
                encoded = json.dumps(schema)
                self.assertNotIn("result_record_id", encoded)
                request, _ = _external_agent_request(
                    batch,
                    protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_4,
                    result_schema=schema,
                )
                self.assertNotIn("result_record_id", json.dumps(request))
                self.assertEqual(
                    schema["properties"]["anchor_results"]["required"],
                    [unit.unit_id for unit in batch.anchor_units],
                )
                for keyword in forbidden:
                    self.assertNotIn(f'"{keyword}"', encoded)
                self.assertNotIn('"dependent', encoded)

    def test_v33_fingerprint_binds_prompt_schema_and_per_anchor_contract(
        self,
    ) -> None:
        batch = RepresentationAnalysisBatch(self.units(40))
        schema = external_agent_representation_analysis_schema(
            EXTERNAL_AGENT_PROTOCOL_V3_3,
            batch=batch,
        )
        request, fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_3,
            result_schema=schema,
        )
        self.assertEqual(
            request["result_schema_fingerprint"],
            _canonical_fingerprint(schema),
        )
        self.assertIn("anchor_results", request["rules"][0])
        changed = json.loads(json.dumps(schema))
        changed["properties"]["anchor_results"]["required"].pop()
        changed_request, changed_fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_3,
            result_schema=changed,
        )
        self.assertNotEqual(fingerprint, changed_fingerprint)
        self.assertNotEqual(
            request["result_schema_fingerprint"],
            changed_request["result_schema_fingerprint"],
        )

    def test_v34_accepts_and_groups_40_exact_candidates(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner(),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.candidates[0].evidence_unit_ids), 40)
        self.assertEqual(result.residue, ())
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v34_accepts_and_groups_40_exact_residue_records(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("all_residue"),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(result.candidates, ())
        self.assertEqual(len(result.residue), 1)
        self.assertEqual(len(result.residue[0].evidence_unit_ids), 40)
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v34_accepts_a_mixed_40_anchor_assignment(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("mixed"),
        )
        result = provider.analyze(RepresentationAnalysisBatch(self.units(40)))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(len(result.residue), 1)
        self.assertEqual(provider.execution_records[0].covered_units, 40)

    def test_v34_allows_one_anchor_to_support_multiple_candidates(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("same_anchor_multiple_candidates"),
            diagnostic_root=self.root.resolve() / "collision-diagnostics",
        )
        result = provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.residue, ())
        self.assertEqual(provider.execution_records[0].covered_units, 1)

    def test_v34_projects_one_candidate_shared_by_multiple_anchors(self) -> None:
        batch = RepresentationAnalysisBatch(self.units(3))
        results = []
        for mode in ("shared_candidate", "shared_candidate_reversed"):
            provider = CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                runner=FakeRunner(mode),
            )
            results.append(provider.analyze(batch))
        result = results[0]
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.candidates[0].evidence_unit_ids,
            tuple(unit.unit_id for unit in batch.anchor_units),
        )
        self.assertEqual(result.residue, ())

    def test_v34_keeps_exactly_different_business_records_separate(self) -> None:
        for mode, expected in (
            ("record_body_drift", (2, 0)),
            ("record_whitespace_drift", (2, 0)),
            ("cross_classification_id", (1, 1)),
        ):
            with self.subTest(mode=mode):
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner(mode)
                )
                result = provider.analyze(
                    RepresentationAnalysisBatch(self.units(2))
                )
                self.assertEqual(
                    (len(result.candidates), len(result.residue)), expected
                )

    def test_v34_hash_collision_fails_closed_after_exact_byte_comparison(self) -> None:
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            runner=FakeRunner("same_anchor_multiple_candidates"),
        )
        with (
            patch(
                "archeos.representation_information._v34_record_digest",
                return_value="forced-collision",
            ),
            self.assertRaisesRegex(Exception, "未产生可验证"),
        ):
            provider.analyze(RepresentationAnalysisBatch((self.unit(),)))
        record = provider.execution_records[0]
        self.assertEqual(record.contract_failure_detail, "record_grouping")
        self.assertEqual(record.grouping_collision_count, 1)
        metadata = (
            provider.diagnostic_root
            / record.processing_run_id
            / "metadata.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(self.unit().unit_id, metadata)
        self.assertNotIn("Second statement.", metadata)
        self.assertNotIn("input_fingerprint", metadata)
        self.assertNotIn("result_fingerprint", metadata)

    def test_v34_rejects_projection_and_duplicate_key_attacks(self) -> None:
        context = replace(self.unit(), unit_id="unit_" + "e" * 64)
        cases = {
            "empty_records": "anchor_coverage",
            "wrong_branch": "anchor_accounting",
            "record_duplicate_id": "record_grouping",
            "candidate_unknown_reference": "evidence_reference",
            "accounting_context_only": "anchor_coverage",
            "raw_duplicate_key": "anchor_accounting",
            "raw_nested_duplicate_key": "top_level_schema",
        }
        for mode, detail in cases.items():
            with self.subTest(mode=mode):
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner(mode)
                )
                with self.assertRaisesRegex(Exception, "未产生可验证"):
                    provider.analyze(
                        RepresentationAnalysisBatch(self.units(2), (context,))
                    )
                record = provider.execution_records[0]
                self.assertEqual(record.failure_category, "result_contract_failure")
                self.assertEqual(record.contract_failure_detail, detail)
                if mode == "record_duplicate_id":
                    self.assertEqual(record.duplicate_exact_body_count, 1)
                    self.assertEqual(record.contract_failure_stage, "record_grouping")

    def test_v34_rejects_anchor_result_map_attacks(self) -> None:
        cases = {
            "accounting_missing_anchor": "anchor_coverage",
            "accounting_extra_anchor": "anchor_coverage",
            "accounting_unknown_anchor": "anchor_coverage",
            "accounting_wrong_outcome": "record_grouping",
            "accounting_wrong_enum": "anchor_accounting",
            "accounting_duplicate_key": "anchor_accounting",
            "dual_assignment": "record_grouping",
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

    def test_v33_rejects_context_only_accounting(self) -> None:
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

    def test_literal_v32_map_result_remains_strictly_readable(self) -> None:
        batch = RepresentationAnalysisBatch((self.unit(),))
        _, fingerprint = _external_agent_request(
            batch,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2,
        )
        payload = {
            "protocol_version": EXTERNAL_AGENT_PROTOCOL_V3_2,
            "input_fingerprint": fingerprint,
            "anchor_accounting": {self.unit().unit_id: "candidate"},
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
            EXTERNAL_AGENT_PROTOCOL_V3_2,
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
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(atomic_path),
                    root / "audits",
                )
                with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
                    self.execute_with_global_authority(
                        handoff,
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0",
                            runner=FakeRunner(mode),
                        ),
                        privacy_binding=self.privacy_binding(),
                        new_call_authority=1,
                    )
                self.assertFalse(
                    (root / "information" / representation.representation_id).exists()
                )
                self.assertFalse(atomic_path.exists())

    def test_v34_records_content_free_40_anchor_coverage_diagnostics(self) -> None:
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
        self.assertEqual(record.candidate_item_count, 20)
        self.assertEqual(record.residue_item_count, 15)
        self.assertEqual(record.accounting_item_count, 40)
        self.assertEqual(record.candidate_anchor_ref_count, 20)
        self.assertEqual(record.residue_anchor_ref_count, 15)
        self.assertEqual(record.duplicate_anchor_ref_count, 0)
        self.assertEqual(record.duplicate_accounting_count, 0)
        self.assertEqual(record.dual_assignment_count, 0)
        self.assertEqual(record.missing_anchor_count, 5)
        self.assertRegex(record.result_fingerprint or "", r"^sha256:[0-9a-f]{64}$")
        metadata_path = (
            provider.diagnostic_root / record.processing_run_id / "metadata.json"
        )
        metadata_text = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_text)
        self.assertEqual(
            metadata["diagnostic_schema_version"],
            "external-agent-diagnostics/3.0",
        )
        self.assertEqual(metadata["covered_units"], 35)
        self.assertEqual(metadata["missing_anchor_count"], 5)
        self.assertEqual(metadata["raw_record_count"], 35)
        self.assertEqual(metadata["projected_record_count"], 2)
        self.assertEqual(metadata["duplicate_exact_body_count"], 0)
        self.assertEqual(metadata["grouping_collision_count"], 0)
        self.assertNotIn("anchor_unit_ids", metadata)
        self.assertNotIn(batch.anchor_units[0].unit_id, metadata_text)
        self.assertNotIn(batch.anchor_units[0].content, metadata_text)

    def test_v34_schema_binds_anchor_and_evidence_context_enums(self) -> None:
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
        anchor_result = schema["properties"]["anchor_results"]["properties"][
            self.unit().unit_id
        ]
        candidate = anchor_result["anyOf"][0]["properties"]["records"][
            "items"
        ]["properties"]
        self.assertEqual(
            candidate["supporting_evidence_unit_ids"]["items"]["enum"],
            [evidence_context.unit_id],
        )
        residue = anchor_result["anyOf"][1]["properties"]["records"][
            "items"
        ]["properties"]
        self.assertNotIn("anchor_unit_ids", candidate)
        self.assertNotIn("anchor_unit_ids", residue)
        self.assertNotIn("uniqueItems", json.dumps(schema))

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
            "candidate_no_anchor": "anchor_coverage",
            "candidate_duplicate_anchor": "record_grouping",
            "candidate_context_as_anchor": "anchor_coverage",
            "candidate_anchor_as_support": "evidence_reference",
            "residue_context_reference": "anchor_coverage",
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

    def test_versioned_request_fingerprints_remain_exactly_readable(self) -> None:
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
            EXTERNAL_AGENT_PROTOCOL_V3_3: (
                "sha256:6091c673ed139672093e04f1aa66e4a24d7d276927cbfa0e187a410a2dd04d42"
            ),
            EXTERNAL_AGENT_PROTOCOL_V3_4: (
                "sha256:007bc856e300c98835ed0b13503d02d3c6a3ec38082952df7d1cbc6ddf079880"
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

    def test_current_v34_producer_passes_the_shared_audit_validator(self) -> None:
        representation, service = self.build_service()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        result = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
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
            "residue_unknown_reference": "anchor_coverage",
            "anchor_uncovered": "anchor_coverage",
            "accounting_missing_anchor": "anchor_coverage",
            "accounting_unknown_anchor": "anchor_coverage",
            "accounting_wrong_outcome": "record_grouping",
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

    def build_service(
        self,
        *,
        blocks: int = 1,
        root: Path | None = None,
        source_id: str = "src_" + "1" * 32,
    ):
        root = self.root if root is None else root
        root.mkdir(parents=True, exist_ok=True)
        external = root / "synthetic.txt"
        external.write_text("synthetic", encoding="utf-8")
        sources = LocalManagedSourceRepository(
            root / "managed",
            id_factory=lambda: source_id,
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

    def semantic_window_binding(
        self, *, batch_size: int = 40
    ) -> SemanticWindowAuthorityBinding:
        return SemanticWindowAuthorityBinding(
            campaign_created_at="2026-08-18T00:00:00.000Z",
            campaign_lower_cursor=(0, "", ""),
            frozen_global_upper_cursor=(100, "upper", "upper"),
            capture_provider_version="synthetic-capture-1.0",
            semantic_batch_size=batch_size,
            window_run_id="run_" + "7" * 32,
            window_plan_fingerprint="sha256:" + "8" * 64,
            window_plan_receipt_fingerprint="sha256:" + "7" * 64,
            window_after_cursor=(0, "", ""),
            window_upper_cursor=(1, "window", "window"),
            previous_checkpoint_fingerprint=None,
            completed_window_chain=(),
            reviewed_git_head="6" * 40,
        )

    def execute_with_global_authority(
        self,
        handoff: ExternalAgentSemanticHandoffService,
        representation_id: str,
        provider: CodexCliRepresentationAnalysisProvider,
        *,
        privacy_binding: SemanticPrivacyBinding,
        new_call_authority: int,
    ):
        """Keep historical recovery tests on the production durable authority path."""

        provider.timeout_seconds = 300.0
        binding = self.semantic_window_binding(
            batch_size=handoff.representation_service.batch_size
        )
        if not (
            handoff.audit_root
            / "semantic_global_authority"
            / "grant.json"
        ).is_file():
            handoff.install_global_authority(
                provider,
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=binding,
                historical_provider_versions=self.historical_provider_versions(
                    handoff.audit_root
                ),
            )
        package = handoff.representation_service.output_root / representation_id
        if not package.exists():
            preflight = handoff.recovery_preflight(
                representation_id, provider, privacy_binding, binding
            )
            if preflight.required_new_calls > new_call_authority:
                raise SemanticHandoffError(
                    "synthetic invocation allowance is insufficient"
                )
        return handoff.execute(
            representation_id,
            provider,
            privacy_binding=privacy_binding,
            authority_binding=binding,
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

    def build_historical_inventory_audit(
        self,
        root: Path,
        *,
        runner_mode: str = "success",
        blocks: int = 1,
    ):
        representation, service = self.build_service(
            root=root / "source",
            blocks=blocks,
        )
        audit_root = root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            audit_root,
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(runner_mode),
        )
        batch = _analysis_batches(
            _units_from_representation(
                representation,
                service.representation_repository,
            ),
            service.batch_size,
        )[0]
        if runner_mode == "success":
            provider.analyze(batch)
        else:
            with self.assertRaises(RepresentationInformationError):
                provider.analyze(batch)
        audit_path = handoff._persist_audits(
            provider.execution_records,
            package_published=False,
            information_ingested=False,
            durable_ingestion_status="ingestion_not_completed",
            package_fingerprint=None,
            handoff_status="failed",
        )[0]
        return representation, service, handoff, provider, batch, audit_path

    def install_historical_inventory_authority(
        self,
        handoff: ExternalAgentSemanticHandoffService,
        provider: CodexCliRepresentationAnalysisProvider,
    ) -> dict[str, object]:
        return handoff.install_global_authority(
            provider,
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=self.semantic_window_binding(),
            historical_provider_versions=self.historical_provider_versions(
                handoff.audit_root
            ),
        )

    @staticmethod
    def historical_provider_versions(audit_root: Path) -> tuple[str, ...]:
        versions: set[str] = set()
        for path in audit_root.glob("run_*/processing-run-audit.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get("provider_version")
            if isinstance(value, str):
                versions.add(value)
        for path in audit_root.glob("semantic_run_*/run-receipt.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            provider = payload.get("provider")
            value = provider.get("provider_version") if isinstance(provider, dict) else None
            if isinstance(value, str):
                attempts = path.parent / "attempts"
                if attempts.is_dir() and any(
                    json.loads(attempt.read_text(encoding="utf-8")).get(
                        "schema_version"
                    )
                    == "semantic-handoff-attempt-receipt/2.0"
                    for attempt in attempts.iterdir()
                ):
                    versions.add(value)
        return tuple(sorted(versions))

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
        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
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
        self.assertEqual(audit["diagnostic_schema_version"], "external-agent-diagnostics/3.0")
        self.assertEqual(audit["raw_record_count"], 1)
        self.assertEqual(audit["projected_record_count"], 1)
        self.assertEqual(audit["duplicate_exact_body_count"], 0)
        self.assertEqual(audit["grouping_collision_count"], 0)
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
            codex_binary=str(self.root / "absent-codex"),
            provider_version="0.147.0",
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
        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
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
        for field in (*_CONTRACT_DIAGNOSTIC_FIELDS, *_GROUPING_DIAGNOSTIC_FIELDS):
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
        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
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
        for field in (*_CONTRACT_DIAGNOSTIC_FIELDS, *_GROUPING_DIAGNOSTIC_FIELDS):
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
        self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
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
        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                runner=FakeRunner(),
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
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
        for field in (*_CONTRACT_DIAGNOSTIC_FIELDS, *_GROUPING_DIAGNOSTIC_FIELDS):
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
        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                runner=FakeRunner(),
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
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
        for field in _GROUPING_DIAGNOSTIC_FIELDS:
            historical.pop(field)
        historical["diagnostic_schema_version"] = "external-agent-diagnostics/2.0"
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
        self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
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
            *_GROUPING_DIAGNOSTIC_FIELDS,
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

        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
        self.assertEqual(first.ingestion.created, 1)
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
        self.assertEqual(replay.ingestion.existing, 1)
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
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
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
        self.assertEqual(audit["diagnostic_schema_version"], "external-agent-diagnostics/3.0")
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
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner("candidate_shape")
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
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
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        with self.assertRaisesRegex(Exception, "未确认新增 Durable"):
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    runner=FakeRunner("coverage_summary"),
                    diagnostic_root=self.root.resolve() / "diagnostics",
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        audit_path = next(audit_root.glob("*/processing-run-audit.json"))
        audit_text = audit_path.read_text(encoding="utf-8")
        audit = json.loads(audit_text)
        self.assertEqual(audit["contract_failure_stage"], "coverage")
        self.assertEqual(audit["eligible_units"], 40)
        self.assertEqual(audit["covered_units"], 35)
        self.assertEqual(audit["unaccounted_units"], 5)
        self.assertEqual(audit["duplicate_anchor_ref_count"], 0)
        self.assertEqual(audit["duplicate_accounting_count"], 0)
        self.assertEqual(audit["dual_assignment_count"], 0)
        self.assertEqual(audit["missing_anchor_count"], 5)
        self.assertRegex(audit["result_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("Synthetic business input", audit_text)

    def test_replay_rechecks_managed_source_before_store_write(self) -> None:
        representation, service = self.build_service()
        store_path = self.root / "atomic.jsonl"
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), self.root / "audits"
        )
        self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
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
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
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
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=2,
            )
        audits = sorted((self.root / "audits").glob("*/processing-run-audit.json"))
        self.assertEqual(len(provider.execution_records), 2)
        self.assertEqual(len(audits), 1)
        payloads = [json.loads(path.read_text()) for path in audits]
        failed = next(item for item in payloads if item["execution_status"] == "failed")
        self.assertEqual(failed["failure_category"], "runtime_nonzero_exit")
        self.assertEqual(failed["handoff_status"], "failed")
        committed_results = tuple(
            (self.root / "audits").glob(
                "semantic_run_*/results/batch_0001/phase-committed.json"
            )
        )
        self.assertEqual(len(committed_results), 1)
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
        units = _units_from_representation(
            representation, service.representation_repository
        )
        batches = _analysis_batches(units, service.batch_size)
        historical.analyze(batches[0])
        with self.assertRaises(RepresentationInformationError):
            historical.analyze(batches[1])
        self.assertEqual(len(historical.execution_records), 2)
        handoff._persist_audits(
            historical.execution_records,
            package_published=False,
            information_ingested=False,
            durable_ingestion_status="ingestion_not_completed",
            package_fingerprint=None,
            handoff_status="failed",
        )

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
        return self.write_v31_attempts_fixture(
            recovery, receipt, (1,), nonces={1: nonce}
        )

    def write_v31_attempts_fixture(
        self,
        recovery,
        receipt: dict[str, object],
        ordinals: tuple[int, ...],
        *,
        nonces: dict[int, str] | None = None,
    ) -> Path:
        for batch in receipt["batches"]:
            batch.pop("batch_contract_fingerprint", None)
            batch["batch_contract_fingerprint"] = _canonical_fingerprint(batch)
        receipt.pop("contract_fingerprint", None)
        receipt.pop("run_receipt_fingerprint", None)
        receipt["contract_fingerprint"] = _canonical_fingerprint(receipt)
        receipt["run_receipt_fingerprint"] = _canonical_fingerprint(receipt)
        legacy_run = recovery.audit_root / receipt["semantic_run_id"]
        legacy_attempts = legacy_run / "attempts"
        legacy_attempts.mkdir(parents=True, mode=0o700)
        os.chmod(legacy_run, 0o700)
        os.chmod(legacy_attempts, 0o700)
        (legacy_run / "run-receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        for ordinal in ordinals:
            attempt = recovery._expected_attempt_payload(
                semantic_run_id=receipt["semantic_run_id"],
                contract_fingerprint=receipt["contract_fingerprint"],
                batch_receipt=receipt["batches"][ordinal - 1],
                ordinal=ordinal,
                attempt_nonce=(
                    nonces[ordinal]
                    if nonces is not None
                    else f"{ordinal:064x}"
                ),
            )
            (legacy_attempts / f"batch_{ordinal:04d}.json").write_text(
                json.dumps(attempt), encoding="utf-8"
            )
        for path in legacy_run.rglob("*.json"):
            os.chmod(path, 0o600)
        return legacy_run

    def write_v31_result_fixture(
        self,
        recovery,
        legacy_run: Path,
        ordinal: int,
        *,
        committed: bool = False,
        protocol_version: str = EXTERNAL_AGENT_PROTOCOL_V3_1,
        provider_version: str = "0.147.0",
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        if protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_1:
            contracts = recovery.historical_v31_batch_contracts
            semantic_run_id = recovery.historical_v31_run_id
            contract_fingerprint = recovery.historical_v31_contract_fingerprint
        elif protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_2:
            contracts = recovery.historical_v32_batch_contracts
            semantic_run_id = recovery.historical_v32_run_id
            contract_fingerprint = recovery.historical_v32_contract_fingerprint
        elif protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_3:
            contracts = recovery.historical_v33_batch_contracts
            semantic_run_id = recovery.historical_v33_run_id
            contract_fingerprint = recovery.historical_v33_contract_fingerprint
        else:
            raise AssertionError("unsupported historical result fixture")
        disk_run_receipt = json.loads(
            (legacy_run / "run-receipt.json").read_text(encoding="utf-8")
        )
        semantic_run_id = disk_run_receipt["semantic_run_id"]
        contract_fingerprint = disk_run_receipt["contract_fingerprint"]
        contract = contracts[ordinal - 1]
        batch = contract["batch"]
        batch_receipt = disk_run_receipt["batches"][ordinal - 1]
        attempt = json.loads(
            (
                legacy_run / "attempts" / f"batch_{ordinal:04d}.json"
            ).read_text(encoding="utf-8")
        )
        anchor_ids = [unit.unit_id for unit in batch.anchor_units]
        payload = (
            {
                "protocol_version": protocol_version,
                "input_fingerprint": batch_receipt["input_fingerprint"],
                "anchor_results": {
                    unit_id: {
                        "classification": "candidate",
                        "records": [
                            {
                                "result_record_id": f"record_{index:032x}",
                                "statement": "Synthetic statement.",
                                "semantic_type": "observation",
                                "concerns": ["Synthetic"],
                                "supporting_evidence_unit_ids": [],
                                "context": "Synthetic context.",
                                "confidence": 0.9,
                            }
                        ],
                    }
                    for index, unit_id in enumerate(anchor_ids, start=1)
                },
            }
            if protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_3
            else {
                "protocol_version": protocol_version,
                "input_fingerprint": batch_receipt["input_fingerprint"],
                "anchor_accounting": (
                    {unit_id: "candidate" for unit_id in anchor_ids}
                    if protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_2
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
                        "anchor_unit_ids": [unit_id],
                        "supporting_evidence_unit_ids": [],
                        "context": "Synthetic context.",
                        "confidence": 0.9,
                    }
                    for unit_id in anchor_ids
                ],
                "residue": [],
            }
        )
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        processing_run_id = f"run_{ordinal:032x}"
        record = ExternalAgentExecutionRecord(
            processing_run_id=processing_run_id,
            protocol_version=protocol_version,
            input_fingerprint=batch_receipt["input_fingerprint"],
            anchor_unit_ids=tuple(anchor_ids),
            provider_route="codex-cli",
            provider_version=provider_version,
            model="gpt-5.6-terra",
            reasoning_effort="medium",
            fallback_policy="none",
            started_at="2026-08-18T00:00:00.000Z",
            finished_at="2026-08-18T00:00:01.000Z",
            execution_status="succeeded",
            failure_category=None,
            contract_failure_detail=None,
            strict_validation_status="passed",
            result_fingerprint=handoff_module._bytes_fingerprint(raw),
            eligible_units=len(anchor_ids),
            covered_units=len(anchor_ids),
            contract_failure_stage=None,
            candidate_item_count=0,
            residue_item_count=0,
            accounting_item_count=0,
            candidate_anchor_ref_count=0,
            residue_anchor_ref_count=0,
            duplicate_anchor_ref_count=0,
            duplicate_accounting_count=0,
            dual_assignment_count=0,
            missing_anchor_count=0,
            unknown_anchor_ref_count=0,
            raw_record_count=0,
            projected_record_count=0,
            duplicate_exact_body_count=0,
            grouping_collision_count=0,
            diagnostic_schema_version="external-agent-diagnostics/2.0",
            elapsed_ms=1000,
            deadline_ms=300000,
            exit_code=0,
            termination_signal=None,
            timeout_phase=None,
            provider_error_category=None,
            result_file_present=True,
            result_size_bytes=len(raw),
            stdout_bytes=0,
            stderr_bytes=0,
            process_cleanup_status="verified",
        )
        historical_execution_record = handoff_module._record_payload(record)
        for field in _GROUPING_DIAGNOSTIC_FIELDS:
            historical_execution_record.pop(field)
        without_fingerprint = {
            "schema_version": "semantic-handoff-batch-result-receipt/2.0",
            "artifact_kind": "semantic_handoff_batch_result",
            "semantic_run_id": semantic_run_id,
            "run_contract_fingerprint": contract_fingerprint,
            "batch_ordinal": ordinal,
            "batch_contract_fingerprint": batch_receipt[
                "batch_contract_fingerprint"
            ],
            "attempt_id": attempt["attempt_id"],
            "attempt_nonce": attempt["attempt_nonce"],
            "attempt_receipt_fingerprint": attempt[
                "attempt_receipt_fingerprint"
            ],
            "processing_run_id": processing_run_id,
            "result_sha256": handoff_module._bytes_fingerprint(raw),
            "result_size_bytes": len(raw),
            "strict_validation_status": "passed",
            "result_readback_status": "verified",
            "process_cleanup_status": "verified",
            "execution_record": historical_execution_record,
        }
        result_receipt = {
            **without_fingerprint,
            "result_receipt_fingerprint": handoff_module._fingerprint(
                without_fingerprint
            ),
        }
        result = legacy_run / "results" / f"batch_{ordinal:04d}"
        result.mkdir(parents=True, mode=0o700)
        os.chmod(result.parent, 0o700)
        os.chmod(result, 0o700)
        payloads = {
            "result-receipt.json": result_receipt,
            "phase-post-strict-pending.json": (
                recovery._expected_result_phase_payload(
                    semantic_run_id=semantic_run_id,
                    contract_fingerprint=contract_fingerprint,
                    batch_receipt=batch_receipt,
                    ordinal=ordinal,
                    result_receipt=result_receipt,
                    phase="post_strict_pending",
                )
            ),
        }
        if committed:
            payloads["phase-committed.json"] = (
                recovery._expected_result_phase_payload(
                    semantic_run_id=semantic_run_id,
                    contract_fingerprint=contract_fingerprint,
                    batch_receipt=batch_receipt,
                    ordinal=ordinal,
                    result_receipt=result_receipt,
                    phase="committed",
                )
            )
        (result / "result.json").write_bytes(raw)
        for name, payload in payloads.items():
            (result / name).write_text(json.dumps(payload), encoding="utf-8")
        for path in result.iterdir():
            os.chmod(path, 0o600)

    def build_linked_v31_inventory_fixture(
        self,
        root: Path,
        *,
        provider_version: str = "0.146.0",
    ):
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(
            root=root / "source",
            blocks=1,
        )
        audit_root = root / "audits"
        audit_root.mkdir(parents=True, mode=0o700)
        os.chmod(audit_root, 0o700)
        current_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        recovery = handoff_module._SemanticRecoveryRun(
            service,
            audit_root,
            representation.representation_id,
            current_provider,
            self.privacy_binding(),
        )
        receipt = json.loads(
            json.dumps(recovery.expected_historical_v31_run_receipt)
        )
        receipt["provider"]["provider_version"] = provider_version
        legacy_run = self.write_v31_attempt_fixture(recovery, receipt)
        self.write_v31_result_fixture(
            recovery,
            legacy_run,
            1,
            committed=True,
            provider_version=provider_version,
        )
        result_receipt = json.loads(
            (
                legacy_run
                / "results"
                / "batch_0001"
                / "result-receipt.json"
            ).read_text(encoding="utf-8")
        )
        record = handoff_module._record_from_payload(
            result_receipt["execution_record"]
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            audit_root,
        )
        record_payload = handoff_module._record_payload(record)
        for field in _GROUPING_DIAGNOSTIC_FIELDS:
            record_payload.pop(field)
        audit_payload = {
            "schema_version": "processing-run-audit/1.0",
            "artifact_kind": "processing_run_audit",
            **record_payload,
            "unaccounted_units": 0,
            "result_readback_status": "verified",
            "package_published": True,
            "package_fingerprint": "sha256:" + "9" * 64,
            "information_ingested": False,
            "durable_ingestion_status": "pending",
            "handoff_status": "pending",
            "audit_readback_status": "verified",
        }
        audit_directory = audit_root / record.processing_run_id
        audit_directory.mkdir(mode=0o700)
        audit_path = audit_directory / "processing-run-audit.json"
        audit_path.write_text(json.dumps(audit_payload), encoding="utf-8")
        os.chmod(audit_path, 0o600)
        handoff_module._validate_versioned_processing_run_audit(audit_path)
        return (
            representation,
            service,
            handoff,
            current_provider,
            recovery,
            legacy_run,
            audit_path,
        )

    def test_v31_attempt_is_counted_preserved_and_isolated_from_v34_run(
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

        result = self.execute_with_global_authority(handoff,
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

    def test_v32_attempt_is_counted_and_isolated_from_v34_run(self) -> None:
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
        self.assertNotEqual(
            recovery.historical_v32_run_id, recovery.semantic_run_id
        )
        receipt = json.loads(
            json.dumps(recovery.expected_historical_v32_run_receipt)
        )
        legacy_run = self.write_v31_attempt_fixture(recovery, receipt)
        before = self.tree_snapshot(legacy_run)

        preflight = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).recovery_preflight(
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.historical_counted_attempts, 1)
        self.assertEqual(preflight.replayable_batches, 0)
        self.assertEqual(preflight.required_new_calls, 1)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.tree_snapshot(legacy_run), before)
        self.assertFalse((audit_root / recovery.semantic_run_id).exists())
        self.assertFalse((self.root / "atomic.jsonl").exists())

    def test_v32_result_recovery_remains_zero_call_readable(self) -> None:
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
            json.dumps(recovery.expected_historical_v32_run_receipt)
        )
        legacy_run = self.write_v31_attempt_fixture(recovery, receipt)
        incomplete_preflight = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).recovery_preflight(
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        self.assertEqual(incomplete_preflight.historical_counted_attempts, 1)
        self.assertEqual(incomplete_preflight.replayable_batches, 0)
        self.assertEqual(incomplete_preflight.required_new_calls, 1)
        self.assertEqual(runner.calls, [])
        self.write_v31_result_fixture(
            recovery,
            legacy_run,
            1,
            committed=True,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_2,
        )
        before = self.tree_snapshot(legacy_run)

        preflight = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).recovery_preflight(
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.historical_counted_attempts, 1)
        self.assertEqual(preflight.replayable_batches, 0)
        self.assertEqual(preflight.required_new_calls, 1)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.tree_snapshot(legacy_run), before)

    def test_v33_attempt_and_result_remain_counted_but_isolated_from_v34(self) -> None:
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
        self.assertNotEqual(recovery.historical_v33_run_id, recovery.semantic_run_id)
        receipt = json.loads(
            json.dumps(recovery.expected_historical_v33_run_receipt)
        )
        legacy_run = self.write_v31_attempt_fixture(recovery, receipt)
        incomplete_preflight = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).recovery_preflight(
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        self.assertEqual(incomplete_preflight.historical_counted_attempts, 1)
        self.assertEqual(incomplete_preflight.replayable_batches, 0)
        self.assertEqual(incomplete_preflight.required_new_calls, 1)
        self.assertEqual(runner.calls, [])
        self.write_v31_result_fixture(
            recovery,
            legacy_run,
            1,
            committed=True,
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_3,
        )
        before = self.tree_snapshot(legacy_run)
        preflight = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        ).recovery_preflight(
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.historical_counted_attempts, 1)
        self.assertEqual(preflight.replayable_batches, 0)
        self.assertEqual(preflight.required_new_calls, 1)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.tree_snapshot(legacy_run), before)
        self.assertFalse((audit_root / recovery.semantic_run_id).exists())

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
                self.write_v31_result_fixture(
                    recovery, legacy_run, 1, committed=committed
                )
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

    def test_v31_version_specific_sequence_causality_matrix(self) -> None:
        import archeos.semantic_handoff as handoff_module

        cases = (
            ("no_attempts", 41, (), (), True, 0),
            ("attempt_1", 41, (1,), (), True, 1),
            ("attempt_1_result_1", 41, (1,), (1,), True, 1),
            ("attempt_2_result_1", 41, (1, 2), (1,), True, 2),
            ("attempt_2_result_2", 41, (1, 2), (1, 2), True, 2),
            ("attempt_2_no_results", 41, (1, 2), (), False, 0),
            ("attempt_3_only_result_1", 81, (1, 2, 3), (1,), False, 0),
            ("attempt_gap", 81, (1, 3), (1,), False, 0),
            ("result_without_attempt", 41, (1,), (1,), False, 0),
        )
        for name, blocks, attempts, results, accepted, expected_count in cases:
            with self.subTest(case=name):
                root = self.root / name
                representation, service = self.build_service(
                    blocks=blocks, root=root
                )
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
                legacy_run = self.write_v31_attempts_fixture(
                    recovery, receipt, attempts
                )
                for ordinal in results:
                    self.write_v31_result_fixture(
                        recovery, legacy_run, ordinal, committed=True
                    )
                if name == "result_without_attempt":
                    (
                        legacy_run / "attempts" / "batch_0001.json"
                    ).unlink()
                before = self.tree_snapshot(audit_root)
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    audit_root,
                )
                if accepted:
                    preflight = handoff.recovery_preflight(
                        representation.representation_id,
                        provider,
                        self.privacy_binding(),
                    )
                    self.assertEqual(
                        preflight.historical_counted_attempts,
                        expected_count,
                    )
                else:
                    with self.assertRaises(SemanticHandoffError):
                        handoff.recovery_preflight(
                            representation.representation_id,
                            provider,
                            self.privacy_binding(),
                        )
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.tree_snapshot(audit_root), before)
                self.assertFalse((root / "atomic.jsonl").exists())

    def test_v34_recovery_identity_is_stable_and_binds_execution_contract(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(blocks=41)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
        self.assertEqual(identity["protocol_version"], EXTERNAL_AGENT_PROTOCOL_V3_4)
        self.assertNotEqual(first.semantic_run_id, first.historical_v33_run_id)
        self.assertEqual(
            identity["local_validator_contract_version"],
            "external-agent-local-validator/3.4",
        )
        self.assertEqual(
            identity["prompt_template_fingerprint"],
            handoff_module._fingerprint(
                handoff_module._external_agent_prompt(
                    {
                        "protocol_version": EXTERNAL_AGENT_PROTOCOL_V3_4,
                        "template_probe": True,
                    }
                )
            ),
        )
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
                            provider_version="0.147.0", timeout_seconds=300, runner=runner
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
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=runner
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
                provider_version="0.147.0", timeout_seconds=300, runner=next_runner
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
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        self.execute_with_global_authority(handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
            "semantic-handoff-attempt-receipt/3.0",
        )
        self.assertEqual(attempt["global_ordinal"], 81)
        self.assertEqual(attempt["state"], "consumed")
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
                    self.execute_with_global_authority(handoff,
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", timeout_seconds=300, runner=runner
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
                        provider_version="0.147.0", timeout_seconds=300, runner=next_runner
                    ),
                    self.privacy_binding(),
                )
                self.assertEqual(preflight.replayable_batches, 1)
                self.assertEqual(preflight.required_new_calls, 0)
                self.assertEqual(preflight.conservatively_counted_attempts, 0)
                result = self.execute_with_global_authority(handoff,
                    representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.147.0", timeout_seconds=300, runner=next_runner
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
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
                    provider_version="0.147.0", timeout_seconds=300, runner=next_runner
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
                provider_version="0.147.0", timeout_seconds=300, runner=next_runner
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
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=first_runner
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
            provider_version="0.147.0", timeout_seconds=300, runner=resume_runner
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            resume_provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 1)
        self.assertEqual(preflight.required_new_calls, 2)
        result = self.execute_with_global_authority(handoff,
            representation.representation_id,
            resume_provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=2,
        )
        self.assertEqual(len(resume_runner.calls), 2)
        self.assertEqual(result.ingestion.created, 3)

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
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )
        with self.assertRaises(SemanticHandoffError):
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=runner
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
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )
        with (
            patch.object(
                information_module,
                "_output_records",
                mutate_after_final_disk_reload,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=runner
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
                    self.execute_with_global_authority(handoff,
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
                    self.execute_with_global_authority(handoff,
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
                    self.execute_with_global_authority(handoff,
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
                            provider_version="0.147.0", timeout_seconds=300, runner=next_runner
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
                        self.execute_with_global_authority(handoff,
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
                    provider_version="0.147.0", timeout_seconds=300, runner=next_runner
                )
                if boundary in convergable:
                    preflight = handoff.recovery_preflight(
                        representation.representation_id,
                        next_provider,
                        self.privacy_binding(),
                    )
                    self.assertEqual(preflight.replayable_batches, 1)
                    self.assertEqual(preflight.required_new_calls, 0)
                    result = self.execute_with_global_authority(handoff,
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
                        self.execute_with_global_authority(handoff,
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
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
            self.execute_with_global_authority(handoff,
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
            provider_version="0.147.0", timeout_seconds=300, runner=resume_runner
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            resume_provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 1)
        self.assertEqual(preflight.required_new_calls, 2)
        result = self.execute_with_global_authority(handoff,
            representation.representation_id,
            resume_provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=2,
        )
        self.assertEqual(len(resume_runner.calls), 2)
        self.assertEqual(result.ingestion.created, 3)
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
            self.execute_with_global_authority(handoff,
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
            provider_version="0.147.0", timeout_seconds=300, runner=next_runner
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
            self.execute_with_global_authority(handoff,
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
            "valid": (1, 0),
            "all_residue": (0, 1),
            "mixed": (1, 1),
        }
        for mode, expected in expected_counts.items():
            with self.subTest(mode=mode):
                root = self.root / mode
                representation, service = self.build_service(blocks=40, root=root)
                runner = FakeRunner(mode)
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    root / "audits",
                )
                result = self.execute_with_global_authority(handoff,
                    representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.147.0", timeout_seconds=300, runner=runner
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
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=first_runner
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
                provider_version="0.147.0", timeout_seconds=300, runner=replay_runner
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
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=replay_runner
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
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertEqual(runner.calls, 1)
        next_runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "LEAD_DECISION_REQUIRED"):
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=next_runner
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
                    self.execute_with_global_authority(handoff,
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
        with self.assertRaisesRegex(Exception, "global authority"):
            ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                audit_root,
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=runner
                ),
                privacy_binding=self.privacy_binding(),
            )
        self.assertEqual(runner.calls, [])
        self.assertFalse(audit_root.exists())

    def test_direct_new_call_without_grant_is_zero_write(self) -> None:
        representation, service = self.build_service(
            root=self.root / "direct-no-grant"
        )
        audit_root = self.root / "direct-no-grant-audits"
        runner = FakeRunner()
        before = self.tree_snapshot(self.root)
        with self.assertRaisesRegex(SemanticHandoffError, "direct/unbound"):
            ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "direct-no-grant.jsonl"),
                audit_root,
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                ),
            )
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.tree_snapshot(self.root), before)
        self.assertFalse(audit_root.exists())

    def test_invalid_global_authority_install_is_zero_write(self) -> None:
        cases = (
            (79, 20, 100, 300, self.semantic_window_binding()),
            (80, 21, 100, 300, self.semantic_window_binding()),
            (80, 20, 101, 300, self.semantic_window_binding()),
            (80, 20, 100, 299, self.semantic_window_binding()),
            (
                80,
                20,
                100,
                300,
                replace(self.semantic_window_binding(), reviewed_git_head="invalid"),
            ),
        )
        for index, (expected, new, cap, timeout, binding) in enumerate(cases):
            with self.subTest(index=index):
                root = self.root / f"invalid-install-{index}"
                representation, service = self.build_service(root=root)
                audit_root = root / "audits"
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
                    audit_root,
                )
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=timeout,
                    runner=FakeRunner(),
                )
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    handoff.install_global_authority(
                        provider,
                        authority_ref="sha256:" + "5" * 64,
                        expected_total=expected,
                        max_new=new,
                        absolute_cap=cap,
                        window_binding=binding,
                    )
                self.assertEqual(self.tree_snapshot(root), before)
                self.assertFalse(audit_root.exists())
                self.assertFalse(
                    (service.output_root / representation.representation_id).exists()
                )

    def test_global_authority_freezes_historical_audit_inventory(self) -> None:
        root = self.root / "historical-inventory"
        audit_root = root / "audits"
        representation, service = self.build_service(root=root / "first")
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "first.jsonl"),
            audit_root,
        )
        historical = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        batch = _analysis_batches(
            _units_from_representation(
                representation, service.representation_repository
            ),
            service.batch_size,
        )[0]
        historical.analyze(batch)
        handoff._persist_audits(
            historical.execution_records,
            package_published=False,
            information_ingested=False,
            durable_ingestion_status="ingestion_not_completed",
            package_fingerprint=None,
            handoff_status="failed",
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        grant = handoff.install_global_authority(
            provider,
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=self.semantic_window_binding(),
            historical_provider_versions=("0.147.0",),
        )
        self.assertEqual(grant["legacy_attempt_inventory_count"], 1)
        self.assertEqual(grant["external_prior_count"], 79)

        drift = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        drift.analyze(batch)
        handoff._persist_audits(
            drift.execution_records,
            package_published=False,
            information_ingested=False,
            durable_ingestion_status="ingestion_not_completed",
            package_fingerprint=None,
            handoff_status="failed",
        )
        next_representation, next_service = self.build_service(
            root=root / "next", source_id="src_" + "2" * 32
        )
        next_runner = FakeRunner()
        before_reject = self.tree_snapshot(audit_root)
        with self.assertRaisesRegex(SemanticHandoffError, "binding"):
            ExternalAgentSemanticHandoffService(
                next_service,
                JsonlAtomicInformationStore(root / "next.jsonl"),
                audit_root,
            ).execute(
                next_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=next_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=self.semantic_window_binding(),
            )
        self.assertEqual(next_runner.calls, [])
        self.assertEqual(self.tree_snapshot(audit_root), before_reject)

    def test_global_authority_accepts_mixed_historical_provider_provenance(
        self,
    ) -> None:
        root = self.root / "mixed-historical-provider"
        (
            representation,
            service,
            handoff,
            current_provider,
            _recovery,
            _legacy_run,
            _linked_audit,
        ) = self.build_linked_v31_inventory_fixture(root)
        historical = CodexCliRepresentationAnalysisProvider(
            provider_version="0.145.0",
            timeout_seconds=300,
            runner=FakeRunner("nonzero"),
        )
        batch = _analysis_batches(
            _units_from_representation(
                representation,
                service.representation_repository,
            ),
            service.batch_size,
        )[0]
        with self.assertRaises(RepresentationInformationError):
            historical.analyze(batch)
        handoff._persist_audits(
            historical.execution_records,
            package_published=False,
            information_ingested=False,
            durable_ingestion_status="ingestion_not_completed",
            package_fingerprint=None,
            handoff_status="failed",
        )

        grant = self.install_historical_inventory_authority(
            handoff,
            current_provider,
        )
        self.assertEqual(grant["legacy_attempt_inventory_count"], 2)
        self.assertEqual(grant["external_prior_count"], 78)
        self.assertEqual(
            grant["historical_provider_versions"],
            ["0.145.0", "0.146.0"],
        )
        self.assertEqual(
            grant["historical_provider_version_counts"],
            {"0.145.0": 1, "0.146.0": 1},
        )
        self.assertEqual(
            grant["contract"]["provider"]["provider_version"],
            "0.147.0",
        )
        replay = self.install_historical_inventory_authority(
            handoff,
            current_provider,
        )
        self.assertEqual(
            replay["legacy_attempt_inventory_fingerprint"],
            grant["legacy_attempt_inventory_fingerprint"],
        )

        before = self.tree_snapshot(root)
        with self.assertRaises(SemanticHandoffError):
            self.install_historical_inventory_authority(
                handoff,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.148.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                ),
            )
        self.assertEqual(self.tree_snapshot(root), before)

    def test_global_authority_requires_exact_approved_historical_version_set(
        self,
    ) -> None:
        for name, approved, succeeds, error in (
            ("exact", ("0.146.0",), True, ""),
            ("missing", (), False, "version 集合不匹配"),
            ("extra", ("0.146.0", "999"), False, "version 集合不匹配"),
            ("unsafe", ("../unsafe", "0.146.0"), False, "授权无效"),
        ):
            with self.subTest(name=name):
                root = self.root / f"historical-version-set-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _recovery,
                    _legacy_run,
                    _linked_audit,
                ) = self.build_linked_v31_inventory_fixture(root)
                before = self.tree_snapshot(root)
                if succeeds:
                    grant = handoff.install_global_authority(
                        provider,
                        authority_ref="sha256:" + "5" * 64,
                        expected_total=80,
                        max_new=20,
                        absolute_cap=100,
                        window_binding=self.semantic_window_binding(),
                        historical_provider_versions=approved,
                    )
                    self.assertEqual(
                        grant["historical_provider_versions"], list(approved)
                    )
                    self.assertEqual(
                        grant["historical_provider_version_counts"],
                        {"0.146.0": 1},
                    )
                else:
                    with self.assertRaisesRegex(
                        SemanticHandoffError, error
                    ):
                        handoff.install_global_authority(
                            provider,
                            authority_ref="sha256:" + "5" * 64,
                            expected_total=80,
                            max_new=20,
                            absolute_cap=100,
                            window_binding=self.semantic_window_binding(),
                            historical_provider_versions=approved,
                        )
                    self.assertEqual(self.tree_snapshot(root), before)
                    self.assertFalse(
                        (handoff.audit_root / "semantic_global_authority").exists()
                    )

    def test_global_authority_binds_verified_current_codex_executable(
        self,
    ) -> None:
        root = self.root / "verified-codex-executable"
        executable = root / "bin" / "codex"
        executable.parent.mkdir(parents=True, mode=0o700)
        executable.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  echo 'codex-cli 0.147.0'\n"
            "  exit 0\n"
            "fi\n"
            "exit 99\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        _representation, service = self.build_service(root=root / "source")
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            root / "audits",
        )
        provider = CodexCliRepresentationAnalysisProvider(
            codex_binary=str(executable),
            provider_version="0.147.0",
            timeout_seconds=300,
        )
        grant = handoff.install_global_authority(
            provider,
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=self.semantic_window_binding(),
        )
        contract = grant["contract"]
        self.assertEqual(contract["provider"]["provider_version"], "0.147.0")
        self.assertRegex(contract["provider_binary_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(str(executable), json.dumps(grant))
        self.assertEqual(provider.provider_start_count, 0)
        executable.write_text(
            executable.read_text(encoding="utf-8") + "# changed\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        with self.assertRaisesRegex(
            RepresentationInformationError, "identity drifted"
        ):
            handoff.execute(
                _representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                authority_binding=self.semantic_window_binding(),
            )
        self.assertEqual(provider.provider_start_count, 0)
        self.assertEqual(list(handoff.audit_root.glob("semantic_run_*")), [])

        mismatch_root = self.root / "verified-codex-mismatch"
        _representation, mismatch_service = self.build_service(
            root=mismatch_root / "source"
        )
        mismatch = ExternalAgentSemanticHandoffService(
            mismatch_service,
            JsonlAtomicInformationStore(mismatch_root / "atomic.jsonl"),
            mismatch_root / "audits",
        )
        before = self.tree_snapshot(mismatch_root)
        with self.assertRaisesRegex(
            RepresentationInformationError, "approved assertion"
        ):
            mismatch.install_global_authority(
                CodexCliRepresentationAnalysisProvider(
                    codex_binary=str(executable),
                    provider_version="999",
                    timeout_seconds=300,
                ),
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=self.semantic_window_binding(),
            )
        self.assertEqual(self.tree_snapshot(mismatch_root), before)

        unsafe_root = self.root / "unsafe-codex-executable"
        unsafe_bin = unsafe_root / "world-writable" / "codex"
        unsafe_bin.parent.mkdir(parents=True, mode=0o700)
        unsafe_bin.write_text(
            "#!/bin/sh\necho 'codex-cli 0.147.0'\n", encoding="utf-8"
        )
        unsafe_bin.chmod(0o700)
        unsafe_bin.parent.chmod(0o777)
        _representation, unsafe_service = self.build_service(
            root=unsafe_root / "source"
        )
        unsafe_handoff = ExternalAgentSemanticHandoffService(
            unsafe_service,
            JsonlAtomicInformationStore(unsafe_root / "atomic.jsonl"),
            unsafe_root / "audits",
        )
        unsafe_before = self.tree_snapshot(unsafe_root)
        with self.assertRaisesRegex(
            RepresentationInformationError, "path is unsafe"
        ):
            unsafe_handoff.install_global_authority(
                CodexCliRepresentationAnalysisProvider(
                    codex_binary=str(unsafe_bin),
                    provider_version="0.147.0",
                    timeout_seconds=300,
                ),
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=self.semantic_window_binding(),
            )
        self.assertEqual(self.tree_snapshot(unsafe_root), unsafe_before)

    def test_global_authority_rejects_unbound_and_linked_audit_shadows(
        self,
    ) -> None:
        root = self.root / "unbound-audit-shadow"
        (
            _representation,
            _service,
            handoff,
            provider,
            _batch,
            audit_path,
        ) = self.build_historical_inventory_audit(root, runner_mode="nonzero")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        clone_id = "run_" + "e" * 32
        audit["processing_run_id"] = clone_id
        clone_dir = handoff.audit_root / clone_id
        clone_dir.mkdir(mode=0o700)
        clone_path = clone_dir / "processing-run-audit.json"
        clone_path.write_text(json.dumps(audit), encoding="utf-8")
        clone_path.chmod(0o600)
        before = self.tree_snapshot(root)
        with self.assertRaisesRegex(SemanticHandoffError, "shadow"):
            handoff.install_global_authority(
                provider,
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=self.semantic_window_binding(),
                historical_provider_versions=("0.147.0",),
            )
        self.assertEqual(self.tree_snapshot(root), before)

        linked_root = self.root / "linked-audit-shadow"
        (
            _representation,
            _service,
            linked_handoff,
            linked_provider,
            _recovery,
            _legacy_run,
            linked_audit,
        ) = self.build_linked_v31_inventory_fixture(linked_root)
        linked_payload = json.loads(linked_audit.read_text(encoding="utf-8"))
        linked_clone_id = "run_" + "d" * 32
        linked_payload["processing_run_id"] = linked_clone_id
        linked_clone_dir = linked_handoff.audit_root / linked_clone_id
        linked_clone_dir.mkdir(mode=0o700)
        linked_clone_path = linked_clone_dir / "processing-run-audit.json"
        linked_clone_path.write_text(json.dumps(linked_payload), encoding="utf-8")
        linked_clone_path.chmod(0o600)
        linked_before = self.tree_snapshot(linked_root)
        with self.assertRaisesRegex(SemanticHandoffError, "shadow"):
            linked_handoff.install_global_authority(
                linked_provider,
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=self.semantic_window_binding(),
                historical_provider_versions=("0.146.0",),
            )
        self.assertEqual(self.tree_snapshot(linked_root), linked_before)

    def test_global_authority_exactly_deduplicates_linked_failed_audit(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        root = self.root / "linked-failed-historical"
        representation, service = self.build_service(
            root=root / "source",
            blocks=1,
        )
        audit_root = root / "audits"
        audit_root.mkdir(parents=True, mode=0o700)
        os.chmod(audit_root, 0o700)
        current_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        recovery = handoff_module._SemanticRecoveryRun(
            service,
            audit_root,
            representation.representation_id,
            current_provider,
            self.privacy_binding(),
        )
        receipt = json.loads(
            json.dumps(recovery.expected_historical_v31_run_receipt)
        )
        receipt["provider"]["provider_version"] = "0.146.0"
        self.write_v31_attempt_fixture(recovery, receipt)
        batch = recovery.historical_v31_batch_contracts[0]["batch"]
        historical = CodexCliRepresentationAnalysisProvider(
            provider_version="0.146.0",
            timeout_seconds=300,
            runner=FakeRunner("nonzero"),
        )
        with self.assertRaises(RepresentationInformationError):
            historical.analyze(batch)
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            audit_root,
        )
        audit_path = handoff._persist_audits(
            historical.execution_records,
            package_published=False,
            information_ingested=False,
            durable_ingestion_status="ingestion_not_completed",
            package_fingerprint=None,
            handoff_status="failed",
        )[0]
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V3_1
        audit["input_fingerprint"] = receipt["batches"][0][
            "input_fingerprint"
        ]
        audit["diagnostic_schema_version"] = (
            "external-agent-diagnostics/2.0"
        )
        for field in _GROUPING_DIAGNOSTIC_FIELDS:
            audit.pop(field)
        audit_path.write_text(json.dumps(audit), encoding="utf-8")

        grant = self.install_historical_inventory_authority(
            handoff,
            current_provider,
        )
        self.assertEqual(grant["legacy_attempt_inventory_count"], 1)
        self.assertEqual(grant["external_prior_count"], 79)

    def test_global_authority_rejects_linked_historical_provenance_drift(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        for attack in (
            "audit-provider",
            "audit-profile",
            "audit-protocol",
            "audit-deadline",
            "audit-input",
            "result-provider",
            "result-cleanup",
            "result-phase",
        ):
            with self.subTest(attack=attack):
                root = self.root / f"linked-historical-{attack}"
                (
                    _representation,
                    _service,
                    handoff,
                    current_provider,
                    recovery,
                    legacy_run,
                    audit_path,
                ) = self.build_linked_v31_inventory_fixture(root)
                if attack.startswith("audit-"):
                    audit = json.loads(audit_path.read_text(encoding="utf-8"))
                    field, value = {
                        "audit-provider": ("provider_version", "0.145.0"),
                        "audit-profile": ("model", "gpt-5.6-sol"),
                        "audit-protocol": (
                            "protocol_version",
                            EXTERNAL_AGENT_PROTOCOL_V3_2,
                        ),
                        "audit-deadline": ("deadline_ms", 299000),
                        "audit-input": (
                            "input_fingerprint",
                            "sha256:" + "0" * 64,
                        ),
                    }[attack]
                    audit[field] = value
                    audit_path.write_text(json.dumps(audit), encoding="utf-8")
                elif attack in {"result-provider", "result-cleanup"}:
                    result = legacy_run / "results" / "batch_0001"
                    receipt_path = result / "result-receipt.json"
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    record = receipt["execution_record"]
                    if attack == "result-provider":
                        record["provider_version"] = "0.145.0"
                    else:
                        record["process_cleanup_status"] = "failed"
                    receipt.pop("result_receipt_fingerprint")
                    receipt["result_receipt_fingerprint"] = (
                        handoff_module._fingerprint(receipt)
                    )
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                    run_receipt = json.loads(
                        (legacy_run / "run-receipt.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    batch_receipt = run_receipt["batches"][0]
                    for phase in ("post_strict_pending", "committed"):
                        phase_path = result / (
                            "phase-post-strict-pending.json"
                            if phase == "post_strict_pending"
                            else "phase-committed.json"
                        )
                        phase_path.write_text(
                            json.dumps(
                                recovery._expected_result_phase_payload(
                                    semantic_run_id=legacy_run.name,
                                    contract_fingerprint=run_receipt[
                                        "contract_fingerprint"
                                    ],
                                    batch_receipt=batch_receipt,
                                    ordinal=1,
                                    result_receipt=receipt,
                                    phase=phase,
                                )
                            ),
                            encoding="utf-8",
                        )
                else:
                    phase_path = (
                        legacy_run
                        / "results"
                        / "batch_0001"
                        / "phase-committed.json"
                    )
                    phase = json.loads(phase_path.read_text(encoding="utf-8"))
                    phase["result_sha256"] = "sha256:" + "0" * 64
                    phase_path.write_text(json.dumps(phase), encoding="utf-8")

                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(
                        handoff,
                        current_provider,
                    )
                self.assertEqual(self.tree_snapshot(root), before)

    def test_global_authority_inventory_accepts_producer_success_and_failure(
        self,
    ) -> None:
        cases = (
            ("success", "success", None),
            (
                "runtime-start",
                "no_result",
                lambda audit: audit.update(
                    failure_category="runtime_start_failure",
                    exit_code=None,
                    process_cleanup_status="not_started",
                ),
            ),
            (
                "runtime-preflight",
                "no_result",
                lambda audit: audit.update(
                    failure_category="runtime_execution_failure",
                    exit_code=None,
                    process_cleanup_status="not_started",
                ),
            ),
            ("runtime", "nonzero", None),
            ("timeout", "timeout", None),
            ("contract", "candidate_shape", None),
            ("no-result", "no_result", None),
            ("invalid-json", "invalid_json", None),
            ("binding", "wrong_binding", None),
            (
                "transport",
                "nonzero",
                lambda audit: audit.update(
                    provider_error_category="network_or_transport"
                ),
            ),
            (
                "cleanup",
                "timeout",
                lambda audit: audit.update(
                    failure_category="process_cleanup_failure",
                    process_cleanup_status="failed",
                    exit_code=0,
                    termination_signal=15,
                    timeout_phase="term_drain",
                ),
            ),
        )
        for name, mode, mutate in cases:
            with self.subTest(name=name):
                root = self.root / f"inventory-valid-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode=mode,
                )
                if mutate is not None:
                    audit = json.loads(audit_path.read_text(encoding="utf-8"))
                    mutate(audit)
                    audit_path.write_text(json.dumps(audit), encoding="utf-8")
                grant = self.install_historical_inventory_authority(
                    handoff,
                    provider,
                )
                self.assertEqual(grant["legacy_attempt_inventory_count"], 1)
                self.assertEqual(grant["external_prior_count"], 79)

    def test_global_authority_inventory_rejects_failure_coverage_drift_zero_write(
        self,
    ) -> None:
        for name, mode in (
            ("runtime-nonzero", "nonzero"),
            ("timeout", "timeout"),
            ("invalid-json", "invalid_json"),
        ):
            with self.subTest(name=name):
                root = self.root / f"inventory-failure-coverage-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(root, runner_mode=mode)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["covered_units"] = audit["eligible_units"]
                audit["unaccounted_units"] = 0
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(handoff, provider)
                self.assertEqual(self.tree_snapshot(root), before)
                self.assertFalse(
                    (handoff.audit_root / "semantic_global_authority").exists()
                )

    def test_global_authority_inventory_rejects_failure_projection_drift_zero_write(
        self,
    ) -> None:
        attacks = (
            ("runtime-start-result", "no_result", {"failure_category": "runtime_start_failure", "exit_code": None, "process_cleanup_status": "not_started", "result_file_present": True, "result_size_bytes": 1, "result_fingerprint": "sha256:" + "a" * 64}),
            ("timeout-no-phase", "timeout", {"timeout_phase": None}),
            ("nonzero-zero-exit", "nonzero", {"exit_code": 0}),
            ("runtime-execution-failed-cleanup", "no_result", {"failure_category": "runtime_execution_failure", "exit_code": None, "process_cleanup_status": "failed"}),
            ("cleanup-verified", "timeout", {"failure_category": "process_cleanup_failure", "process_cleanup_status": "verified"}),
            ("no-result-present", "no_result", {"result_file_present": True, "result_size_bytes": 1, "result_fingerprint": "sha256:" + "a" * 64}),
            ("invalid-result-absent", "invalid_json", {"result_file_present": False, "result_size_bytes": 0, "result_fingerprint": None}),
            ("binding-result-absent", "wrong_binding", {"result_file_present": False, "result_size_bytes": 0, "result_fingerprint": None}),
            ("contract-cleanup", "candidate_shape", {"process_cleanup_status": "failed"}),
        )
        for name, mode, mutation in attacks:
            with self.subTest(name=name):
                root = self.root / f"inventory-failure-projection-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(root, runner_mode=mode)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit.update(mutation)
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(handoff, provider)
                self.assertEqual(self.tree_snapshot(root), before)
                self.assertFalse(
                    (handoff.audit_root / "semantic_global_authority").exists()
                )

    def test_global_authority_inventory_rejects_producer_count_drift_zero_write(
        self,
    ) -> None:
        zero_contract = {field: 0 for field in _CONTRACT_DIAGNOSTIC_FIELDS[1:]}
        zero_grouping = {field: 0 for field in _GROUPING_DIAGNOSTIC_FIELDS}
        attacks = (
            ("candidate-item-only", "candidate_shape", 1, {"candidate_item_count": 8}),
            (
                "covered-without-records",
                "candidate_shape",
                1,
                {**zero_contract, **zero_grouping},
            ),
            ("success-raw-below-eligible", "success", 40, {"raw_record_count": 1, "projected_record_count": 1}),
            ("success-contract-count", "success", 1, {"candidate_item_count": 1}),
            (
                "raw-item-drift",
                "candidate_shape",
                1,
                {"candidate_item_count": 5, "candidate_anchor_ref_count": 5, "raw_record_count": 10, "projected_record_count": 5},
            ),
            ("covered-without-raw", "candidate_shape", 1, {"raw_record_count": 0, "projected_record_count": 0}),
            ("missing", "candidate_shape", 1, {"missing_anchor_count": 1}),
            ("accounting", "candidate_shape", 1, {"accounting_item_count": 0}),
            ("duplicate-anchor", "candidate_shape", 1, {"duplicate_anchor_ref_count": 1}),
            ("duplicate-exact", "candidate_shape", 1, {"duplicate_exact_body_count": 1}),
            ("collision", "candidate_shape", 1, {"grouping_collision_count": 1}),
            (
                "grouping-without-signal",
                "candidate_shape",
                1,
                {
                    "contract_failure_detail": "record_grouping",
                    "contract_failure_stage": "record_grouping",
                },
            ),
            (
                "coverage-without-signal",
                "candidate_shape",
                1,
                {
                    "contract_failure_detail": "anchor_coverage",
                    "contract_failure_stage": "coverage",
                },
            ),
            ("binding-empty-result", "wrong_binding", 1, {"result_size_bytes": 0}),
            ("contract-empty-result", "candidate_shape", 1, {"result_size_bytes": 0}),
        )
        for name, mode, blocks, mutation in attacks:
            with self.subTest(name=name):
                root = self.root / f"inventory-producer-count-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode=mode,
                    blocks=blocks,
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit.update(mutation)
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(handoff, provider)
                self.assertEqual(self.tree_snapshot(root), before)
                self.assertFalse(
                    (handoff.audit_root / "semantic_global_authority").exists()
                )

    def test_global_authority_inventory_accepts_top_level_projection_union(
        self,
    ) -> None:
        for name, missing in (("non-object", 0), ("object", 1)):
            with self.subTest(name=name):
                root = self.root / f"inventory-top-level-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode="candidate_shape",
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit.update(
                    failure_category="result_contract_failure",
                    contract_failure_detail="top_level_schema",
                    contract_failure_stage="top_level",
                    covered_units=0,
                    unaccounted_units=audit["eligible_units"],
                    **{
                        field: 0
                        for field in (
                            *_CONTRACT_DIAGNOSTIC_FIELDS[1:],
                            *_GROUPING_DIAGNOSTIC_FIELDS,
                        )
                    },
                )
                audit["missing_anchor_count"] = missing
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                grant = self.install_historical_inventory_authority(
                    handoff,
                    provider,
                )
                self.assertEqual(grant["legacy_attempt_inventory_count"], 1)

    def test_processing_audit_writer_uses_same_producer_contract_before_write(
        self,
    ) -> None:
        root = self.root / "audit-writer-producer-contract"
        representation, service = self.build_service(root=root / "source")
        audit_root = root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            audit_root,
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner("candidate_shape"),
        )
        batch = _analysis_batches(
            _units_from_representation(
                representation,
                service.representation_repository,
            ),
            service.batch_size,
        )[0]
        with self.assertRaises(RepresentationInformationError):
            provider.analyze(batch)
        invalid = replace(
            provider.execution_records[0],
            candidate_item_count=8,
        )
        with self.assertRaises(SemanticHandoffError):
            handoff._persist_audits(
                [invalid],
                package_published=False,
                information_ingested=False,
                durable_ingestion_status="ingestion_not_completed",
                package_fingerprint=None,
                handoff_status="failed",
            )
        self.assertFalse(audit_root.exists())

    def test_global_authority_inventory_rejects_unreachable_versioned_detail(
        self,
    ) -> None:
        cases = (
            (
                EXTERNAL_AGENT_PROTOCOL_V1,
                "anchor_accounting",
                None,
            ),
            (
                EXTERNAL_AGENT_PROTOCOL_V3_3,
                "record_grouping",
                "record_grouping",
            ),
        )
        for protocol, detail, stage in cases:
            with self.subTest(protocol=protocol, detail=detail):
                root = self.root / (
                    "inventory-detail-" + protocol.replace("/", "_")
                )
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode="candidate_shape",
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["protocol_version"] = protocol
                audit["input_fingerprint"] = _external_agent_request(
                    batch,
                    protocol_version=protocol,
                )[1]
                audit["contract_failure_detail"] = detail
                if protocol == EXTERNAL_AGENT_PROTOCOL_V1:
                    audit["covered_units"] = 0
                    audit["unaccounted_units"] = audit["eligible_units"]
                    audit["result_fingerprint"] = None
                    for field in (
                        *_CONTRACT_DIAGNOSTIC_FIELDS,
                        *_GROUPING_DIAGNOSTIC_FIELDS,
                    ):
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/1.0"
                    )
                else:
                    audit["contract_failure_stage"] = stage
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(handoff, provider)
                self.assertEqual(self.tree_snapshot(root), before)

    def test_global_authority_inventory_accepts_v34_record_grouping_detail(
        self,
    ) -> None:
        root = self.root / "inventory-v34-record-grouping"
        (
            _representation,
            _service,
            handoff,
            provider,
            _batch,
            audit_path,
        ) = self.build_historical_inventory_audit(
            root,
            runner_mode="candidate_duplicate_anchor",
        )
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["contract_failure_detail"], "record_grouping")
        self.assertEqual(audit["raw_record_count"], 2)
        self.assertEqual(audit["projected_record_count"], 1)
        self.assertEqual(audit["duplicate_exact_body_count"], 1)
        self.assertEqual(audit["grouping_collision_count"], 0)
        grant = self.install_historical_inventory_authority(handoff, provider)
        self.assertEqual(grant["legacy_attempt_inventory_count"], 1)

    def test_global_authority_inventory_accepts_versioned_anchor_coverage_signals(
        self,
    ) -> None:
        def counts(
            covered: int,
            accounting: int,
            missing: int,
            *,
            candidates: int | None = None,
            unknown: int = 0,
            duplicate_accounting: int = 0,
        ) -> dict[str, int]:
            candidate_count = covered if candidates is None else candidates
            return {
                "covered_units": covered,
                "unaccounted_units": 2 - covered,
                "candidate_item_count": candidate_count,
                "residue_item_count": 0,
                "accounting_item_count": accounting,
                "candidate_anchor_ref_count": candidate_count,
                "residue_anchor_ref_count": 0,
                "duplicate_anchor_ref_count": 0,
                "duplicate_accounting_count": duplicate_accounting,
                "dual_assignment_count": 0,
                "missing_anchor_count": missing,
                "unknown_anchor_ref_count": unknown,
                "raw_record_count": candidate_count,
                "projected_record_count": candidate_count,
                "duplicate_exact_body_count": 0,
                "grouping_collision_count": 0,
            }

        cases = (
            ("v31-missing", EXTERNAL_AGENT_PROTOCOL_V3_1, counts(1, 2, 1)),
            ("v31-short", EXTERNAL_AGENT_PROTOCOL_V3_1, counts(2, 1, 0)),
            ("v31-duplicate", EXTERNAL_AGENT_PROTOCOL_V3_1, counts(2, 2, 0, duplicate_accounting=1)),
            ("v31-unknown", EXTERNAL_AGENT_PROTOCOL_V3_1, counts(2, 2, 0, unknown=1)),
            ("v32-missing", EXTERNAL_AGENT_PROTOCOL_V3_2, counts(1, 2, 1)),
            ("v32-short", EXTERNAL_AGENT_PROTOCOL_V3_2, counts(2, 1, 0)),
            ("v32-extra", EXTERNAL_AGENT_PROTOCOL_V3_2, counts(2, 3, 0, unknown=1)),
            ("v32-swap", EXTERNAL_AGENT_PROTOCOL_V3_2, counts(1, 2, 1, unknown=1)),
            ("v33-missing", EXTERNAL_AGENT_PROTOCOL_V3_3, counts(1, 1, 1)),
            ("v33-extra", EXTERNAL_AGENT_PROTOCOL_V3_3, counts(2, 3, 0, unknown=1)),
            ("v33-swap", EXTERNAL_AGENT_PROTOCOL_V3_3, counts(1, 2, 1, unknown=1)),
            ("v33-empty", EXTERNAL_AGENT_PROTOCOL_V3_3, counts(1, 2, 1)),
            ("v33-nonobject", EXTERNAL_AGENT_PROTOCOL_V3_3, counts(0, 0, 2, candidates=0)),
            ("v34-missing", EXTERNAL_AGENT_PROTOCOL_V3_4, counts(1, 1, 1)),
            ("v34-extra", EXTERNAL_AGENT_PROTOCOL_V3_4, counts(2, 3, 0, unknown=1)),
            ("v34-swap", EXTERNAL_AGENT_PROTOCOL_V3_4, counts(1, 2, 1, unknown=1)),
            ("v34-empty", EXTERNAL_AGENT_PROTOCOL_V3_4, counts(1, 2, 1)),
            ("v34-nonobject", EXTERNAL_AGENT_PROTOCOL_V3_4, counts(0, 0, 2, candidates=0)),
        )
        for name, protocol, projection in cases:
            with self.subTest(name=name):
                root = self.root / f"inventory-anchor-coverage-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode="candidate_shape",
                    blocks=2,
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["protocol_version"] = protocol
                audit["input_fingerprint"] = _external_agent_request(
                    batch,
                    protocol_version=protocol,
                )[1]
                audit["contract_failure_detail"] = "anchor_coverage"
                audit["contract_failure_stage"] = "coverage"
                audit.update(projection)
                if protocol != EXTERNAL_AGENT_PROTOCOL_V3_4:
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                grant = self.install_historical_inventory_authority(
                    handoff,
                    provider,
                )
                self.assertEqual(grant["legacy_attempt_inventory_count"], 1)

    def test_global_authority_inventory_accepts_genuine_v31_accounting_projections(
        self,
    ) -> None:
        projections: set[tuple[int, int, int]] = set()
        for name, symbolic_refs in (
            ("empty", ()),
            ("one-known", ("a",)),
            ("one-unknown", ("u",)),
            ("duplicate-known", ("a", "a")),
            ("known-unknown", ("a", "u")),
            ("duplicate-unknown", ("u", "u")),
            ("distinct-unknown", ("u", "v")),
            ("extra-known-duplicate", ("a", "b", "a")),
            ("extra-known-triplicate", ("a", "a", "a")),
            ("extra-one-unknown", ("a", "b", "u")),
            ("extra-duplicate-unknown", ("a", "u", "u")),
            ("extra-triplicate-unknown", ("u", "u", "u")),
        ):
            with self.subTest(name=name):
                root = self.root / f"inventory-v31-raw-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode="candidate_shape",
                    blocks=2,
                )
                known_a, known_b = (
                    unit.unit_id for unit in batch.anchor_units
                )
                unknown_u = "unit_" + "f" * 64
                unknown_v = "unit_" + "e" * 64
                references = {
                    "a": known_a,
                    "b": known_b,
                    "u": unknown_u,
                    "v": unknown_v,
                }
                accounting_refs = tuple(
                    references[value] for value in symbolic_refs
                )
                schema = external_agent_representation_analysis_schema(
                    EXTERNAL_AGENT_PROTOCOL_V3_1,
                    batch=batch,
                )
                request, fingerprint = _external_agent_request(
                    batch,
                    protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
                    result_schema=schema,
                )
                with tempfile.TemporaryDirectory() as directory:
                    result_path = Path(directory) / "result.json"
                    process = FakeProcess(
                        ["codex", "--output-last-message", str(result_path)],
                        mode="success",
                        calls=[],
                        accounting_refs=accounting_refs,
                    )
                    process.communicate(
                        input="Request:\n" + json.dumps(request)
                    )
                    raw = result_path.read_text(encoding="utf-8")
                with self.assertRaises(RepresentationInformationError) as raised:
                    _parse_external_agent_result(
                        raw,
                        batch,
                        fingerprint,
                        expected_protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
                    )
                self.assertEqual(
                    getattr(raised.exception, "detail", None),
                    "anchor_coverage",
                )
                diagnostics = _contract_failure_diagnostics(
                    raw,
                    batch,
                    "anchor_coverage",
                )
                projection = (
                    int(diagnostics["accounting_item_count"]),
                    int(diagnostics["unknown_anchor_ref_count"]),
                    int(diagnostics["duplicate_accounting_count"]),
                )
                projections.add(projection)
                if name == "duplicate-unknown":
                    self.assertEqual(projection, (2, 2, 1))

                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                covered_units = int(diagnostics.pop("covered_units"))
                audit.update(
                    protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_1,
                    input_fingerprint=fingerprint,
                    contract_failure_detail="anchor_coverage",
                    covered_units=covered_units,
                    unaccounted_units=audit["eligible_units"] - covered_units,
                    result_fingerprint=(
                        "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
                    ),
                    result_size_bytes=len(raw.encode()),
                    diagnostic_schema_version="external-agent-diagnostics/2.0",
                    **diagnostics,
                )
                for field in _GROUPING_DIAGNOSTIC_FIELDS:
                    audit.pop(field)
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                grant = self.install_historical_inventory_authority(
                    handoff,
                    provider,
                )
                self.assertEqual(grant["legacy_attempt_inventory_count"], 1)
        self.assertEqual(len(projections), 12)

    def test_global_authority_inventory_rejects_anchor_coverage_signal_drift(
        self,
    ) -> None:
        cases = (
            ("v31-no-signal", EXTERNAL_AGENT_PROTOCOL_V3_1, {}),
            ("v32-no-signal", EXTERNAL_AGENT_PROTOCOL_V3_2, {}),
            ("v33-no-signal", EXTERNAL_AGENT_PROTOCOL_V3_3, {}),
            ("v34-no-signal", EXTERNAL_AGENT_PROTOCOL_V3_4, {}),
            ("v32-duplicate", EXTERNAL_AGENT_PROTOCOL_V3_2, {"duplicate_accounting_count": 1}),
            ("v33-duplicate", EXTERNAL_AGENT_PROTOCOL_V3_3, {"duplicate_accounting_count": 1}),
            ("v34-duplicate", EXTERNAL_AGENT_PROTOCOL_V3_4, {"duplicate_accounting_count": 1}),
            ("v33-short-accounting", EXTERNAL_AGENT_PROTOCOL_V3_3, {"accounting_item_count": 1}),
            ("v33-long-accounting", EXTERNAL_AGENT_PROTOCOL_V3_3, {"accounting_item_count": 3}),
            ("v34-short-accounting", EXTERNAL_AGENT_PROTOCOL_V3_4, {"accounting_item_count": 1}),
            ("v34-long-accounting", EXTERNAL_AGENT_PROTOCOL_V3_4, {"accounting_item_count": 3}),
        )
        for name, protocol, mutation in cases:
            with self.subTest(name=name):
                root = self.root / f"inventory-anchor-coverage-invalid-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode="candidate_shape",
                    blocks=2,
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["protocol_version"] = protocol
                audit["input_fingerprint"] = _external_agent_request(
                    batch,
                    protocol_version=protocol,
                )[1]
                audit["contract_failure_detail"] = "anchor_coverage"
                audit["contract_failure_stage"] = "coverage"
                audit.update(
                    covered_units=2,
                    unaccounted_units=0,
                    candidate_item_count=2,
                    residue_item_count=0,
                    accounting_item_count=2,
                    candidate_anchor_ref_count=2,
                    residue_anchor_ref_count=0,
                    duplicate_anchor_ref_count=0,
                    duplicate_accounting_count=0,
                    dual_assignment_count=0,
                    missing_anchor_count=0,
                    unknown_anchor_ref_count=0,
                    raw_record_count=2,
                    projected_record_count=2,
                    duplicate_exact_body_count=0,
                    grouping_collision_count=0,
                )
                audit.update(mutation)
                if protocol != EXTERNAL_AGENT_PROTOCOL_V3_4:
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(handoff, provider)
                self.assertEqual(self.tree_snapshot(root), before)

    def test_global_authority_inventory_rejects_impossible_audit_states_zero_write(
        self,
    ) -> None:
        attacks = {
            "success_nonzero_exit": lambda audit: audit.update(exit_code=7),
            "success_package_not_bool": lambda audit: audit.update(
                package_published=None
            ),
            "completed_without_package": lambda audit: audit.update(
                durable_ingestion_status="completed",
                handoff_status="completed",
            ),
            "failed_strict_passed": lambda audit: audit.update(
                execution_status="failed",
                failure_category="runtime_nonzero_exit",
                strict_validation_status="passed",
            ),
            "missing_field": lambda audit: audit.pop("handoff_status"),
            "extra_field": lambda audit: audit.update(unexpected=True),
        }
        for name, mutate in attacks.items():
            with self.subTest(name=name):
                root = self.root / f"inventory-impossible-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(root)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                mutate(audit)
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(
                        handoff,
                        provider,
                    )
                self.assertEqual(self.tree_snapshot(root), before)
                self.assertFalse(
                    (handoff.audit_root / "semantic_global_authority").exists()
                )

    def test_global_authority_inventory_uses_exact_historical_audit_versions(
        self,
    ) -> None:
        protocols = (
            EXTERNAL_AGENT_PROTOCOL_V1,
            EXTERNAL_AGENT_PROTOCOL_V2,
            EXTERNAL_AGENT_PROTOCOL_V3,
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            EXTERNAL_AGENT_PROTOCOL_V3_2,
            EXTERNAL_AGENT_PROTOCOL_V3_3,
            EXTERNAL_AGENT_PROTOCOL_V3_4,
        )
        for protocol in protocols:
            with self.subTest(protocol=protocol):
                root = self.root / f"inventory-version-{protocol.replace('/', '_')}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(root)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["protocol_version"] = protocol
                audit["input_fingerprint"] = _external_agent_request(
                    batch,
                    protocol_version=protocol,
                )[1]
                if protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V1,
                    EXTERNAL_AGENT_PROTOCOL_V2,
                    EXTERNAL_AGENT_PROTOCOL_V3,
                }:
                    for field in (
                        *_CONTRACT_DIAGNOSTIC_FIELDS,
                        *_GROUPING_DIAGNOSTIC_FIELDS,
                    ):
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/1.0"
                    )
                elif protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V3_1,
                    EXTERNAL_AGENT_PROTOCOL_V3_2,
                    EXTERNAL_AGENT_PROTOCOL_V3_3,
                }:
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                grant = self.install_historical_inventory_authority(
                    handoff,
                    provider,
                )
                self.assertEqual(grant["legacy_attempt_inventory_count"], 1)
                audit["unexpected"] = True
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(
                        handoff,
                        provider,
                    )
                self.assertEqual(self.tree_snapshot(root), before)

    def test_global_authority_inventory_uses_versioned_failure_coverage_states(
        self,
    ) -> None:
        protocols = (
            EXTERNAL_AGENT_PROTOCOL_V1,
            EXTERNAL_AGENT_PROTOCOL_V2,
            EXTERNAL_AGENT_PROTOCOL_V3,
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            EXTERNAL_AGENT_PROTOCOL_V3_2,
            EXTERNAL_AGENT_PROTOCOL_V3_3,
            EXTERNAL_AGENT_PROTOCOL_V3_4,
        )
        for protocol in protocols:
            with self.subTest(protocol=protocol):
                root = self.root / (
                    "inventory-failure-version-" + protocol.replace("/", "_")
                )
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode="invalid_json",
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["protocol_version"] = protocol
                audit["input_fingerprint"] = _external_agent_request(
                    batch,
                    protocol_version=protocol,
                )[1]
                if protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V1,
                    EXTERNAL_AGENT_PROTOCOL_V2,
                    EXTERNAL_AGENT_PROTOCOL_V3,
                }:
                    audit["result_fingerprint"] = None
                    for field in (
                        *_CONTRACT_DIAGNOSTIC_FIELDS,
                        *_GROUPING_DIAGNOSTIC_FIELDS,
                    ):
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/1.0"
                    )
                elif protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V3_1,
                    EXTERNAL_AGENT_PROTOCOL_V3_2,
                    EXTERNAL_AGENT_PROTOCOL_V3_3,
                }:
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                grant = self.install_historical_inventory_authority(
                    handoff,
                    provider,
                )
                self.assertEqual(grant["legacy_attempt_inventory_count"], 1)

                rejected_root = self.root / (
                    "inventory-failure-version-rejected-"
                    + protocol.replace("/", "_")
                )
                (
                    _representation,
                    _service,
                    rejected_handoff,
                    rejected_provider,
                    rejected_batch,
                    rejected_path,
                ) = self.build_historical_inventory_audit(
                    rejected_root,
                    runner_mode="invalid_json",
                )
                rejected = json.loads(rejected_path.read_text(encoding="utf-8"))
                rejected["protocol_version"] = protocol
                rejected["input_fingerprint"] = _external_agent_request(
                    rejected_batch,
                    protocol_version=protocol,
                )[1]
                rejected["covered_units"] = rejected["eligible_units"]
                rejected["unaccounted_units"] = 0
                if protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V1,
                    EXTERNAL_AGENT_PROTOCOL_V2,
                    EXTERNAL_AGENT_PROTOCOL_V3,
                }:
                    rejected["result_fingerprint"] = None
                    for field in (
                        *_CONTRACT_DIAGNOSTIC_FIELDS,
                        *_GROUPING_DIAGNOSTIC_FIELDS,
                    ):
                        rejected.pop(field)
                    rejected["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/1.0"
                    )
                elif protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V3_1,
                    EXTERNAL_AGENT_PROTOCOL_V3_2,
                    EXTERNAL_AGENT_PROTOCOL_V3_3,
                }:
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        rejected.pop(field)
                    rejected["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                rejected_path.write_text(json.dumps(rejected), encoding="utf-8")
                before = self.tree_snapshot(rejected_root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(
                        rejected_handoff,
                        rejected_provider,
                    )
                self.assertEqual(self.tree_snapshot(rejected_root), before)

    def test_global_authority_inventory_preserves_versioned_contract_coverage(
        self,
    ) -> None:
        protocols = (
            EXTERNAL_AGENT_PROTOCOL_V1,
            EXTERNAL_AGENT_PROTOCOL_V2,
            EXTERNAL_AGENT_PROTOCOL_V3,
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            EXTERNAL_AGENT_PROTOCOL_V3_2,
            EXTERNAL_AGENT_PROTOCOL_V3_3,
            EXTERNAL_AGENT_PROTOCOL_V3_4,
        )
        for protocol in protocols:
            with self.subTest(protocol=protocol):
                root = self.root / (
                    "inventory-contract-version-" + protocol.replace("/", "_")
                )
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(
                    root,
                    runner_mode="candidate_shape",
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["protocol_version"] = protocol
                audit["input_fingerprint"] = _external_agent_request(
                    batch,
                    protocol_version=protocol,
                )[1]
                if protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V1,
                    EXTERNAL_AGENT_PROTOCOL_V2,
                    EXTERNAL_AGENT_PROTOCOL_V3,
                }:
                    audit["covered_units"] = 0
                    audit["unaccounted_units"] = audit["eligible_units"]
                    audit["result_fingerprint"] = None
                    for field in (
                        *_CONTRACT_DIAGNOSTIC_FIELDS,
                        *_GROUPING_DIAGNOSTIC_FIELDS,
                    ):
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/1.0"
                    )
                elif protocol in {
                    EXTERNAL_AGENT_PROTOCOL_V3_1,
                    EXTERNAL_AGENT_PROTOCOL_V3_2,
                    EXTERNAL_AGENT_PROTOCOL_V3_3,
                }:
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        audit.pop(field)
                    audit["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                grant = self.install_historical_inventory_authority(
                    handoff,
                    provider,
                )
                self.assertEqual(grant["legacy_attempt_inventory_count"], 1)

    def test_global_authority_inventory_rejects_versioned_binding_drift_zero_write(
        self,
    ) -> None:
        attacks = {
            "unsafe_provider_version": ("provider_version", "unsafe/version"),
            "unknown_protocol": ("protocol_version", "unknown-protocol/9.9"),
            "diagnostics": (
                "diagnostic_schema_version",
                "external-agent-diagnostics/2.0",
            ),
            "termination": ("termination_signal", 15),
        }
        for name, (field, value) in attacks.items():
            with self.subTest(name=name):
                root = self.root / f"inventory-binding-{name}"
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(root)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit[field] = value
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    self.install_historical_inventory_authority(
                        handoff,
                        provider,
                    )
                self.assertEqual(self.tree_snapshot(root), before)

    def test_global_authority_rejects_eighty_one_historical_calls_zero_write(
        self,
    ) -> None:
        root = self.root / "historical-overflow"
        audit_root = root / "audits"
        representation, service = self.build_service(root=root / "source")
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            audit_root,
        )
        historical = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        batch = _analysis_batches(
            _units_from_representation(
                representation, service.representation_repository
            ),
            service.batch_size,
        )[0]
        for _ in range(81):
            historical.analyze(batch)
        handoff._persist_audits(
            historical.execution_records,
            package_published=False,
            information_ingested=False,
            durable_ingestion_status="ingestion_not_completed",
            package_fingerprint=None,
            handoff_status="failed",
        )
        before = self.tree_snapshot(audit_root)
        with self.assertRaisesRegex(SemanticHandoffError, "超过 baseline"):
            handoff.install_global_authority(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                ),
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=self.semantic_window_binding(),
                historical_provider_versions=("0.147.0",),
            )
        self.assertEqual(self.tree_snapshot(audit_root), before)
        self.assertFalse((audit_root / "semantic_global_authority").exists())

    def test_global_authority_accepts_proven_zero_call_window_chains(self) -> None:
        initial = self.semantic_window_binding()
        completed_initial = SemanticCompletedWindowBinding(
            window_run_id=initial.window_run_id,
            window_plan_fingerprint=initial.window_plan_fingerprint,
            window_plan_receipt_fingerprint=(
                initial.window_plan_receipt_fingerprint
            ),
            window_status_fingerprint="sha256:" + "9" * 64,
            window_after_cursor=initial.window_after_cursor,
            window_upper_cursor=initial.window_upper_cursor,
        )
        zero_window = replace(
            initial,
            window_run_id="run_" + "8" * 32,
            window_plan_fingerprint="sha256:" + "3" * 64,
            window_plan_receipt_fingerprint="sha256:" + "2" * 64,
            window_after_cursor=initial.window_upper_cursor,
            window_upper_cursor=(2, "zero", "zero"),
            previous_checkpoint_fingerprint="sha256:" + "1" * 64,
            completed_window_chain=(completed_initial,),
        )
        completed_zero = SemanticCompletedWindowBinding(
            window_run_id=zero_window.window_run_id,
            window_plan_fingerprint=zero_window.window_plan_fingerprint,
            window_plan_receipt_fingerprint=(
                zero_window.window_plan_receipt_fingerprint
            ),
            window_status_fingerprint="sha256:" + "4" * 64,
            window_after_cursor=zero_window.window_after_cursor,
            window_upper_cursor=zero_window.window_upper_cursor,
        )
        third_window = replace(
            initial,
            window_run_id="run_" + "9" * 32,
            window_plan_fingerprint="sha256:" + "a" * 64,
            window_plan_receipt_fingerprint="sha256:" + "b" * 64,
            window_after_cursor=zero_window.window_upper_cursor,
            window_upper_cursor=(3, "third", "third"),
            previous_checkpoint_fingerprint="sha256:" + "c" * 64,
            completed_window_chain=(completed_initial, completed_zero),
        )

        for scenario, first_call, current in (
            ("initial-zero", False, zero_window),
            ("call-zero-call", True, third_window),
        ):
            with self.subTest(scenario=scenario):
                root = self.root / scenario
                audit_root = root / "audits"
                first_representation, first_service = self.build_service(
                    root=root / "first", source_id="src_" + "1" * 32
                )
                first_handoff = ExternalAgentSemanticHandoffService(
                    first_service,
                    JsonlAtomicInformationStore(root / "first.jsonl"),
                    audit_root,
                )
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                )
                first_handoff.install_global_authority(
                    provider,
                    authority_ref="sha256:" + "5" * 64,
                    expected_total=80,
                    max_new=20,
                    absolute_cap=100,
                    window_binding=initial,
                )
                if first_call:
                    first_handoff.execute(
                        first_representation.representation_id,
                        provider,
                        privacy_binding=self.privacy_binding(),
                        authority_binding=initial,
                    )
                next_representation, next_service = self.build_service(
                    root=root / "next", source_id="src_" + "2" * 32
                )
                next_runner = FakeRunner()
                ExternalAgentSemanticHandoffService(
                    next_service,
                    JsonlAtomicInformationStore(root / "next.jsonl"),
                    audit_root,
                ).execute(
                    next_representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.147.0",
                        timeout_seconds=300,
                        runner=next_runner,
                    ),
                    privacy_binding=self.privacy_binding(),
                    authority_binding=current,
                )
                self.assertEqual(len(next_runner.calls), 1)

        tampered = replace(
            third_window,
            completed_window_chain=(
                completed_initial,
                replace(
                    completed_zero,
                    window_after_cursor=(1, "gap", "gap"),
                ),
            ),
        )
        tampered_representation, tampered_service = self.build_service(
            root=root / "tampered", source_id="src_" + "3" * 32
        )
        tampered_runner = FakeRunner()
        before_tamper = self.tree_snapshot(audit_root)
        with self.assertRaisesRegex(SemanticHandoffError, "不连续"):
            ExternalAgentSemanticHandoffService(
                tampered_service,
                JsonlAtomicInformationStore(root / "tampered.jsonl"),
                audit_root,
            ).execute(
                tampered_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=tampered_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=tampered,
            )
        self.assertEqual(tampered_runner.calls, [])
        self.assertEqual(self.tree_snapshot(audit_root), before_tamper)

    def test_global_authority_caps_twenty_attempts_across_windows(self) -> None:
        shared_audits = self.root / "global-audits"

        def build_handoff(index: int, blocks: int = 1):
            root = self.root / f"global_{index:02d}"
            representation, service = self.build_service(
                blocks=blocks,
                root=root,
                source_id=f"src_{index:032x}",
            )
            return representation, ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(root / "atomic.jsonl"),
                shared_audits,
            )

        first_representation, first_handoff = build_handoff(1, 1)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        first_window = self.semantic_window_binding()
        grant = first_handoff.install_global_authority(
            provider,
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=first_window,
        )
        self.assertEqual(grant["external_prior_count"], 80)
        self.assertEqual(
            first_handoff.install_global_authority(
                provider,
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=first_window,
            ),
            grant,
        )
        before_drift = self.tree_snapshot(shared_audits)
        with self.assertRaises(SemanticHandoffError):
            first_handoff.install_global_authority(
                provider,
                authority_ref="sha256:" + "4" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=first_window,
            )
        self.assertEqual(self.tree_snapshot(shared_audits), before_drift)

        first_handoff.execute(
            first_representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            authority_binding=first_window,
        )
        for index in range(2, 20):
            representation, handoff = build_handoff(index, index)
            runner = FakeRunner()
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=first_window,
            )
            self.assertEqual(len(runner.calls), 1)

        second_window = replace(
            first_window,
            window_run_id="run_" + "8" * 32,
            window_plan_fingerprint="sha256:" + "3" * 64,
            window_plan_receipt_fingerprint="sha256:" + "2" * 64,
            window_after_cursor=first_window.window_upper_cursor,
            window_upper_cursor=(2, "next", "next"),
            previous_checkpoint_fingerprint="sha256:" + "1" * 64,
            completed_window_chain=(
                SemanticCompletedWindowBinding(
                    window_run_id=first_window.window_run_id,
                    window_plan_fingerprint=(
                        first_window.window_plan_fingerprint
                    ),
                    window_plan_receipt_fingerprint=(
                        first_window.window_plan_receipt_fingerprint
                    ),
                    window_status_fingerprint="sha256:" + "9" * 64,
                    window_after_cursor=first_window.window_after_cursor,
                    window_upper_cursor=first_window.window_upper_cursor,
                ),
            ),
        )
        multi_representation, multi_handoff = build_handoff(41, 41)
        multi_runner = FakeRunner()
        before_shortage = self.tree_snapshot(shared_audits)
        with self.assertRaisesRegex(SemanticHandoffError, "额度不足"):
            multi_handoff.execute(
                multi_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=multi_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=second_window,
            )
        self.assertEqual(multi_runner.calls, [])
        self.assertEqual(self.tree_snapshot(shared_audits), before_shortage)

        if not hasattr(os, "fork"):
            self.skipTest("global authority concurrency requires fork")
        competitors = [build_handoff(20, 20), build_handoff(22, 22)]
        outcome_paths = [self.root / f"competitor-{index}.json" for index in range(2)]
        child_pids: list[int] = []
        for index, (representation, handoff) in enumerate(competitors):
            pid = os.fork()
            if pid == 0:
                runner = FakeRunner("nonzero")
                state = "success"
                try:
                    handoff.execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0",
                            timeout_seconds=300,
                            runner=runner,
                        ),
                        privacy_binding=self.privacy_binding(),
                        authority_binding=second_window,
                    )
                except BaseException:  # noqa: BLE001 - child evidence capture.
                    state = "failed"
                outcome_paths[index].write_text(
                    json.dumps({"state": state, "calls": len(runner.calls)}),
                    encoding="utf-8",
                )
                os.chmod(outcome_paths[index], 0o600)
                os._exit(0)
            child_pids.append(pid)
        for pid in child_pids:
            waited, status = os.waitpid(pid, 0)
            self.assertEqual(waited, pid)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
        outcomes = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in outcome_paths
        ]
        self.assertEqual(
            sorted(item["state"] for item in outcomes),
            ["failed", "failed"],
        )
        self.assertEqual(sum(item["calls"] for item in outcomes), 1)
        attempts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in shared_audits.glob("semantic_run_*/attempts/*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("schema_version")
            == "semantic-handoff-attempt-receipt/3.0"
        ]
        self.assertEqual(
            sorted(item["global_ordinal"] for item in attempts),
            list(range(81, 101)),
        )
        self.assertEqual(
            len(tuple(shared_audits.glob("semantic_run_*"))),
            20,
        )
        final_attempt = next(
            item for item in attempts if item["global_ordinal"] == 100
        )
        final_run = shared_audits / final_attempt["semantic_run_id"]
        self.assertFalse(
            (
                final_run
                / "results"
                / f"batch_{final_attempt['batch_ordinal']:04d}"
            ).exists()
        )

        blocked_representation, blocked_handoff = build_handoff(21, 21)
        blocked_runner = FakeRunner()
        before_cap = self.tree_snapshot(shared_audits)
        with self.assertRaisesRegex(SemanticHandoffError, "outcome unknown"):
            blocked_handoff.execute(
                blocked_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=blocked_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=second_window,
            )
        self.assertEqual(blocked_runner.calls, [])
        self.assertEqual(self.tree_snapshot(shared_audits), before_cap)

    def test_global_authority_unknown_and_unbound_paths_fail_closed(self) -> None:
        shared_audits = self.root / "unknown-audits"
        representation, service = self.build_service(
            blocks=1,
            root=self.root / "unknown_first",
            source_id="src_" + "a" * 32,
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "unknown-first.jsonl"),
            shared_audits,
        )
        binding = self.semantic_window_binding()
        no_grant_runner = FakeRunner()
        with self.assertRaisesRegex(SemanticHandoffError, "未安装"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=no_grant_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=binding,
            )
        self.assertEqual(no_grant_runner.calls, [])

        failing_runner = FakeRunner("nonzero")
        failing_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=failing_runner,
        )
        handoff.install_global_authority(
            failing_provider,
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=binding,
        )
        with self.assertRaisesRegex(SemanticHandoffError, "未确认新增 Durable"):
            handoff.execute(
                representation.representation_id,
                failing_provider,
                privacy_binding=self.privacy_binding(),
                authority_binding=binding,
            )
        self.assertEqual(len(failing_runner.calls), 1)

        next_representation, next_service = self.build_service(
            blocks=2,
            root=self.root / "unknown_second",
            source_id="src_" + "b" * 32,
        )
        next_handoff = ExternalAgentSemanticHandoffService(
            next_service,
            JsonlAtomicInformationStore(self.root / "unknown-second.jsonl"),
            shared_audits,
        )
        next_runner = FakeRunner()
        with self.assertRaisesRegex(SemanticHandoffError, "outcome unknown"):
            next_handoff.execute(
                next_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=next_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=binding,
            )
        self.assertEqual(next_runner.calls, [])
        with self.assertRaisesRegex(SemanticHandoffError, "direct/unbound"):
            next_handoff.execute(
                next_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=next_runner,
                ),
            )
        self.assertEqual(next_runner.calls, [])

    def test_global_authority_tamper_rejects_before_next_provider(self) -> None:
        import archeos.semantic_handoff as handoff_module

        for attack in (
            "grant_mode",
            "authority_inventory",
            "global_ordinal",
            "window_plan",
            "reviewed_head",
            "deadline",
        ):
            with self.subTest(attack=attack):
                root = self.root / f"global_attack_{attack}"
                audit_root = root / "audits"
                first_representation, first_service = self.build_service(
                    blocks=1,
                    root=root / "first",
                    source_id="src_" + "a" * 32,
                )
                handoff = ExternalAgentSemanticHandoffService(
                    first_service,
                    JsonlAtomicInformationStore(root / "first.jsonl"),
                    audit_root,
                )
                binding = self.semantic_window_binding()
                provider = CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                )
                handoff.install_global_authority(
                    provider,
                    authority_ref="sha256:" + "5" * 64,
                    expected_total=80,
                    max_new=20,
                    absolute_cap=100,
                    window_binding=binding,
                )
                handoff.execute(
                    first_representation.representation_id,
                    provider,
                    privacy_binding=self.privacy_binding(),
                    authority_binding=binding,
                )
                next_binding = binding
                next_timeout = 300
                authority_root = audit_root / "semantic_global_authority"
                attempt_path = next(
                    audit_root.glob("semantic_run_*/attempts/batch_0001.json")
                )
                if attack == "grant_mode":
                    os.chmod(authority_root / "grant.json", 0o644)
                elif attack == "authority_inventory":
                    extra = authority_root / "unexpected.json"
                    extra.write_text("{}", encoding="utf-8")
                    os.chmod(extra, 0o600)
                elif attack in {"global_ordinal", "window_plan"}:
                    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
                    if attack == "global_ordinal":
                        attempt["global_ordinal"] = 82
                    else:
                        attempt["window"]["window_plan_fingerprint"] = (
                            "sha256:" + "0" * 64
                        )
                    projected = dict(attempt)
                    projected.pop("attempt_receipt_fingerprint")
                    attempt["attempt_receipt_fingerprint"] = (
                        handoff_module._fingerprint(projected)
                    )
                    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
                elif attack == "reviewed_head":
                    next_binding = replace(binding, reviewed_git_head="0" * 40)
                else:
                    next_timeout = 299

                next_representation, next_service = self.build_service(
                    blocks=2,
                    root=root / "next",
                    source_id="src_" + "b" * 32,
                )
                next_runner = FakeRunner()
                with self.assertRaises(SemanticHandoffError):
                    ExternalAgentSemanticHandoffService(
                        next_service,
                        JsonlAtomicInformationStore(root / "next.jsonl"),
                        audit_root,
                    ).execute(
                        next_representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0",
                            timeout_seconds=next_timeout,
                            runner=next_runner,
                        ),
                        privacy_binding=self.privacy_binding(),
                        authority_binding=next_binding,
                    )
                self.assertEqual(next_runner.calls, [])

    def test_global_authority_visible_grant_converges_after_fsync_failure(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(
            root=self.root / "grant_crash",
            source_id="src_" + "c" * 32,
        )
        audit_root = self.root / "grant-crash-audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "grant-crash.jsonl"),
            audit_root,
        )
        runner = FakeRunner()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=runner,
        )
        binding = self.semantic_window_binding()
        authority_root = audit_root / "semantic_global_authority"
        original_fsync = handoff_module._fsync_directory
        failed = False

        def fail_after_grant_visible(path):
            nonlocal failed
            if (
                path == authority_root
                and (authority_root / "grant.json").exists()
                and not failed
            ):
                failed = True
                raise OSError("synthetic authority fsync interruption")
            return original_fsync(path)

        with (
            patch.object(handoff_module, "_fsync_directory", fail_after_grant_visible),
            self.assertRaises(OSError),
        ):
            handoff.install_global_authority(
                provider,
                authority_ref="sha256:" + "5" * 64,
                expected_total=80,
                max_new=20,
                absolute_cap=100,
                window_binding=binding,
            )
        self.assertTrue((authority_root / "grant.json").is_file())
        grant = handoff.install_global_authority(
            provider,
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=binding,
        )
        self.assertEqual(grant["baseline_total"], 80)
        self.assertEqual(runner.calls, [])
        self.assertFalse(
            (service.output_root / representation.representation_id).exists()
        )

    def test_concurrent_exact_global_authority_install_is_idempotent(self) -> None:
        representation, service = self.build_service(
            root=self.root / "concurrent-install-source"
        )
        audit_root = self.root / "concurrent-install-audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "concurrent-install.jsonl"),
            audit_root,
        )
        binding = self.semantic_window_binding()
        ready_paths = [self.root / f"install-ready-{index}" for index in range(2)]
        start_path = self.root / "install-start"
        child_script = """
import sys
import time
from pathlib import Path

from archeos.atomic_information import JsonlAtomicInformationStore
from archeos.representation import LocalRepresentationRepository
from archeos.representation_information import (
    CodexCliRepresentationAnalysisProvider,
    RepresentationInformationService,
)
from archeos.semantic_handoff import (
    ExternalAgentSemanticHandoffService,
    SemanticWindowAuthorityBinding,
)
from archeos.source import LocalManagedSourceRepository
from tests.test_semantic_handoff import FakeRunner

managed_root, representation_root, information_root, audit_root, store_path = (
    Path(value) for value in sys.argv[1:6]
)
ready_path, start_path = (Path(value) for value in sys.argv[6:8])
ready_path.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not start_path.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("concurrent install start barrier timed out")
    time.sleep(0.01)
service = RepresentationInformationService(
    LocalManagedSourceRepository(managed_root),
    LocalRepresentationRepository(representation_root),
    information_root,
)
handoff = ExternalAgentSemanticHandoffService(
    service,
    JsonlAtomicInformationStore(store_path),
    audit_root,
)
handoff.install_global_authority(
    CodexCliRepresentationAnalysisProvider(
        provider_version="0.147.0",
        timeout_seconds=300,
        runner=FakeRunner(),
    ),
    authority_ref="sha256:" + "5" * 64,
    expected_total=80,
    max_new=20,
    absolute_cap=100,
    window_binding=SemanticWindowAuthorityBinding(
        campaign_created_at="2026-08-18T00:00:00.000Z",
        campaign_lower_cursor=(0, "", ""),
        frozen_global_upper_cursor=(100, "upper", "upper"),
        capture_provider_version="synthetic-capture-1.0",
        semantic_batch_size=40,
        window_run_id="run_" + "7" * 32,
        window_plan_fingerprint="sha256:" + "8" * 64,
        window_plan_receipt_fingerprint="sha256:" + "7" * 64,
        window_after_cursor=(0, "", ""),
        window_upper_cursor=(1, "window", "window"),
        previous_checkpoint_fingerprint=None,
        completed_window_chain=(),
        reviewed_git_head="6" * 40,
    ),
)
print("passed")
"""
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(self.root / "concurrent-install-source" / "managed"),
                    str(
                        self.root
                        / "concurrent-install-source"
                        / "representations"
                    ),
                    str(self.root / "concurrent-install-source" / "information"),
                    str(audit_root),
                    str(self.root / "concurrent-install.jsonl"),
                    str(ready_path),
                    str(start_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for ready_path in ready_paths
        ]
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready_paths):
            if time.monotonic() >= deadline:
                self.fail("concurrent install child readiness timed out")
            time.sleep(0.01)
        start_path.write_text("start", encoding="utf-8")
        outcomes = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(
            [(process.returncode, stdout.strip(), stderr) for process, (stdout, stderr) in zip(processes, outcomes, strict=True)],
            [(0, "passed", ""), (0, "passed", "")],
        )
        grant = handoff.install_global_authority(
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=FakeRunner(),
            ),
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=binding,
        )
        self.assertEqual(grant["baseline_total"], 80)
        self.assertFalse(
            (service.output_root / representation.representation_id).exists()
        )

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
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        winner_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", timeout_seconds=300, runner=winner_runner
        )
        binding = self.semantic_window_binding()
        handoff.install_global_authority(
            winner_provider,
            authority_ref="sha256:" + "5" * 64,
            expected_total=80,
            max_new=20,
            absolute_cap=100,
            window_binding=binding,
        )
        winner_result: list[object] = []
        winner_error: list[BaseException] = []

        def run_winner() -> None:
            try:
                winner_result.append(
                    handoff.execute(
                        representation.representation_id,
                        winner_provider,
                        privacy_binding=self.privacy_binding(),
                        authority_binding=binding,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - thread evidence capture.
                winner_error.append(exc)

        thread = threading.Thread(target=run_winner)
        thread.start()
        self.assertTrue(started.wait(5))
        loser_runner = FakeRunner()
        loser_error: list[BaseException] = []
        loser_result: list[object] = []

        def run_loser() -> None:
            try:
                loser_result.append(
                    handoff.execute(
                        representation.representation_id,
                        CodexCliRepresentationAnalysisProvider(
                            provider_version="0.147.0", timeout_seconds=300, runner=loser_runner
                        ),
                        privacy_binding=self.privacy_binding(),
                        authority_binding=binding,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - concurrency evidence.
                loser_error.append(exc)

        loser_thread = threading.Thread(target=run_loser)
        loser_thread.start()
        time.sleep(0.1)
        self.assertEqual(loser_runner.calls, [])
        release.set()
        thread.join(5)
        loser_thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(loser_thread.is_alive())
        self.assertEqual(winner_error, [])
        self.assertEqual(len(winner_result), 1)
        self.assertEqual(len(winner_runner.calls), 1)
        self.assertEqual(loser_runner.calls, [])
        self.assertLessEqual(len(loser_result), 1)
        self.assertLessEqual(len(loser_error), 1)

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

        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )

        with (
            patch.object(
                handoff_module,
                "publish_directory_no_replace",
                collide_on_batch,
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=runner
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
            self.execute_with_global_authority(handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=retry_runner
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertEqual(retry_runner.calls, [])

    def test_success_raw_body_is_only_in_private_batch_artifact(self) -> None:
        representation, service = self.build_service(blocks=1)
        audit_root = self.root / "audits"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        self.execute_with_global_authority(handoff,
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
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
        )
        with (
            patch.object(
                ExternalAgentSemanticHandoffService,
                "_persist_audits",
                side_effect=OSError("synthetic pre-audit crash"),
            ),
            self.assertRaises(OSError),
        ):
            self.execute_with_global_authority(handoff,
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
        replay = self.execute_with_global_authority(handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", timeout_seconds=300, runner=replay_runner
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=0,
        )
        self.assertEqual(replay_runner.calls, [])
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.created, 3)

    def test_historical_package_replay_ignores_absent_recovery_receipts(self) -> None:
        representation, service = self.build_service(blocks=2)
        audit_root = self.root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
        replay_runner = FakeRunner()
        replay = self.execute_with_global_authority(handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", timeout_seconds=300, runner=replay_runner
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
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
            audit_root,
        )
        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=3,
        )
        manifest = json.loads((first.package / "manifest.json").read_text())
        self.assertEqual(
            [len(batch["unit_ids"]) for batch in manifest["batches"]], [40, 40, 1]
        )
        self.assertEqual(len(provider.execution_records), 3)

        service.batch_size = 100
        replay_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
        )
        replay = handoff.execute(representation.representation_id, replay_provider)
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay_provider.execution_records, [])
        self.assertEqual(replay.ingestion.existing, 3)

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
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=3,
            )

        audits = sorted(audit_root.glob("*/processing-run-audit.json"))
        audit_payloads = [json.loads(path.read_text()) for path in audits]
        self.assertEqual(len(provider.execution_records), 2)
        self.assertEqual(len(audits), 1)
        failed = next(
            payload
            for payload in audit_payloads
            if payload["execution_status"] == "failed"
        )
        self.assertEqual(failed["failure_category"], "result_contract_failure")
        self.assertEqual(failed["contract_failure_detail"], "anchor_coverage")
        self.assertEqual(failed["eligible_units"], 40)
        self.assertEqual(failed["covered_units"], 39)
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
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service, InspectingStore(self.root / "atomic.jsonl"), audit_root
        )
        self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )

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
        self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", timeout_seconds=300, runner=SequenceRunner("valid", "valid")
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=2,
        )
        next(audit_root.glob("*/processing-run-audit.json")).unlink()
        replay_store = self.root / "replay-atomic.jsonl"
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            ExternalAgentSemanticHandoffService(
                service, JsonlAtomicInformationStore(replay_store), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
        self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", timeout_seconds=300, runner=SequenceRunner("valid", "valid")
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=2,
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
                    provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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

        handoff = ExternalAgentSemanticHandoffService(
            service, ReadbackFailingStore(store_path), audit_root
        )
        with self.assertRaisesRegex(Exception, "已写入或正在读回"):
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
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
                provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
        )
        first_handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "initial-atomic.jsonl"),
            audit_root,
        )
        self.execute_with_global_authority(
            first_handoff,
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
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
                            provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
                        ),
                    )
                self.assertFalse(replay_store.exists())
        audit_path.write_text(json.dumps(original), encoding="utf-8")

    def test_reused_provider_persists_only_current_package_records(self) -> None:
        first_representation, first_service = self.build_service(root=self.root / "first")
        second_representation, second_service = self.build_service(root=self.root / "second")
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", timeout_seconds=300, runner=SequenceRunner("valid", "valid")
        )
        first_handoff = ExternalAgentSemanticHandoffService(
            first_service,
            JsonlAtomicInformationStore(self.root / "first-atomic.jsonl"),
            self.root / "first-audits",
        )
        self.execute_with_global_authority(
            first_handoff,
            first_representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
        second_handoff = ExternalAgentSemanticHandoffService(
            second_service,
            JsonlAtomicInformationStore(self.root / "second-atomic.jsonl"),
            self.root / "second-audits",
        )
        second = self.execute_with_global_authority(
            second_handoff,
            second_representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=1,
        )
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
            handoff = ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(self.root / "atomic.jsonl"),
                audit_root,
            )
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
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
                provider_version="0.147.0", timeout_seconds=300, runner=FakeRunner()
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
