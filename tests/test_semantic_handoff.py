from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
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
    SemanticResultOnlyRequest,
    SemanticWindowAuthorityBinding,
    _package_fingerprint,
    _SemanticGlobalAuthority,
    _SemanticRecoveryRun,
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


class XlsxJsonAdapter:
    name = "synthetic-xlsx"
    version = "1.0"
    kind = "xlsx_structure"
    supported_media_types = ("application/synthetic",)

    def __init__(self, cells: list[dict[str, object]]) -> None:
        self.cells = cells

    def build(self, _source, _materialized, staging_dir, _configuration):
        artifact = staging_dir / "artifacts" / "synthetic-xlsx.json"
        artifact.write_text(
            json.dumps(
                {
                    "sheets": [
                        {
                            "cells": self.cells,
                            "embedded_media": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return AdapterBuildResult(
            self.kind,
            (
                AdapterArtifact(
                    "structure",
                    "artifacts/synthetic-xlsx.json",
                    "application/json",
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
        elif mode == "xlsx_whitespace_mixed":
            for anchor_id, unit in zip(
                anchor_ids, request["anchor_units"], strict=True
            ):
                content = unit["content"]
                cell = unit["structured_value"]["source_locator"]["cell"]
                row = int(cell[1:])
                if isinstance(content, str) and not content.strip():
                    group = (row - 1) // 2 + 1
                    anchor_results[anchor_id] = {
                        "classification": "residue",
                        "records": [
                            residue(
                                f"record_{group:032x}"
                            )
                            | {
                                "reason_not_absorbed": (
                                    f"Synthetic blank cell group {group}."
                                )
                            }
                        ],
                    }
                elif isinstance(content, str) and content.startswith("Candidate"):
                    anchor_results[anchor_id] = {
                        "classification": "candidate",
                        "records": [
                            candidate(
                                f"record_{row:032x}", statement=content
                            )
                        ],
                    }
                else:
                    group = (row - 41) // 3 + 1
                    anchor_results[anchor_id] = {
                        "classification": "residue",
                        "records": [
                            residue(
                                f"record_{(100 + group):032x}"
                            )
                            | {
                                "reason_not_absorbed": (
                                    f"Synthetic deferred cell group {group}."
                                )
                            }
                        ],
                    }

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
        batch_size: int = 40,
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
            batch_size=batch_size,
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
            inventory_authority = self.write_inventory_authority(
                handoff, provider, binding
            )
            handoff.install_global_authority(
                provider,
                inventory_authority_file=inventory_authority,
                window_binding=binding,
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
        binding = self.semantic_window_binding()
        return handoff.install_global_authority(
            provider,
            inventory_authority_file=self.write_inventory_authority(
                handoff, provider, binding
            ),
            window_binding=binding,
        )

    def install_authority(
        self,
        handoff: ExternalAgentSemanticHandoffService,
        provider: CodexCliRepresentationAnalysisProvider,
        binding: SemanticWindowAuthorityBinding,
        *,
        labels: tuple[str, ...] | None = None,
        mutate=None,
    ) -> dict[str, object]:
        return handoff.install_global_authority(
            provider,
            inventory_authority_file=self.write_inventory_authority(
                handoff,
                provider,
                binding,
                labels=labels,
                mutate=mutate,
            ),
            window_binding=binding,
        )

    def build_cap1000_extension_fixture(
        self,
        root: Path,
        *,
        batch_size: int = 40,
    ):
        representation, service = self.build_service(
            root=root / "activation-source",
            source_id="src_" + "e" * 32,
            batch_size=batch_size,
        )
        audit_root = root / "audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "activation-atomic.jsonl"),
            audit_root,
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        base_window = self.semantic_window_binding(batch_size=batch_size)
        grant = self.install_authority(
            handoff, provider, base_window
        )
        handoff.execute(
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            authority_binding=base_window,
        )
        grant_bytes = (
            audit_root / "semantic_global_authority" / "grant.json"
        ).read_bytes()
        ordinal_81_bytes = next(
            path.read_bytes()
            for path in audit_root.glob("semantic_run_*/attempts/*.json")
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 81
        )
        final_head = "7" * 40
        extension = handoff.install_global_authority_extension(
            provider,
            window_binding=base_window,
            reviewed_git_head=final_head,
        )
        return (
            handoff,
            provider,
            base_window,
            grant,
            extension,
            representation,
            grant_bytes,
            ordinal_81_bytes,
        )

    def build_ordinal_166_unknown_fixture(self, root: Path):
        import archeos.semantic_handoff as handoff_module

        (
            _activation_handoff,
            provider,
            base_window,
            _grant,
            extension,
            _activation_representation,
            _grant_bytes,
            _ordinal_81_bytes,
        ) = self.build_cap1000_extension_fixture(root)
        completed = SemanticCompletedWindowBinding(
            window_run_id=base_window.window_run_id,
            window_plan_fingerprint=base_window.window_plan_fingerprint,
            window_plan_receipt_fingerprint=(
                base_window.window_plan_receipt_fingerprint
            ),
            window_status_fingerprint="sha256:" + "c" * 64,
            window_after_cursor=base_window.window_after_cursor,
            window_upper_cursor=base_window.window_upper_cursor,
        )
        current_window = replace(
            base_window,
            window_run_id="run_" + "9" * 32,
            window_plan_fingerprint="sha256:" + "a" * 64,
            window_plan_receipt_fingerprint="sha256:" + "b" * 64,
            window_after_cursor=base_window.window_upper_cursor,
            window_upper_cursor=(2, "current", "current"),
            previous_checkpoint_fingerprint="sha256:" + "d" * 64,
            completed_window_chain=(completed,),
            reviewed_git_head="7" * 40,
        )
        failed = None
        failed_representation = None
        for ordinal in range(82, 167):
            item_root = root / f"ordinal-{ordinal:04d}"
            representation, service = self.build_service(
                root=item_root,
                source_id=f"src_{ordinal:032x}",
                blocks=41 if ordinal == 166 else 1,
            )
            runner = FakeRunner("nonzero" if ordinal == 166 else "valid")
            candidate = ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(item_root / "atomic.jsonl"),
                root / "audits",
            )
            call_provider = CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner,
            )
            if ordinal == 166:
                with self.assertRaises(SemanticHandoffError):
                    candidate.execute(
                        representation.representation_id,
                        call_provider,
                        privacy_binding=self.privacy_binding(),
                        authority_binding=current_window,
                    )
                failed = candidate
                failed_representation = representation
            else:
                candidate.execute(
                    representation.representation_id,
                    call_provider,
                    privacy_binding=self.privacy_binding(),
                    authority_binding=current_window,
                )
            self.assertEqual(len(runner.calls), 1)
        assert failed is not None and failed_representation is not None
        authority = handoff_module._SemanticGlobalAuthority(failed.audit_root)
        base = authority._read_base_grant()
        ext = authority._read_extension(base)
        previous = authority._effective_authority_before_resolution(base, ext)
        attempts, unknown = authority._global_attempts(previous)
        self.assertTrue(unknown)
        self.assertEqual(attempts[-1]["global_ordinal"], 166)
        attempt = attempts[-1]
        run_payload = json.loads(
            (
                failed.audit_root
                / attempt["semantic_run_id"]
                / "run-receipt.json"
            ).read_text(encoding="utf-8")
        )
        batch = run_payload["batches"][attempt["batch_ordinal"] - 1]
        audit_path, audit = authority._matching_failed_audit(
            run_payload=run_payload,
            batch_receipt=batch,
        )
        digest = {
            "run_id": attempt["window"]["window_run_id"],
            "plan_fingerprint": attempt["window"][
                "window_plan_fingerprint"
            ],
            "plan_receipt_fingerprint": attempt["window"][
                "window_plan_receipt_fingerprint"
            ],
            "item_id": "conversation:synthetic",
            "source_id": failed_representation.source_id,
            "representation_id": failed_representation.representation_id,
            "representation_manifest": (
                failed_representation.to_manifest_dict()
            ),
            "representation_artifact_inventory_fingerprint": (
                _canonical_fingerprint(
                    [
                        {
                            "artifact_id": artifact.artifact_id,
                            "content_hash": artifact.content_hash,
                        }
                        for artifact in failed_representation.artifacts
                    ]
                )
            ),
        }
        continuation = {
            "previous_reviewed_git_head": extension["reviewed_git_head"],
            "previous_execution_contract": extension["execution_contract"],
            "reviewed_git_head": "8" * 40,
            "execution_contract": extension["execution_contract"],
            "next_global_ordinal": 167,
        }
        payload = {
            "schema_version": "semantic-handoff-unknown-resolution-authority/1.0",
            "artifact_kind": "semantic_handoff_unknown_resolution_authority",
            "decision_ref": "https://github.com/leevi2010-cursor/ArcheOS/issues/117",
            "current_global_authority_fingerprint": extension[
                "extension_fingerprint"
            ],
            "global_ordinal": 166,
            "window": attempt["window"],
            "semantic_attempt": {
                key: attempt[key]
                for key in (
                    "semantic_run_id",
                    "run_contract_fingerprint",
                    "batch_ordinal",
                    "batch_contract_fingerprint",
                    "input_fingerprint",
                    "attempt_id",
                    "attempt_nonce",
                    "attempt_receipt_fingerprint",
                )
            },
            "failure_audit": {
                "processing_run_id": audit["processing_run_id"],
                "relative_path": audit_path.relative_to(
                    failed.audit_root
                ).as_posix(),
                "audit_fingerprint": "sha256:"
                + hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                "failure_category": "runtime_nonzero_exit",
                "result_file_present": False,
                "process_cleanup_status": "verified",
                "audit_readback_status": "verified",
            },
            "digest": digest,
            "continuation": continuation,
        }
        manifest = {
            **payload,
            "payload_fingerprint": _canonical_fingerprint(payload),
        }
        manifest_path = root / "unknown-authority.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        return (
            failed,
            provider,
            current_window,
            failed_representation,
            digest,
            manifest_path,
        )

    def build_ordinal_212_timeout_fixture(self, root: Path):
        import archeos.semantic_handoff as handoff_module

        (
            failed,
            _provider,
            window_166,
            _failed_representation,
            digest_166,
            manifest_166,
        ) = self.build_ordinal_166_unknown_fixture(root)
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        failed.resolve_unknown(
            provider,
            authority_manifest_file=manifest_166,
            reviewed_git_head="8" * 40,
            digest_binding=digest_166,
            commit_failed_closed_status=lambda _resolution_id: (
                "sha256:" + "a" * 64,
                digest_166,
            ),
        )
        window_167 = replace(window_166, reviewed_git_head="8" * 40)
        latest = failed
        for ordinal in range(167, 177):
            item_root = root / f"ordinal-{ordinal:04d}"
            representation, service = self.build_service(
                root=item_root,
                source_id=f"src_{ordinal:032x}",
            )
            latest = ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(item_root / "atomic.jsonl"),
                failed.audit_root,
            )
            latest.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=window_167,
            )
        maintenance = latest.install_maintenance_continuation(
            provider,
            window_binding=window_167,
            reviewed_git_head="9" * 40,
            authority_ref=(
                "https://github.com/leevi2010-cursor/ArcheOS/issues/127"
                "#issuecomment-1234567890"
            ),
        )
        window_177 = replace(window_167, reviewed_git_head="9" * 40)
        timeout_handoff = None
        timeout_representation = None
        timeout_provider = None
        for ordinal in range(177, 213):
            item_root = root / f"ordinal-{ordinal:04d}"
            representation, service = self.build_service(
                root=item_root,
                source_id=f"src_{ordinal:032x}",
                blocks=3 if ordinal == 212 else 1,
            )
            runner = FakeRunner("timeout" if ordinal == 212 else "valid")
            candidate = ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(item_root / "atomic.jsonl"),
                failed.audit_root,
            )
            call_provider = CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner,
            )
            if ordinal == 212:
                with self.assertRaises(SemanticHandoffError):
                    candidate.execute(
                        representation.representation_id,
                        call_provider,
                        privacy_binding=self.privacy_binding(),
                        authority_binding=window_177,
                    )
                timeout_handoff = candidate
                timeout_representation = representation
                timeout_provider = call_provider
            else:
                candidate.execute(
                    representation.representation_id,
                    call_provider,
                    privacy_binding=self.privacy_binding(),
                    authority_binding=window_177,
                )
        assert (
            timeout_handoff is not None
            and timeout_representation is not None
            and timeout_provider is not None
        )
        authority = handoff_module._SemanticGlobalAuthority(
            timeout_handoff.audit_root
        )
        base = authority._read_base_grant()
        extension = authority._read_extension(base)
        resolution = authority._read_unknown_resolution()
        continuation = authority._read_maintenance_continuation(
            base, extension, resolution
        )
        previous = authority._effective_authority(
            base, extension, resolution, continuation
        )
        attempts, unknown = authority._global_attempts(previous)
        self.assertTrue(unknown)
        self.assertEqual(attempts[-1]["global_ordinal"], 212)
        attempt = attempts[-1]
        run_payload = json.loads(
            (
                timeout_handoff.audit_root
                / attempt["semantic_run_id"]
                / "run-receipt.json"
            ).read_text(encoding="utf-8")
        )
        batch = run_payload["batches"][attempt["batch_ordinal"] - 1]
        audit_path, audit = authority._matching_timeout_212_audit(
            run_payload=run_payload,
            batch_receipt=batch,
        )
        diagnostic_path = (
            timeout_provider.diagnostic_root
            / audit["processing_run_id"]
            / "metadata.json"
        )
        digest = {
            "run_id": attempt["window"]["window_run_id"],
            "plan_fingerprint": attempt["window"][
                "window_plan_fingerprint"
            ],
            "plan_receipt_fingerprint": attempt["window"][
                "window_plan_receipt_fingerprint"
            ],
            "item_id": "conversation:synthetic-212",
            "source_id": timeout_representation.source_id,
            "representation_id": timeout_representation.representation_id,
            "representation_manifest": (
                timeout_representation.to_manifest_dict()
            ),
            "representation_artifact_inventory_fingerprint": (
                _canonical_fingerprint(
                    [
                        {
                            "artifact_id": artifact.artifact_id,
                            "content_hash": artifact.content_hash,
                        }
                        for artifact in timeout_representation.artifacts
                    ]
                )
            ),
        }
        payload = {
            "schema_version": (
                "semantic-handoff-timeout-212-resolution-authority/1.0"
            ),
            "artifact_kind": (
                "semantic_handoff_timeout_212_resolution_authority"
            ),
            "authority_ref": (
                "https://github.com/leevi2010-cursor/ArcheOS/issues/133"
                "#issuecomment-1234567890"
            ),
            "current_global_authority_fingerprint": maintenance[
                "continuation_fingerprint"
            ],
            "global_ordinal": 212,
            "activation_total": 212,
            "activation_unknown_count": 1,
            "activation_last_global_ordinal": 212,
            "activation_attempt_inventory_fingerprint": (
                _canonical_fingerprint(attempts)
            ),
            "window": attempt["window"],
            "semantic_attempt": {
                key: attempt[key]
                for key in (
                    "semantic_run_id",
                    "run_contract_fingerprint",
                    "batch_ordinal",
                    "batch_contract_fingerprint",
                    "input_fingerprint",
                    "attempt_id",
                    "attempt_nonce",
                    "attempt_receipt_fingerprint",
                )
            },
            "failure_audit": {
                "processing_run_id": audit["processing_run_id"],
                "relative_path": audit_path.relative_to(
                    timeout_handoff.audit_root
                ).as_posix(),
                "audit_fingerprint": "sha256:"
                + hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                "failure_category": "timeout",
                "timeout_phase": "initial_communicate",
                "result_file_present": False,
                "process_cleanup_status": "verified",
                "audit_readback_status": "verified",
            },
            "diagnostic_metadata": {
                "processing_run_id": audit["processing_run_id"],
                "metadata_fingerprint": "sha256:"
                + hashlib.sha256(diagnostic_path.read_bytes()).hexdigest(),
            },
            "digest": digest,
            "continuation": {
                "previous_reviewed_git_head": "9" * 40,
                "previous_execution_contract": maintenance[
                    "execution_contract"
                ],
                "reviewed_git_head": "a" * 40,
                "execution_contract": maintenance["execution_contract"],
                "next_global_ordinal": 213,
                "absolute_cap": 1000,
            },
        }
        manifest = {
            **payload,
            "payload_fingerprint": _canonical_fingerprint(payload),
        }
        manifest_path = root / "timeout-212-authority.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o600)
        return (
            timeout_handoff,
            timeout_provider,
            window_177,
            timeout_representation,
            digest,
            manifest_path,
            audit_path,
            diagnostic_path,
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

    def write_inventory_authority(
        self,
        handoff: ExternalAgentSemanticHandoffService,
        provider: CodexCliRepresentationAnalysisProvider,
        window: SemanticWindowAuthorityBinding,
        *,
        labels: tuple[str, ...] | None = None,
        mutate=None,
    ) -> Path:
        import archeos.semantic_handoff as handoff_module

        observed = self.historical_provider_versions(handoff.audit_root)
        approved = observed if labels is None else labels
        authority = handoff_module._SemanticGlobalAuthority(handoff.audit_root)
        _total, inventory_fingerprint, counts = authority._legacy_inventory(observed)
        approved_counts = {
            label: counts.get(label, 1)
            for label in approved
        }
        payload = {
            "schema_version": "semantic-handoff-inventory-authority/1.0",
            "artifact_kind": "semantic_handoff_inventory_authority",
            "authority_ref": "sha256:" + "5" * 64,
            "reviewed_git_head": window.reviewed_git_head,
            "campaign": {
                "created_at": window.campaign_created_at,
                "lower_cursor": list(window.campaign_lower_cursor),
                "frozen_global_upper_cursor": list(
                    window.frozen_global_upper_cursor
                ),
                "capture_provider_version": window.capture_provider_version,
                "semantic_batch_size": window.semantic_batch_size,
            },
            "expected_raw_provider_labels": list(approved),
            "historical_provider_version_counts": approved_counts,
            "local_total": sum(approved_counts.values()),
            "legacy_inventory_fingerprint": inventory_fingerprint,
            "baseline_total": 80,
            "max_new": 20,
            "absolute_cap": 100,
        }
        if mutate is not None:
            mutate(payload)
        payload["payload_fingerprint"] = _canonical_fingerprint(payload)
        directory = self.root / "inventory-authorities"
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"authority-{len(tuple(directory.iterdir())):04d}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        return path

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
        changed = handoff.execute(
            representation.representation_id, changed_profile
        )
        self.assertTrue(changed.replayed_existing_package)
        self.assertEqual(changed.ingestion.existing, 1)
        self.assertEqual(changed_runner.calls, [])

    def test_xlsx_whitespace_cached_results_finalize_without_provider(self) -> None:
        import archeos.representation_information as information_module

        cells = [
            {
                "value": " ",
                "source_locator": {"sheet": "Sheet1", "cell": f"A{row}"},
            }
            for row in range(1, 35)
        ]
        cells.extend(
            {
                "value": f"Candidate {(row - 35) // 2 + 1}",
                "source_locator": {"sheet": "Sheet1", "cell": f"A{row}"},
            }
            for row in range(35, 41)
        )
        cells.extend(
            {
                "value": f"Deferred {(row - 41) // 3 + 1}",
                "source_locator": {"sheet": "Sheet1", "cell": f"A{row}"},
            }
            for row in range(41, 59)
        )
        external = self.root / "synthetic-xlsx.bin"
        external.write_text("synthetic", encoding="utf-8")
        sources = LocalManagedSourceRepository(
            self.root / "managed-xlsx",
            id_factory=lambda: "src_" + "d" * 32,
            clock=lambda: "2026-08-15T00:00:00.000Z",
        )
        source = sources.admit(
            external, metadata={"media_type": "application/synthetic"}
        ).source
        representations = LocalRepresentationRepository(
            self.root / "representations-xlsx"
        )
        representation = RepresentationService(sources, representations).build(
            source.source_id, XlsxJsonAdapter(cells)
        ).representation
        service = RepresentationInformationService(
            sources,
            representations,
            self.root / "information-xlsx",
            batch_size=40,
            clock=lambda: "2026-08-15T00:00:00.000Z",
        )
        audit_root = self.root / "audits-xlsx"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "atomic-xlsx.jsonl"),
            audit_root,
        )
        units = _units_from_representation(representation, representations)
        blank_unit_ids = {
            unit.unit_id
            for unit in units
            if isinstance(unit.content, str) and not unit.content.strip()
        }
        self.assertEqual(len(units), 58)
        self.assertEqual(len(blank_unit_ids), 34)

        current_evidence = information_module._evidence

        def legacy_evidence(unit_ids, by_id, positions):
            records = current_evidence(unit_ids, by_id, positions)
            for record in records:
                unit = by_id[record["unit_id"]]
                if unit.content is not None:
                    record["excerpt"] = unit.content
            return records

        first_runner = FakeRunner("xlsx_whitespace_mixed")
        with (
            patch.object(information_module, "_evidence", legacy_evidence),
            self.assertRaises(SemanticHandoffError),
        ):
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=first_runner,
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=2,
            )
        self.assertEqual(len(first_runner.calls), 2)
        self.assertFalse(
            (service.output_root / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic-xlsx.jsonl").exists())

        blocked_runner = FakeRunner("nonzero")
        blocked_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=blocked_runner,
        )
        preflight = handoff.recovery_preflight(
            representation.representation_id,
            blocked_provider,
            self.privacy_binding(),
        )
        self.assertEqual(preflight.replayable_batches, 2)
        self.assertEqual(preflight.required_new_calls, 0)
        attempts_before = tuple(audit_root.glob("semantic_run_*/attempts/*.json"))
        self.assertEqual(len(attempts_before), 2)
        finalized = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            blocked_provider,
            privacy_binding=self.privacy_binding(),
            new_call_authority=0,
        )
        self.assertEqual(blocked_runner.calls, [])
        self.assertEqual(
            len(tuple(audit_root.glob("semantic_run_*/attempts/*.json"))), 2
        )
        manifest, candidates = validate_representation_information_package(
            finalized.package
        )
        residue = [
            json.loads(line)
            for line in (finalized.package / "residue.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(manifest["counts"]["atomic_information_candidates"], 3)
        self.assertEqual(manifest["counts"]["residue_items"], 23)
        candidate_evidence = {
            evidence["unit_id"]
            for item in candidates
            for evidence in item["source_evidence"]
        }
        residue_evidence = {
            evidence["unit_id"]
            for item in residue
            for evidence in item["source_evidence"]
        }
        self.assertTrue(blank_unit_ids.isdisjoint(candidate_evidence))
        self.assertTrue(blank_unit_ids <= residue_evidence)
        blank_excerpts = [
            evidence["excerpt"]
            for item in residue
            for evidence in item["source_evidence"]
            if evidence["unit_id"] in blank_unit_ids
        ]
        self.assertTrue(all(excerpt.strip() for excerpt in blank_excerpts))
        self.assertTrue(
            all(json.loads(excerpt)["value"] == " " for excerpt in blank_excerpts)
        )
        candidate_excerpts = [
            evidence["excerpt"]
            for item in candidates
            for evidence in item["source_evidence"]
        ]
        self.assertTrue(
            all(excerpt.startswith("Candidate") for excerpt in candidate_excerpts)
        )

        replay_runner = FakeRunner("nonzero")
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=replay_runner
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay.ingestion.existing, 3)
        self.assertEqual(replay_runner.calls, [])
        self.assertEqual(
            len(tuple(audit_root.glob("semantic_run_*/attempts/*.json"))), 2
        )

        malformed = self.root / "malformed-xlsx-package"
        shutil.copytree(finalized.package, malformed)
        candidate_path = malformed / "atomic_information_candidates.jsonl"
        malformed_candidates = [
            json.loads(line)
            for line in candidate_path.read_text(encoding="utf-8").splitlines()
        ]
        malformed_candidates[0]["source_evidence"][0]["locator"] = "not-json"
        candidate_path.write_text(
            "".join(json.dumps(item) + "\n" for item in malformed_candidates),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            RepresentationInformationError, "Evidence locator is invalid"
        ):
            validate_representation_information_package(malformed)

    def test_new_publish_rejects_package_provider_mismatch_before_ingestion(
        self,
    ) -> None:
        import archeos.representation_information as information_module

        representation, service = self.build_service()
        audit_root = self.root / "new-provider-mismatch-audits"
        store_path = self.root / "new-provider-mismatch-atomic.jsonl"
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), audit_root
        )
        mismatched = {
            "name": provider.name,
            "provider_version": provider.provider_version,
            "model": provider.model,
            "reasoning_effort": "high",
            "fallback_policy": provider.fallback_policy,
        }
        with (
            patch.object(
                information_module,
                "_provider_manifest",
                return_value=mismatched,
            ),
            self.assertRaisesRegex(Exception, "审计无法安全保存"),
        ):
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        self.assertFalse(store_path.exists())
        self.assertEqual(
            tuple(audit_root.glob("run_*/processing-run-audit.json")), ()
        )

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

    def test_profiled_historical_packages_replay_with_changed_current_profile(
        self,
    ) -> None:
        protocols = (
            EXTERNAL_AGENT_PROTOCOL_V3_1,
            EXTERNAL_AGENT_PROTOCOL_V3_2,
            EXTERNAL_AGENT_PROTOCOL_V3_3,
            EXTERNAL_AGENT_PROTOCOL_V3_4,
        )
        for protocol in protocols:
            with self.subTest(protocol=protocol):
                root = self.root / protocol.replace("/", "-")
                representation, service = self.build_service(root=root)
                audit_root = root / "audits"
                handoff = ExternalAgentSemanticHandoffService(
                    service,
                    JsonlAtomicInformationStore(root / "atomic.jsonl"),
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
                if protocol != EXTERNAL_AGENT_PROTOCOL_V3_4:
                    batch = _analysis_batches(
                        _units_from_representation(
                            representation,
                            service.representation_repository,
                        ),
                        service.batch_size,
                    )[0]
                    _, fingerprint = _external_agent_request(
                        batch, protocol_version=protocol
                    )
                    audit_path = first.audit_paths[0]
                    historical = json.loads(
                        audit_path.read_text(encoding="utf-8")
                    )
                    historical["protocol_version"] = protocol
                    historical["input_fingerprint"] = fingerprint
                    historical["diagnostic_schema_version"] = (
                        "external-agent-diagnostics/2.0"
                    )
                    for field in _GROUPING_DIAGNOSTIC_FIELDS:
                        historical.pop(field)
                    audit_path.write_text(
                        json.dumps(historical), encoding="utf-8"
                    )

                replay_runner = FakeRunner()
                replay = handoff.execute(
                    representation.representation_id,
                    CodexCliRepresentationAnalysisProvider(
                        provider_version="0.999.0",
                        reasoning_effort="high",
                        timeout_seconds=17,
                        runner=replay_runner,
                    ),
                )
                self.assertTrue(replay.replayed_existing_package)
                self.assertEqual(replay.ingestion.existing, 1)
                self.assertEqual(replay_runner.calls, [])
                for invalid_deadline in (True, 0, -1, "300000"):
                    with self.subTest(
                        protocol=protocol,
                        invalid_deadline=invalid_deadline,
                    ):
                        audit_path = first.audit_paths[0]
                        tampered = json.loads(
                            audit_path.read_text(encoding="utf-8")
                        )
                        tampered["deadline_ms"] = invalid_deadline
                        audit_path.write_text(
                            json.dumps(tampered), encoding="utf-8"
                        )
                        rejected_runner = FakeRunner()
                        with self.assertRaisesRegex(
                            Exception, "未能安全重放"
                        ):
                            handoff.execute(
                                representation.representation_id,
                                CodexCliRepresentationAnalysisProvider(
                                    provider_version="0.999.0",
                                    reasoning_effort="high",
                                    timeout_seconds=17,
                                    runner=rejected_runner,
                                ),
                            )
                        self.assertEqual(rejected_runner.calls, [])
                        tampered["deadline_ms"] = 300000
                        audit_path.write_text(
                            json.dumps(tampered), encoding="utf-8"
                        )

    def test_name_only_v1_multibatch_requires_one_historical_provider_version(
        self,
    ) -> None:
        representation, service = self.build_service(blocks=41)
        audit_root = self.root / "v1-split-audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "v1-split-atomic.jsonl"),
            audit_root,
        )
        first = self.execute_with_global_authority(
            handoff,
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
            privacy_binding=self.privacy_binding(),
            new_call_authority=2,
        )
        manifest_path = first.package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provider"] = {"name": "external-agent-codex-cli"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        package_fingerprint = _package_fingerprint(first.package)
        batches = _analysis_batches(
            _units_from_representation(
                representation, service.representation_repository
            ),
            service.batch_size,
        )
        fingerprints = {
            tuple(unit.unit_id for unit in batch.anchor_units):
            _external_agent_request(
                batch, protocol_version=EXTERNAL_AGENT_PROTOCOL_V1
            )[1]
            for batch in batches
        }
        for index, audit_path in enumerate(first.audit_paths):
            historical = json.loads(audit_path.read_text(encoding="utf-8"))
            historical["protocol_version"] = EXTERNAL_AGENT_PROTOCOL_V1
            historical["input_fingerprint"] = fingerprints[
                tuple(historical["anchor_unit_ids"])
            ]
            historical["provider_version"] = f"0.14{7 + index}.0"
            historical["package_fingerprint"] = package_fingerprint
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
                historical.pop(field)
            audit_path.write_text(json.dumps(historical), encoding="utf-8")

        replay_runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.999.0", runner=replay_runner
                ),
            )
        self.assertEqual(replay_runner.calls, [])

    def test_historical_profiled_package_rejects_package_audit_profile_drift(
        self,
    ) -> None:
        representation, service = self.build_service()
        audit_root = self.root / "historical-profile-drift-audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(
                self.root / "historical-profile-drift-atomic.jsonl"
            ),
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
        manifest_path = first.package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provider"]["reasoning_effort"] = "high"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        package_fingerprint = _package_fingerprint(first.package)
        audit = json.loads(first.audit_paths[0].read_text(encoding="utf-8"))
        audit["package_fingerprint"] = package_fingerprint
        first.audit_paths[0].write_text(json.dumps(audit), encoding="utf-8")

        replay_runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    reasoning_effort="high",
                    runner=replay_runner,
                ),
            )
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
        changed = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.148.0", runner=changed_runner
            ),
        )
        self.assertTrue(changed.replayed_existing_package)
        self.assertEqual(changed.ingestion.existing, 1)
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
        deadline_ms: int = 300000,
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
            deadline_ms=deadline_ms,
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
        provider_version: str = "codex-cli-0.147.0",
        source_id: str = "src_" + "a" * 32,
        audit_root: Path | None = None,
        blocks: int = 1,
        timeout_seconds: int = 300,
    ):
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(
            root=root / "source",
            blocks=blocks,
            source_id=source_id,
        )
        audit_root = root / "audits" if audit_root is None else audit_root
        audit_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(audit_root, 0o700)
        current_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=timeout_seconds,
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
        ordinals = tuple(range(1, len(receipt["batches"]) + 1))
        legacy_run = self.write_v31_attempts_fixture(
            recovery,
            receipt,
            ordinals,
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            audit_root,
        )
        audit_paths: list[Path] = []
        for ordinal in ordinals:
            self.write_v31_result_fixture(
                recovery,
                legacy_run,
                ordinal,
                committed=True,
                provider_version=provider_version,
                deadline_ms=timeout_seconds * 1000,
            )
            result_receipt = json.loads(
                (
                    legacy_run
                    / "results"
                    / f"batch_{ordinal:04d}"
                    / "result-receipt.json"
                ).read_text(encoding="utf-8")
            )
            record = handoff_module._record_from_payload(
                result_receipt["execution_record"]
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
            audit_paths.append(audit_path)
        return (
            representation,
            service,
            handoff,
            current_provider,
            recovery,
            legacy_run,
            audit_paths[0],
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
            ("baseline_total", 79, 300, self.semantic_window_binding()),
            ("max_new", 21, 300, self.semantic_window_binding()),
            ("absolute_cap", 101, 300, self.semantic_window_binding()),
            (None, None, 299, self.semantic_window_binding()),
            (
                None,
                None,
                300,
                replace(self.semantic_window_binding(), reviewed_git_head="invalid"),
            ),
        )
        for index, (field, value, timeout, binding) in enumerate(cases):
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
                manifest = self.write_inventory_authority(
                    handoff,
                    provider,
                    binding,
                    mutate=(
                        None
                        if field is None
                        else lambda payload, key=field, changed=value: payload.__setitem__(
                            key, changed
                        )
                    ),
                )
                before = self.tree_snapshot(root)
                with self.assertRaises(SemanticHandoffError):
                    handoff.install_global_authority(
                        provider,
                        inventory_authority_file=manifest,
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
        binding = self.semantic_window_binding()
        grant = handoff.install_global_authority(
            provider,
            inventory_authority_file=self.write_inventory_authority(
                handoff, provider, binding, labels=("0.147.0",)
            ),
            window_binding=binding,
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
            provider_version="0.147.0",
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
            ["0.147.0", "codex-cli-0.147.0"],
        )
        self.assertEqual(
            grant["historical_provider_version_counts"],
            {"0.147.0": 1, "codex-cli-0.147.0": 1},
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

    def test_global_authority_accepts_linked_historical_noncurrent_deadline(
        self,
    ) -> None:
        root = self.root / "linked-historical-120-second-deadline"
        (
            _representation,
            _service,
            handoff,
            _historical_provider,
            _recovery,
            legacy_run,
            audit_path,
        ) = self.build_linked_v31_inventory_fixture(
            root,
            timeout_seconds=120,
        )
        result_receipt = json.loads(
            (
                legacy_run
                / "results"
                / "batch_0001"
                / "result-receipt.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            json.loads(
                (legacy_run / "run-receipt.json").read_text(encoding="utf-8")
            )["execution_deadline_ms"],
            120000,
        )
        self.assertEqual(
            result_receipt["execution_record"]["deadline_ms"], 120000
        )
        self.assertEqual(
            json.loads(audit_path.read_text(encoding="utf-8"))["deadline_ms"],
            120000,
        )

        current_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        grant = self.install_historical_inventory_authority(
            handoff, current_provider
        )
        self.assertEqual(grant["legacy_attempt_inventory_count"], 1)
        self.assertEqual(grant["contract"]["execution_deadline_ms"], 300000)

    def test_standalone_historical_deadline_is_frozen_by_private_manifest(
        self,
    ) -> None:
        for drift_after_manifest in (False, True):
            with self.subTest(drift_after_manifest=drift_after_manifest):
                root = self.root / (
                    "standalone-deadline-drift"
                    if drift_after_manifest
                    else "standalone-deadline-accepted"
                )
                (
                    _representation,
                    _service,
                    handoff,
                    provider,
                    _batch,
                    audit_path,
                ) = self.build_historical_inventory_audit(root)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["deadline_ms"] = 120000
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                binding = self.semantic_window_binding()
                manifest = self.write_inventory_authority(
                    handoff, provider, binding
                )
                if drift_after_manifest:
                    audit["deadline_ms"] = 119000
                    audit_path.write_text(json.dumps(audit), encoding="utf-8")
                    before = self.tree_snapshot(handoff.audit_root)
                    with self.assertRaises(SemanticHandoffError):
                        handoff.install_global_authority(
                            provider,
                            inventory_authority_file=manifest,
                            window_binding=binding,
                        )
                    self.assertEqual(
                        self.tree_snapshot(handoff.audit_root), before
                    )
                    self.assertFalse(
                        (
                            handoff.audit_root / "semantic_global_authority"
                        ).exists()
                    )
                else:
                    grant = handoff.install_global_authority(
                        provider,
                        inventory_authority_file=manifest,
                        window_binding=binding,
                    )
                    self.assertEqual(
                        grant["legacy_attempt_inventory_count"], 1
                    )

    def test_global_authority_requires_exact_approved_historical_version_set(
        self,
    ) -> None:
        for name, approved, succeeds, error in (
            ("exact", ("codex-cli-0.147.0",), True, ""),
            ("missing", (), False, "version 集合不匹配"),
            ("extra", ("codex-cli-0.147.0", "999"), False, "manifest binding"),
            ("unsafe", ("../unsafe", "codex-cli-0.147.0"), False, "Provider label"),
            ("numeric-short", ("999",), False, "Provider label"),
            ("numeric-semver", ("999.0.0",), False, "version 不匹配"),
            ("different", ("0.148.0",), False, "version 不匹配"),
            ("wide-prefix", ("vendor-codex-cli-0.147.0",), False, "Provider label"),
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
                binding = self.semantic_window_binding()
                manifest = self.write_inventory_authority(
                    handoff, provider, binding, labels=approved
                )
                before = self.tree_snapshot(root)
                if succeeds:
                    grant = handoff.install_global_authority(
                        provider,
                        inventory_authority_file=manifest,
                        window_binding=binding,
                    )
                    self.assertEqual(
                        grant["historical_provider_versions"], list(approved)
                    )
                    self.assertEqual(
                        grant["historical_provider_version_counts"],
                        {"codex-cli-0.147.0": 1},
                    )
                else:
                    with self.assertRaisesRegex(
                        SemanticHandoffError, error
                    ):
                        handoff.install_global_authority(
                            provider,
                            inventory_authority_file=manifest,
                            window_binding=binding,
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
        original_executable = executable.read_bytes()
        binding = self.semantic_window_binding()
        grant = self.install_authority(
            handoff,
            provider,
            binding,
        )
        contract = grant["contract"]
        self.assertEqual(contract["provider"]["provider_version"], "0.147.0")
        self.assertRegex(contract["provider_binary_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(
            contract["provider_resolved_path_sha256"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertNotIn(str(executable), json.dumps(grant))
        self.assertEqual(provider.provider_start_count, 0)
        executable.write_bytes(original_executable)
        executable.chmod(0o700)
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
            self.install_authority(
                mismatch,
                CodexCliRepresentationAnalysisProvider(
                    codex_binary=str(executable),
                    provider_version="999",
                    timeout_seconds=300,
                ),
                self.semantic_window_binding(),
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
            self.install_authority(
                unsafe_handoff,
                CodexCliRepresentationAnalysisProvider(
                    codex_binary=str(unsafe_bin),
                    provider_version="0.147.0",
                    timeout_seconds=300,
                ),
                self.semantic_window_binding(),
            )
        self.assertEqual(self.tree_snapshot(unsafe_root), unsafe_before)

    def test_global_authority_post_attempt_executable_drift_is_counted_unknown(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        root = self.root / "post-attempt-executable-drift"
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
        representation, service = self.build_service(root=root / "source")
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
        original_executable = executable.read_bytes()
        binding = self.semantic_window_binding()
        self.install_authority(handoff, provider, binding)
        original_publish = handoff_module._SemanticGlobalAuthority.publish_attempt

        def publish_then_drift(authority, *args, **kwargs):
            receipt = original_publish(authority, *args, **kwargs)
            executable.write_text(
                executable.read_text(encoding="utf-8") + "# drift\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            return receipt

        with (
            patch.object(
                handoff_module._SemanticGlobalAuthority,
                "publish_attempt",
                publish_then_drift,
            ),
            self.assertRaisesRegex(
                SemanticHandoffError, "未确认新增 Durable"
            ),
        ):
            handoff.execute(
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                authority_binding=binding,
        )
        self.assertEqual(provider.provider_start_count, 0)
        executable.write_bytes(original_executable)
        executable.chmod(0o700)
        global_attempts = [
            payload
            for path in handoff.audit_root.glob(
                "semantic_run_*/attempts/batch_*.json"
            )
            if (
                payload := json.loads(path.read_text(encoding="utf-8"))
            ).get("schema_version")
            == "semantic-handoff-attempt-receipt/3.0"
        ]
        self.assertEqual(len(global_attempts), 1)
        with self.assertRaisesRegex(SemanticHandoffError, "outcome.*不确定"):
            handoff.execute(
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                authority_binding=binding,
            )

    def test_inventory_authority_manifest_is_private_exact_and_race_checked(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        root = self.root / "inventory-authority-file"
        _representation, service = self.build_service(root=root / "source")
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            root / "audits",
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        binding = self.semantic_window_binding()
        manifest = self.write_inventory_authority(handoff, provider, binding)
        manifest.chmod(0o644)
        before = self.tree_snapshot(handoff.audit_root)
        with self.assertRaises(SemanticHandoffError):
            handoff.install_global_authority(
                provider,
                inventory_authority_file=manifest,
                window_binding=binding,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before)

        manifest.chmod(0o600)
        original = handoff_module._SemanticGlobalAuthority._expected_grant
        calls = 0

        def mutate_between_scans(authority, **kwargs):
            nonlocal calls
            result = original(authority, **kwargs)
            calls += 1
            if calls == 1:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["authority_ref"] = "sha256:" + "4" * 64
                payload.pop("payload_fingerprint")
                payload["payload_fingerprint"] = _canonical_fingerprint(payload)
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                manifest.chmod(0o600)
            return result

        with (
            patch.object(
                handoff_module._SemanticGlobalAuthority,
                "_expected_grant",
                mutate_between_scans,
            ),
            self.assertRaisesRegex(SemanticHandoffError, "preflight"),
        ):
            handoff.install_global_authority(
                provider,
                inventory_authority_file=manifest,
                window_binding=binding,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before)

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
            self.install_authority(
                handoff,
                provider,
                self.semantic_window_binding(),
                labels=("0.147.0",),
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
        with self.assertRaisesRegex(
            SemanticHandoffError, "shadow|无法一一绑定"
        ):
            self.install_authority(
                linked_handoff,
                linked_provider,
                self.semantic_window_binding(),
                labels=("codex-cli-0.147.0",),
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
        receipt["provider"]["provider_version"] = "codex-cli-0.147.0"
        self.write_v31_attempt_fixture(recovery, receipt)
        batch = recovery.historical_v31_batch_contracts[0]["batch"]
        historical = CodexCliRepresentationAnalysisProvider(
            provider_version="codex-cli-0.147.0",
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

    def test_global_authority_counts_two_distinct_exact_linked_attempts(
        self,
    ) -> None:
        root = self.root / "two-linked-attempts"
        (
            _first_representation,
            _first_service,
            first_handoff,
            provider,
            _first_recovery,
            _first_run,
            _first_audit,
        ) = self.build_linked_v31_inventory_fixture(
            root,
            blocks=41,
        )
        binding = self.semantic_window_binding()
        grant = self.install_authority(
            first_handoff,
            provider,
            binding,
            labels=("codex-cli-0.147.0",),
        )
        self.assertEqual(grant["legacy_attempt_inventory_count"], 2)
        self.assertEqual(
            grant["historical_provider_version_counts"],
            {"codex-cli-0.147.0": 2},
        )
        self.assertEqual(grant["external_prior_count"], 78)

    def test_global_authority_counts_two_same_binding_runs_with_own_audits(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        root = self.root / "two-same-binding-runs-with-own-audits"
        representation, service = self.build_service(root=root / "source")
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            root / "audits",
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )

        def publish_exact(privacy_receipt: str):
            recovery = handoff_module._SemanticRecoveryRun(
                service,
                handoff.audit_root,
                representation.representation_id,
                provider,
                replace(
                    self.privacy_binding(),
                    receipt_fingerprint=privacy_receipt,
                ),
            )
            recovery.ensure_run_receipt()
            recovery.publish_attempt(1)
            provider._capture_successful_raw = True
            try:
                result = provider.analyze(recovery.batches[0])
            finally:
                provider._capture_successful_raw = False
            record = provider.execution_records[-1]
            raw_result = provider._successful_results.pop()
            RepresentationInformationService._validate_batch_result(
                recovery.batches[0], result
            )
            recovery.publish_result(1, raw_result, record)
            handoff._persist_audits(
                (record,),
                package_published=True,
                information_ingested=False,
                durable_ingestion_status="pending",
                package_fingerprint="sha256:" + "9" * 64,
                handoff_status="pending",
            )
            return recovery

        recovery_one = publish_exact("sha256:" + "7" * 64)
        recovery_two = publish_exact("sha256:" + "8" * 64)
        self.assertNotEqual(
            recovery_one.semantic_run_id, recovery_two.semantic_run_id
        )

        grant = self.install_authority(
            handoff,
            provider,
            self.semantic_window_binding(),
            labels=("0.147.0",),
        )
        self.assertEqual(grant["legacy_attempt_inventory_count"], 2)
        self.assertEqual(grant["external_prior_count"], 78)

    def test_global_authority_does_not_lend_one_related_audit_to_two_attempts(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        root = self.root / "one-audit-two-related-attempts"
        representation, service = self.build_service(root=root / "source")
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            root / "audits",
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        recovery_one = handoff_module._SemanticRecoveryRun(
            service,
            handoff.audit_root,
            representation.representation_id,
            provider,
            self.privacy_binding(),
        )
        recovery_one.ensure_run_receipt()
        recovery_one.publish_attempt(1)
        provider._capture_successful_raw = True
        try:
            result = provider.analyze(recovery_one.batches[0])
        finally:
            provider._capture_successful_raw = False
        record = provider.execution_records[-1]
        raw_result = provider._successful_results.pop()
        RepresentationInformationService._validate_batch_result(
            recovery_one.batches[0], result
        )
        recovery_one.publish_result(1, raw_result, record)
        handoff._persist_audits(
            (record,),
            package_published=True,
            information_ingested=False,
            durable_ingestion_status="pending",
            package_fingerprint="sha256:" + "9" * 64,
            handoff_status="pending",
        )
        binding = self.semantic_window_binding()
        manifest = self.write_inventory_authority(
            handoff,
            provider,
            binding,
            labels=("0.147.0",),
        )
        recovery_two = handoff_module._SemanticRecoveryRun(
            service,
            handoff.audit_root,
            representation.representation_id,
            provider,
            replace(
                self.privacy_binding(),
                receipt_fingerprint="sha256:" + "8" * 64,
            ),
        )
        recovery_two.ensure_run_receipt()
        recovery_two.publish_attempt(1)
        self.assertNotEqual(
            recovery_one.semantic_run_id, recovery_two.semantic_run_id
        )
        before = self.tree_snapshot(handoff.audit_root)
        with self.assertRaisesRegex(SemanticHandoffError, "无法一一绑定"):
            handoff.install_global_authority(
                provider,
                inventory_authority_file=manifest,
                window_binding=binding,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before)
        self.assertFalse(
            (handoff.audit_root / "semantic_global_authority").exists()
        )

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
            "run-deadline",
            "result-provider",
            "result-deadline",
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
                elif attack == "run-deadline":
                    receipt_path = legacy_run / "run-receipt.json"
                    receipt = json.loads(
                        receipt_path.read_text(encoding="utf-8")
                    )
                    receipt["execution_deadline_ms"] = 120000
                    receipt.pop("run_receipt_fingerprint")
                    receipt.pop("contract_fingerprint")
                    receipt["contract_fingerprint"] = (
                        handoff_module._fingerprint(receipt)
                    )
                    receipt["run_receipt_fingerprint"] = (
                        handoff_module._fingerprint(receipt)
                    )
                    receipt_path.write_text(
                        json.dumps(receipt), encoding="utf-8"
                    )
                elif attack in {
                    "result-provider",
                    "result-deadline",
                    "result-cleanup",
                }:
                    result = legacy_run / "results" / "batch_0001"
                    receipt_path = result / "result-receipt.json"
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    record = receipt["execution_record"]
                    if attack == "result-provider":
                        record["provider_version"] = "0.145.0"
                    elif attack == "result-deadline":
                        record["deadline_ms"] = 120000
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
            self.install_authority(
                handoff,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                ),
                self.semantic_window_binding(),
                labels=("0.147.0",),
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
                self.install_authority(first_handoff, provider, initial)
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
        grant = self.install_authority(first_handoff, provider, first_window)
        self.assertEqual(grant["external_prior_count"], 80)
        self.assertEqual(
            self.install_authority(first_handoff, provider, first_window),
            grant,
        )
        before_drift = self.tree_snapshot(shared_audits)
        with self.assertRaises(SemanticHandoffError):
            self.install_authority(
                first_handoff,
                provider,
                first_window,
                mutate=lambda payload: payload.__setitem__(
                    "authority_ref", "sha256:" + "4" * 64
                ),
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

    def test_global_attempt_summary_accepts_canonical_representation_identity(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        representation, service = self.build_service(
            root=self.root / "global-summary-source"
        )
        audit_root = self.root / "global-summary-audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "global-summary.jsonl"),
            audit_root,
        )
        runner = FakeRunner()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=runner,
        )
        binding = self.semantic_window_binding()
        self.install_authority(handoff, provider, binding)
        handoff.execute(
            representation.representation_id,
            provider,
            privacy_binding=self.privacy_binding(),
            authority_binding=binding,
        )
        handoff.install_global_authority_extension(
            provider,
            window_binding=binding,
            reviewed_git_head="7" * 40,
        )
        authority = handoff_module._SemanticGlobalAuthority(audit_root)
        base = authority._read_base_grant()
        attempts, unknown = authority._global_attempts(
            authority._effective_authority(
                base,
                authority._read_extension(base),
                authority._read_unknown_resolution(),
            )
        )
        self.assertFalse(unknown)
        latest = attempts[-1]
        production_shaped_attempts = [
            {**latest, "global_ordinal": ordinal}
            for ordinal in range(81, 177)
        ]
        provider_calls = len(runner.calls)

        with patch.object(
            handoff_module._SemanticGlobalAuthority,
            "_global_attempts",
            return_value=(production_shaped_attempts, False),
        ):
            summary = handoff.global_attempt_summary(
                representation.representation_id
            )

        self.assertEqual(
            summary,
            {
                "global_attempt_total": 176,
                "global_unknown": 0,
                "next_global_ordinal": 177,
                "absolute_cap": 1000,
            },
        )
        self.assertEqual(len(runner.calls), provider_calls)

    def test_global_attempt_summary_rejects_noncanonical_identity_before_write(
        self,
    ) -> None:
        _representation, service = self.build_service(
            root=self.root / "invalid-global-summary-source"
        )
        audit_root = self.root / "invalid-global-summary-audits"
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(
                self.root / "invalid-global-summary.jsonl"
            ),
            audit_root,
        )
        before = self.tree_snapshot(audit_root)

        with self.assertRaisesRegex(SemanticHandoffError, "identity 无效"):
            handoff.global_attempt_summary("representation-invalid")

        self.assertEqual(self.tree_snapshot(audit_root), before)

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
        self.install_authority(handoff, failing_provider, binding)
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
                self.install_authority(handoff, provider, binding)
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
            self.install_authority(handoff, provider, binding)
        self.assertTrue((authority_root / "grant.json").is_file())
        grant = self.install_authority(handoff, provider, binding)
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
        inventory_authority = self.write_inventory_authority(
            handoff,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=FakeRunner(),
            ),
            binding,
        )
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
ready_path, start_path, inventory_authority = (
    Path(value) for value in sys.argv[6:9]
)
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
    inventory_authority_file=inventory_authority,
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
                    str(inventory_authority),
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
            inventory_authority_file=inventory_authority,
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
        self.install_authority(handoff, winner_provider, binding)
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

    def test_recovery_publish_rejects_package_provider_mismatch(self) -> None:
        representation, service = self.build_service(blocks=2)
        audit_root = self.root / "recovery-provider-mismatch-audits"
        store_path = self.root / "recovery-provider-mismatch-atomic.jsonl"
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
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                provider,
                privacy_binding=self.privacy_binding(),
                new_call_authority=1,
            )
        package = service.output_root / representation.representation_id
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provider"]["reasoning_effort"] = "high"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        replay_runner = FakeRunner()
        with self.assertRaisesRegex(Exception, "未能安全收敛"):
            self.execute_with_global_authority(
                handoff,
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=replay_runner,
                ),
                privacy_binding=self.privacy_binding(),
                new_call_authority=0,
            )
        self.assertEqual(replay_runner.calls, [])
        self.assertFalse(store_path.exists())

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

    def test_cap1000_extension_is_append_only_exact_and_replay_safe(self) -> None:
        (
            handoff,
            provider,
            base_window,
            grant,
            extension,
            representation,
            grant_bytes,
            ordinal_81_bytes,
        ) = self.build_cap1000_extension_fixture(self.root / "cap1000")
        authority_root = handoff.audit_root / "semantic_global_authority"
        self.assertEqual((authority_root / "grant.json").read_bytes(), grant_bytes)
        ordinal_81_path = next(
            path
            for path in handoff.audit_root.glob(
                "semantic_run_*/attempts/*.json"
            )
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 81
        )
        self.assertEqual(ordinal_81_path.read_bytes(), ordinal_81_bytes)
        self.assertEqual(extension["base_global_authority_fingerprint"], grant[
            "global_authority_fingerprint"
        ])
        self.assertEqual(extension["activation_total"], 81)
        self.assertEqual(extension["activation_unknown_count"], 0)
        self.assertEqual(extension["previous_absolute_cap"], 100)
        self.assertEqual(extension["new_absolute_cap"], 1000)
        self.assertEqual(extension["first_authorized_ordinal"], 82)
        self.assertEqual(extension["last_authorized_ordinal"], 1000)
        self.assertEqual(
            handoff.install_global_authority_extension(
                provider,
                window_binding=base_window,
                reviewed_git_head="7" * 40,
            ),
            extension,
        )
        before_drift = self.tree_snapshot(handoff.audit_root)
        with self.assertRaisesRegex(SemanticHandoffError, "已存在且不匹配"):
            handoff.install_global_authority_extension(
                provider,
                window_binding=base_window,
                reviewed_git_head="8" * 40,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before_drift)
        with self.assertRaisesRegex(SemanticHandoffError, "window head"):
            handoff.install_global_authority_extension(
                provider,
                window_binding=replace(
                    base_window, reviewed_git_head="8" * 40
                ),
                reviewed_git_head="7" * 40,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before_drift)

        replay_runner = FakeRunner()
        replay = handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=replay_runner,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(
                base_window, reviewed_git_head="7" * 40
            ),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay_runner.calls, [])

    def test_cap1000_extension_requires_exact_activation_ledger(self) -> None:
        representation, service = self.build_service(
            root=self.root / "cap1000-invalid-source"
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(self.root / "cap1000-invalid.jsonl"),
            self.root / "cap1000-invalid-audits",
        )
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        binding = self.semantic_window_binding()
        self.install_authority(handoff, provider, binding)
        before = self.tree_snapshot(handoff.audit_root)
        with self.assertRaisesRegex(SemanticHandoffError, "activation ledger"):
            handoff.install_global_authority_extension(
                provider,
                window_binding=binding,
                reviewed_git_head="7" * 40,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before)
        self.assertFalse(
            (
                handoff.audit_root
                / "semantic_global_authority"
                / "extension-cap-1000.json"
            ).exists()
        )
        self.assertFalse(
            (service.output_root / representation.representation_id).exists()
        )

    def test_cap1000_extension_rejects_self_consistent_binding_tamper(
        self,
    ) -> None:
        (
            handoff,
            provider,
            base_window,
            _grant,
            extension,
            _representation,
            _grant_bytes,
            _ordinal_81_bytes,
        ) = self.build_cap1000_extension_fixture(self.root / "cap1000-tamper")
        extension_path = (
            handoff.audit_root
            / "semantic_global_authority"
            / "extension-cap-1000.json"
        )
        original = extension_path.read_bytes()

        mutations = {
            "base fingerprint": lambda payload: payload.__setitem__(
                "base_global_authority_fingerprint", "sha256:" + "0" * 64
            ),
            "campaign": lambda payload: payload["campaign"].__setitem__(
                "semantic_batch_size", 41
            ),
            "activation total": lambda payload: payload.__setitem__(
                "activation_total", 82
            ),
            "activation unknown": lambda payload: payload.__setitem__(
                "activation_unknown_count", 1
            ),
            "activation ordinal": lambda payload: payload.__setitem__(
                "activation_last_global_ordinal", 82
            ),
            "activation inventory": lambda payload: payload.__setitem__(
                "activation_attempt_inventory_fingerprint",
                "sha256:" + "1" * 64,
            ),
            "base head": lambda payload: payload.__setitem__(
                "base_reviewed_git_head", "8" * 40
            ),
            "current contract": lambda payload: payload[
                "execution_contract"
            ].__setitem__("execution_deadline_ms", 299000),
            "absolute cap": lambda payload: payload.__setitem__(
                "new_absolute_cap", 1001
            ),
            "ordinal range": lambda payload: payload.__setitem__(
                "first_authorized_ordinal", 81
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                payload = json.loads(original)
                mutate(payload)
                projected = dict(payload)
                projected.pop("extension_fingerprint")
                payload["extension_fingerprint"] = _canonical_fingerprint(
                    projected
                )
                extension_path.write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                with self.assertRaises(SemanticHandoffError):
                    handoff.install_global_authority_extension(
                        provider,
                        window_binding=base_window,
                        reviewed_git_head="7" * 40,
                    )
                extension_path.write_bytes(original)
                self.assertEqual(
                    json.loads(original), extension
                )

    def test_cap1000_extension_converges_visible_receipt_after_fsync_failure(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        (
            handoff,
            provider,
            base_window,
            _grant,
            extension,
            _representation,
            _grant_bytes,
            _ordinal_81_bytes,
        ) = self.build_cap1000_extension_fixture(self.root / "cap1000-crash")
        extension_path = (
            handoff.audit_root
            / "semantic_global_authority"
            / "extension-cap-1000.json"
        )
        extension_path.unlink()
        original_fsync = handoff_module._fsync_directory
        failed = False

        def fail_after_visible(path):
            nonlocal failed
            if path == extension_path.parent and extension_path.exists() and not failed:
                failed = True
                raise OSError("synthetic extension fsync interruption")
            return original_fsync(path)

        with (
            patch.object(handoff_module, "_fsync_directory", fail_after_visible),
            self.assertRaises(OSError),
        ):
            handoff.install_global_authority_extension(
                provider,
                window_binding=base_window,
                reviewed_git_head="7" * 40,
            )
        self.assertTrue(extension_path.is_file())
        self.assertEqual(
            handoff.install_global_authority_extension(
                provider,
                window_binding=base_window,
                reviewed_git_head="7" * 40,
            ),
            extension,
        )

    def test_cap1000_extension_concurrent_exact_install_is_idempotent(
        self,
    ) -> None:
        (
            handoff,
            provider,
            base_window,
            _grant,
            extension,
            _representation,
            _grant_bytes,
            _ordinal_81_bytes,
        ) = self.build_cap1000_extension_fixture(
            self.root / "cap1000-concurrent"
        )
        extension_path = (
            handoff.audit_root
            / "semantic_global_authority"
            / "extension-cap-1000.json"
        )
        extension_path.unlink()
        start = threading.Barrier(3)
        outcomes: list[dict[str, object]] = []

        def install() -> None:
            start.wait()
            try:
                observed = handoff.install_global_authority_extension(
                    provider,
                    window_binding=base_window,
                    reviewed_git_head="7" * 40,
                )
                outcomes.append({"state": "passed", "payload": observed})
            except BaseException as error:  # noqa: BLE001 - concurrency evidence.
                outcomes.append({"state": "failed", "error": repr(error)})

        threads = [threading.Thread(target=install) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(
            sorted(str(outcome["state"]) for outcome in outcomes),
            ["passed", "passed"],
            outcomes,
        )
        self.assertTrue(
            all(outcome.get("payload") == extension for outcome in outcomes),
            outcomes,
        )
        self.assertEqual(
            json.loads(extension_path.read_text(encoding="utf-8")), extension
        )

    def test_cap1000_extension_allows_82_through_1000_then_blocks_1001(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        shared = self.root / "cap1000-boundary"
        (
            _handoff,
            _provider,
            base_window,
            _grant,
            extension,
            _representation,
            _grant_bytes,
            _ordinal_81_bytes,
        ) = self.build_cap1000_extension_fixture(shared, batch_size=1)
        representation, service = self.build_service(
            root=shared / "remaining-source",
            source_id="src_" + "f" * 32,
            blocks=1,
            batch_size=1,
        )
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(shared / "remaining-atomic.jsonl"),
            shared / "audits",
        )
        completed = SemanticCompletedWindowBinding(
            window_run_id=base_window.window_run_id,
            window_plan_fingerprint=base_window.window_plan_fingerprint,
            window_plan_receipt_fingerprint=(
                base_window.window_plan_receipt_fingerprint
            ),
            window_status_fingerprint="sha256:" + "a" * 64,
            window_after_cursor=base_window.window_after_cursor,
            window_upper_cursor=base_window.window_upper_cursor,
        )
        next_window = replace(
            base_window,
            window_run_id="run_" + "8" * 32,
            window_plan_fingerprint="sha256:" + "b" * 64,
            window_plan_receipt_fingerprint="sha256:" + "c" * 64,
            window_after_cursor=base_window.window_upper_cursor,
            window_upper_cursor=(2, "next", "next"),
            previous_checkpoint_fingerprint="sha256:" + "d" * 64,
            completed_window_chain=(completed,),
            reviewed_git_head="7" * 40,
        )
        runner = FakeRunner()
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=next_window,
        )
        self.assertEqual(len(runner.calls), 1)
        attempts = sorted(
            (
                json.loads(path.read_text(encoding="utf-8"))
                for path in (shared / "audits").glob(
                    "semantic_run_*/attempts/*.json"
                )
            ),
            key=lambda item: item["global_ordinal"],
        )
        self.assertEqual(
            [item["global_ordinal"] for item in attempts],
            [81, 82],
        )
        self.assertTrue(
            all(
                item["global_authority_fingerprint"]
                == extension["extension_fingerprint"]
                for item in attempts[1:]
            )
        )
        at_cap = [
            attempts[0],
            *(
                {**attempts[1], "global_ordinal": ordinal}
                for ordinal in range(82, 1001)
            ),
        ]

        blocked_representation, blocked_service = self.build_service(
            root=shared / "blocked-source",
            source_id="src_" + "9" * 32,
            batch_size=1,
        )
        blocked_handoff = ExternalAgentSemanticHandoffService(
            blocked_service,
            JsonlAtomicInformationStore(shared / "blocked-atomic.jsonl"),
            shared / "audits",
        )
        blocked_runner = FakeRunner()
        before = self.tree_snapshot(shared / "audits")
        with (
            patch.object(
                handoff_module._SemanticGlobalAuthority,
                "_global_attempts",
                return_value=(at_cap, False),
            ),
            self.assertRaisesRegex(SemanticHandoffError, "额度不足"),
        ):
            blocked_handoff.execute(
                blocked_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=blocked_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=next_window,
            )
        self.assertEqual(blocked_runner.calls, [])
        self.assertEqual(self.tree_snapshot(shared / "audits"), before)

    def test_unknown_166_resolution_is_append_only_and_continues_at_167(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        (
            failed,
            _provider,
            current_window,
            failed_representation,
            digest,
            manifest_path,
        ) = self.build_ordinal_166_unknown_fixture(self.root / "unknown-166")
        next_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        status_fingerprint = "sha256:" + "a" * 64
        calls: list[str] = []

        authority = handoff_module._SemanticGlobalAuthority(failed.audit_root)
        base = authority._read_base_grant()
        extension = authority._read_extension(base)
        self.assertIsNotNone(extension)
        previous = authority._effective_authority_before_resolution(
            base, extension
        )
        attempts, unknown = authority._global_attempts(previous)
        self.assertTrue(unknown)
        attempt_166 = attempts[-1]
        failed_run = failed.audit_root / str(
            attempt_166["semantic_run_id"]
        )
        run_payload = json.loads(
            (failed_run / "run-receipt.json").read_text(encoding="utf-8")
        )
        failed_batch = run_payload["batches"][
            int(attempt_166["batch_ordinal"]) - 1
        ]
        failure_audit_path, failure_audit = authority._matching_failed_audit(
            run_payload=run_payload,
            batch_receipt=failed_batch,
        )
        resolution_path = (
            failed.audit_root
            / "semantic_global_authority"
            / "unknown-resolution-ordinal-0166.json"
        )
        status_path = self.root / "unknown-166-failed-closed-status.json"

        def current_digest() -> dict[str, object]:
            repository = (
                failed.representation_service.representation_repository
            )
            representation = repository.get(
                failed_representation.representation_id
            )
            verification = repository.verify(representation.representation_id)
            if not verification.verified:
                raise SemanticHandoffError(
                    "synthetic Representation replay verification failed"
                )
            inventory = []
            for artifact in representation.artifacts:
                raw = repository.read_artifact(
                    representation.representation_id, artifact.artifact_id
                )
                if (
                    "sha256:" + hashlib.sha256(raw).hexdigest()
                    != artifact.content_hash
                    or len(raw) != artifact.size_bytes
                ):
                    raise SemanticHandoffError(
                        "synthetic artifact replay verification failed"
                    )
                inventory.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "content_hash": artifact.content_hash,
                    }
                )
            return {
                **{
                    key: value
                    for key, value in digest.items()
                    if key
                    not in {
                        "representation_manifest",
                        "representation_artifact_inventory_fingerprint",
                    }
                },
                "representation_manifest": representation.to_manifest_dict(),
                "representation_artifact_inventory_fingerprint": (
                    _canonical_fingerprint(inventory)
                ),
            }

        def commit(
            resolution_id: str,
        ) -> tuple[str, dict[str, object]]:
            calls.append(resolution_id)
            observed_digest = current_digest()
            if observed_digest != digest:
                raise SemanticHandoffError(
                    "synthetic Representation pre-status drift"
                )
            expected_status = {
                "resolution_id": resolution_id,
                "state": "failed_closed",
            }
            if status_path.exists():
                self.assertEqual(
                    json.loads(status_path.read_text(encoding="utf-8")),
                    expected_status,
                )
            else:
                status_path.write_text(
                    json.dumps(expected_status), encoding="utf-8"
                )
                status_path.chmod(0o600)
            return status_fingerprint, current_digest()

        manifest_bytes = manifest_path.read_bytes()
        for label, mutate in (
            (
                "generated_at",
                lambda payload: payload["digest"][
                    "representation_manifest"
                ]["representation"].update(
                    {"generated_at": "2026-08-20T00:00:00.000Z"}
                ),
            ),
            (
                "inventory_fingerprint",
                lambda payload: payload["digest"].update(
                    {
                        "representation_artifact_inventory_fingerprint": (
                            "sha256:" + "0" * 64
                        )
                    }
                ),
            ),
        ):
            with self.subTest(prewrite_manifest_drift=label):
                tampered = json.loads(manifest_bytes)
                mutate(tampered)
                without_fingerprint = dict(tampered)
                without_fingerprint.pop("payload_fingerprint")
                tampered["payload_fingerprint"] = _canonical_fingerprint(
                    without_fingerprint
                )
                manifest_path.write_text(
                    json.dumps(tampered), encoding="utf-8"
                )
                manifest_path.chmod(0o600)
                before = self.tree_snapshot(failed.audit_root)
                with self.assertRaises(SemanticHandoffError):
                    failed.resolve_unknown(
                        next_provider,
                        authority_manifest_file=manifest_path,
                        reviewed_git_head="8" * 40,
                        digest_binding=digest,
                        commit_failed_closed_status=commit,
                    )
                self.assertEqual(self.tree_snapshot(failed.audit_root), before)
                self.assertFalse(status_path.exists())
                self.assertFalse(resolution_path.exists())
                self.assertEqual(calls, [])
        manifest_path.write_bytes(manifest_bytes)
        manifest_path.chmod(0o600)

        recovery = handoff_module._SemanticRecoveryRun(
            failed.representation_service,
            failed.audit_root,
            failed_representation.representation_id,
            next_provider,
            self.privacy_binding(),
            global_authority=authority,
            window_binding=current_window,
        )
        self.assertEqual(len(recovery.batches), 2)
        recovery._validated_global_grant = previous
        attempt_167_path = recovery.attempts_dir / "batch_0002.json"
        attempt_167 = recovery._global_attempt_payload(
            2,
            attempt_nonce="f" * 64,
            global_ordinal=167,
            grant=previous,
            window=current_window,
        )
        original_publish = handoff_module._publish_private_json_marker
        original_publish(attempt_167_path, attempt_167)
        parseable_runner = FakeRunner()
        parseable_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=parseable_runner,
        )
        parseable_provider._capture_successful_raw = True
        try:
            parseable_result = parseable_provider.analyze(recovery.batches[1])
        finally:
            parseable_provider._capture_successful_raw = False
        RepresentationInformationService._validate_batch_result(
            recovery.batches[1], parseable_result
        )
        recovery.publish_result(
            2,
            parseable_provider._successful_results.pop(),
            parseable_provider.execution_records[-1],
        )
        before_167_reject = self.tree_snapshot(failed.audit_root)
        with self.assertRaises(SemanticHandoffError):
            failed.resolve_unknown(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="8" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            )
        self.assertEqual(
            self.tree_snapshot(failed.audit_root), before_167_reject
        )
        self.assertFalse(status_path.exists())
        self.assertFalse(resolution_path.exists())
        self.assertEqual(calls, [])
        shutil.rmtree(recovery._result_path(2))
        attempt_167_path.unlink()
        if recovery.results_dir.exists() and not any(
            recovery.results_dir.iterdir()
        ):
            recovery.results_dir.rmdir()

        def assert_post_status_drift_rejected(
            label: str,
            mutate,
            restore,
        ) -> None:
            def commit_with_drift(
                resolution_id: str,
            ) -> tuple[str, dict[str, object]]:
                fingerprint, binding = commit(resolution_id)
                mutate()
                return fingerprint, binding

            before_records = len(next_provider.execution_records)
            with self.subTest(post_status_drift=label):
                try:
                    with self.assertRaises(SemanticHandoffError):
                        failed.resolve_unknown(
                            next_provider,
                            authority_manifest_file=manifest_path,
                            reviewed_git_head="8" * 40,
                            digest_binding=digest,
                            commit_failed_closed_status=commit_with_drift,
                        )
                    self.assertEqual(
                        json.loads(status_path.read_text(encoding="utf-8"))[
                            "state"
                        ],
                        "failed_closed",
                    )
                    self.assertFalse(resolution_path.exists())
                    self.assertEqual(
                        len(next_provider.execution_records), before_records
                    )
                finally:
                    restore()

        result_path = failed_run / "results" / "batch_0001"

        def publish_sudden_result() -> None:
            result_path.parent.mkdir(mode=0o700, exist_ok=True)
            result_path.write_bytes(b"synthetic-result-drift")

        def remove_sudden_result() -> None:
            result_path.unlink(missing_ok=True)
            if result_path.parent.exists() and not any(
                result_path.parent.iterdir()
            ):
                result_path.parent.rmdir()

        assert_post_status_drift_rejected(
            "result_appeared",
            publish_sudden_result,
            remove_sudden_result,
        )

        assert_post_status_drift_rejected(
            "attempt_167",
            lambda: original_publish(attempt_167_path, attempt_167),
            lambda: attempt_167_path.unlink(missing_ok=True),
        )

        shadow_run_id = "run_" + "e" * 32
        shadow_audit_path = (
            failed.audit_root / shadow_run_id / "processing-run-audit.json"
        )

        def publish_shadow_audit() -> None:
            shadow_audit_path.parent.mkdir(mode=0o700)
            shadow = dict(failure_audit)
            shadow["processing_run_id"] = shadow_run_id
            shadow_audit_path.write_text(
                json.dumps(shadow), encoding="utf-8"
            )
            shadow_audit_path.chmod(0o600)

        assert_post_status_drift_rejected(
            "shadow_audit",
            publish_shadow_audit,
            lambda: shutil.rmtree(shadow_audit_path.parent),
        )

        self.assertTrue(failure_audit_path.exists())

        def interrupt_receipt(path, payload):
            if path.name == "unknown-resolution-ordinal-0166.json":
                raise OSError("synthetic receipt interruption")
            return original_publish(path, payload)

        with (
            patch.object(
                handoff_module,
                "_publish_private_json_marker",
                interrupt_receipt,
            ),
            self.assertRaises(OSError),
        ):
            failed.resolve_unknown(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="8" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            )

        def interrupt_after_receipt(path, payload):
            original_publish(path, payload)
            if path.name == "unknown-resolution-ordinal-0166.json":
                raise OSError("synthetic post-receipt interruption")

        with (
            patch.object(
                handoff_module,
                "_publish_private_json_marker",
                interrupt_after_receipt,
            ),
            self.assertRaises(OSError),
        ):
            failed.resolve_unknown(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="8" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            )
        receipt = failed.resolve_unknown(
            next_provider,
            authority_manifest_file=manifest_path,
            reviewed_git_head="8" * 40,
            digest_binding=digest,
            commit_failed_closed_status=commit,
        )
        self.assertEqual(receipt["global_ordinal"], 166)
        self.assertTrue(receipt["preserved_but_unabsorbed"])
        self.assertEqual(receipt["continuation"]["next_global_ordinal"], 167)
        self.assertEqual(next_provider.execution_records, [])
        self.assertEqual(
            failed.resolve_unknown(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="8" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            ),
            receipt,
        )
        failed.validate_unknown_resolution_digest(
            digest_binding=digest,
            failed_closed_status_fingerprint=status_fingerprint,
            resolution_id=receipt["resolution_id"],
        )
        blocked_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            failed.execute(
                failed_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=blocked_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=replace(
                    current_window, reviewed_git_head="8" * 40
                ),
            )
        self.assertEqual(blocked_runner.calls, [])

        next_root = self.root / "unknown-166" / "ordinal-0167"
        representation, service = self.build_service(
            root=next_root,
            source_id="src_" + "f" * 32,
        )
        runner_167 = FakeRunner()
        ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(next_root / "atomic.jsonl"),
            failed.audit_root,
        ).execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner_167,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(current_window, reviewed_git_head="8" * 40),
        )
        self.assertEqual(len(runner_167.calls), 1)
        attempts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in failed.audit_root.glob("semantic_run_*/attempts/*.json")
            if json.loads(path.read_text(encoding="utf-8")).get(
                "schema_version"
            )
            == "semantic-handoff-attempt-receipt/3.0"
        ]
        self.assertEqual(max(item["global_ordinal"] for item in attempts), 167)

    def test_maintenance_continuation_is_single_exact_and_replay_safe(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        root = self.root / "maintenance-continuation"
        (
            failed,
            _provider,
            previous_window,
            _failed_representation,
            digest,
            manifest_path,
        ) = self.build_ordinal_166_unknown_fixture(root)
        resolution_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        failed.resolve_unknown(
            resolution_provider,
            authority_manifest_file=manifest_path,
            reviewed_git_head="8" * 40,
            digest_binding=digest,
            commit_failed_closed_status=lambda _resolution_id: (
                "sha256:" + "a" * 64,
                digest,
            ),
        )
        active_window = replace(previous_window, reviewed_git_head="8" * 40)
        latest_handoff = None
        latest_representation = None
        for ordinal in range(167, 177):
            item_root = root / f"ordinal-{ordinal:04d}"
            representation, service = self.build_service(
                root=item_root,
                source_id=f"src_{ordinal:032x}",
            )
            candidate = ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(item_root / "atomic.jsonl"),
                failed.audit_root,
            )
            runner = FakeRunner()
            candidate.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=active_window,
            )
            self.assertEqual(len(runner.calls), 1)
            latest_handoff = candidate
            latest_representation = representation
        assert latest_handoff is not None and latest_representation is not None
        authority_root = failed.audit_root / "semantic_global_authority"
        protected = {
            path.relative_to(failed.audit_root).as_posix(): path.read_bytes()
            for path in (
                authority_root / "grant.json",
                authority_root / "extension-cap-1000.json",
                authority_root / "unknown-resolution-ordinal-0166.json",
                *sorted(failed.audit_root.glob("semantic_run_*/attempts/*.json")),
                *sorted(
                    failed.audit_root.glob(
                        "semantic_run_*/results/*/result-receipt.json"
                    )
                ),
            )
        }
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/127"
            "#issuecomment-1234567890"
        )
        install_runner = FakeRunner()
        install_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=install_runner,
        )

        original_publish = handoff_module._publish_private_json_marker

        def interrupt_after_publish(path, payload):
            original_publish(path, payload)
            if path.name == "maintenance-continuation.json":
                raise OSError("synthetic post-receipt interruption")

        with (
            patch.object(
                handoff_module,
                "_publish_private_json_marker",
                interrupt_after_publish,
            ),
            self.assertRaisesRegex(OSError, "post-receipt interruption"),
        ):
            failed.install_maintenance_continuation(
                install_provider,
                window_binding=active_window,
                reviewed_git_head="9" * 40,
                authority_ref=authority_ref,
            )

        continuation = failed.install_maintenance_continuation(
            install_provider,
            window_binding=active_window,
            reviewed_git_head="9" * 40,
            authority_ref=authority_ref,
        )

        self.assertEqual(install_runner.calls, [])
        self.assertEqual(continuation["activation_total"], 176)
        self.assertEqual(continuation["activation_unknown_count"], 0)
        self.assertEqual(continuation["activation_last_global_ordinal"], 176)
        self.assertEqual(continuation["next_global_ordinal"], 177)
        self.assertEqual(continuation["absolute_cap"], 1000)
        self.assertEqual(
            continuation["previous_execution_contract"],
            continuation["execution_contract"],
        )
        self.assertEqual(
            {
                path.relative_to(failed.audit_root).as_posix(): path.read_bytes()
                for path in (
                    authority_root / "grant.json",
                    authority_root / "extension-cap-1000.json",
                    authority_root / "unknown-resolution-ordinal-0166.json",
                    *sorted(
                        failed.audit_root.glob("semantic_run_*/attempts/*.json")
                    ),
                    *sorted(
                        failed.audit_root.glob(
                            "semantic_run_*/results/*/result-receipt.json"
                        )
                    ),
                )
            },
            protected,
        )
        self.assertEqual(
            failed.install_maintenance_continuation(
                install_provider,
                window_binding=replace(
                    active_window, reviewed_git_head="9" * 40
                ),
                reviewed_git_head="9" * 40,
                authority_ref=authority_ref,
            ),
            continuation,
        )
        for kwargs in (
            {"reviewed_git_head": "a" * 40, "authority_ref": authority_ref},
            {
                "reviewed_git_head": "9" * 40,
                "authority_ref": authority_ref.replace(
                    "1234567890", "1234567891"
                ),
            },
        ):
            with self.assertRaisesRegex(
                SemanticHandoffError, "已存在且不匹配"
            ):
                failed.install_maintenance_continuation(
                    install_provider,
                    window_binding=replace(
                        active_window, reviewed_git_head="9" * 40
                    ),
                    **kwargs,
                )

        with self.assertRaises(SemanticHandoffError):
            failed.install_maintenance_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=299,
                    runner=FakeRunner(),
                ),
                window_binding=replace(
                    active_window, reviewed_git_head="9" * 40
                ),
                reviewed_git_head="9" * 40,
                authority_ref=authority_ref,
            )
        with self.assertRaisesRegex(
            SemanticHandoffError, "已存在且不匹配"
        ):
            failed.install_maintenance_continuation(
                install_provider,
                window_binding=replace(
                    active_window,
                    reviewed_git_head="9" * 40,
                    capture_provider_version="synthetic-capture-2.0",
                ),
                reviewed_git_head="9" * 40,
                authority_ref=authority_ref,
            )
        authority = handoff_module._SemanticGlobalAuthority(failed.audit_root)
        base = authority._read_base_grant()
        extension = authority._read_extension(base)
        resolution = authority._read_unknown_resolution()
        observed_continuation = authority._read_maintenance_continuation(
            base, extension, resolution
        )
        effective = authority._effective_authority(
            base, extension, resolution, observed_continuation
        )
        attempts, unknown = authority._global_attempts(effective)
        self.assertFalse(unknown)
        with (
            patch.object(
                handoff_module._SemanticGlobalAuthority,
                "_global_attempts",
                return_value=(attempts[:-1], False),
            ),
            self.assertRaisesRegex(
                SemanticHandoffError, "已存在且不匹配"
            ),
        ):
            failed.install_maintenance_continuation(
                install_provider,
                window_binding=replace(
                    active_window, reviewed_git_head="9" * 40
                ),
                reviewed_git_head="9" * 40,
                authority_ref=authority_ref,
            )

        replay_runner = FakeRunner()
        replay = latest_handoff.execute(
            latest_representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=replay_runner,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(active_window, reviewed_git_head="9" * 40),
        )
        self.assertTrue(replay.replayed_existing_package)
        self.assertEqual(replay_runner.calls, [])

        next_root = root / "ordinal-0177"
        next_representation, next_service = self.build_service(
            root=next_root,
            source_id="src_" + "f" * 32,
        )
        next_handoff = ExternalAgentSemanticHandoffService(
            next_service,
            JsonlAtomicInformationStore(next_root / "atomic.jsonl"),
            failed.audit_root,
        )
        old_head_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            next_handoff.execute(
                next_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=old_head_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=active_window,
            )
        self.assertEqual(old_head_runner.calls, [])
        next_runner = FakeRunner()
        next_handoff.execute(
            next_representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=next_runner,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(active_window, reviewed_git_head="9" * 40),
        )
        self.assertEqual(len(next_runner.calls), 1)
        attempt_177 = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in failed.audit_root.glob("semantic_run_*/attempts/*.json")
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 177
        )
        self.assertEqual(
            attempt_177["global_authority_fingerprint"],
            continuation["continuation_fingerprint"],
        )

    def test_timeout_212_resolution_is_zero_call_and_continues_at_213(
        self,
    ) -> None:
        import archeos.semantic_handoff as handoff_module

        root = self.root / "timeout-212"
        (
            handoff,
            timeout_provider,
            previous_window,
            representation,
            digest,
            manifest_path,
            audit_path,
            diagnostic_path,
        ) = self.build_ordinal_212_timeout_fixture(root)
        authority_root = handoff.audit_root / "semantic_global_authority"
        protected = {
            path.relative_to(handoff.audit_root).as_posix(): path.read_bytes()
            for path in (
                authority_root / "grant.json",
                authority_root / "extension-cap-1000.json",
                authority_root / "unknown-resolution-ordinal-0166.json",
                authority_root / "maintenance-continuation.json",
                *sorted(handoff.audit_root.glob("semantic_run_*/attempts/*.json")),
                *sorted(
                    handoff.audit_root.glob(
                        "semantic_run_*/results/*/result-receipt.json"
                    )
                ),
            )
        }
        next_runner = FakeRunner()
        next_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=next_runner,
            diagnostic_root=timeout_provider.diagnostic_root,
        )
        status_path = root / "failed-closed-status.json"
        calls: list[str] = []

        def commit(resolution_id: str):
            calls.append(resolution_id)
            status = {"resolution_id": resolution_id, "state": "failed_closed"}
            if status_path.exists():
                self.assertEqual(
                    json.loads(status_path.read_text(encoding="utf-8")),
                    status,
                )
            else:
                status_path.write_text(json.dumps(status), encoding="utf-8")
                status_path.chmod(0o600)
            return "sha256:" + "b" * 64, digest

        original_metadata = diagnostic_path.read_bytes()
        diagnostic_path.write_text("{}", encoding="utf-8")
        before_drift = self.tree_snapshot(handoff.audit_root)
        with self.assertRaises(SemanticHandoffError):
            handoff.resolve_timeout_212(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="a" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before_drift)
        self.assertFalse(status_path.exists())
        self.assertEqual(calls, [])
        diagnostic_path.write_bytes(original_metadata)

        original_audit = audit_path.read_bytes()
        audit = json.loads(original_audit)
        audit["process_cleanup_status"] = "failed"
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        with self.assertRaises(SemanticHandoffError):
            handoff.resolve_timeout_212(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="a" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            )
        self.assertFalse(status_path.exists())
        audit_path.write_bytes(original_audit)

        original_publish = handoff_module._publish_private_json_marker

        def interrupt_receipt(path, payload):
            if path.name == "unknown-resolution-ordinal-0212.json":
                raise OSError("synthetic receipt interruption")
            return original_publish(path, payload)

        with (
            patch.object(
                handoff_module,
                "_publish_private_json_marker",
                interrupt_receipt,
            ),
            self.assertRaisesRegex(OSError, "receipt interruption"),
        ):
            handoff.resolve_timeout_212(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="a" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            )
        self.assertTrue(status_path.is_file())
        self.assertFalse(
            (authority_root / "unknown-resolution-ordinal-0212.json").exists()
        )

        receipt = handoff.resolve_timeout_212(
            next_provider,
            authority_manifest_file=manifest_path,
            reviewed_git_head="a" * 40,
            digest_binding=digest,
            commit_failed_closed_status=commit,
        )
        self.assertEqual(next_runner.calls, [])
        self.assertEqual(receipt["global_ordinal"], 212)
        self.assertEqual(receipt["activation_total"], 212)
        self.assertEqual(receipt["activation_unknown_count"], 1)
        self.assertEqual(receipt["continuation"]["next_global_ordinal"], 213)
        self.assertTrue(receipt["preserved_but_unabsorbed"])
        self.assertEqual(
            handoff.resolve_timeout_212(
                next_provider,
                authority_manifest_file=manifest_path,
                reviewed_git_head="a" * 40,
                digest_binding=digest,
                commit_failed_closed_status=commit,
            ),
            receipt,
        )
        handoff.validate_timeout_212_resolution_digest(
            digest_binding=digest,
            failed_closed_status_fingerprint="sha256:" + "b" * 64,
            resolution_id=receipt["resolution_id"],
        )
        self.assertEqual(
            {
                path.relative_to(handoff.audit_root).as_posix(): path.read_bytes()
                for path in (
                    authority_root / "grant.json",
                    authority_root / "extension-cap-1000.json",
                    authority_root / "unknown-resolution-ordinal-0166.json",
                    authority_root / "maintenance-continuation.json",
                    *sorted(
                        handoff.audit_root.glob(
                            "semantic_run_*/attempts/*.json"
                        )
                    ),
                    *sorted(
                        handoff.audit_root.glob(
                            "semantic_run_*/results/*/result-receipt.json"
                        )
                    ),
                )
            },
            protected,
        )
        self.assertFalse(
            (
                handoff.representation_service.output_root
                / representation.representation_id
            ).exists()
        )

        next_root = root / "ordinal-0213"
        next_representation, next_service = self.build_service(
            root=next_root,
            source_id="src_" + "f" * 32,
        )
        next_handoff = ExternalAgentSemanticHandoffService(
            next_service,
            JsonlAtomicInformationStore(next_root / "atomic.jsonl"),
            handoff.audit_root,
        )
        old_head_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            next_handoff.execute(
                next_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=old_head_runner,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=previous_window,
            )
        self.assertEqual(old_head_runner.calls, [])
        runner_213 = FakeRunner()
        next_handoff.execute(
            next_representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner_213,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(
                previous_window, reviewed_git_head="a" * 40
            ),
        )
        self.assertEqual(len(runner_213.calls), 1)
        attempt_213 = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in handoff.audit_root.glob(
                "semantic_run_*/attempts/*.json"
            )
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 213
        )
        self.assertEqual(
            attempt_213["global_authority_fingerprint"],
            receipt["resolution_receipt_fingerprint"],
        )

    def test_reviewed_head_continuation_mismatch_is_zero_call_fail_closed(
        self,
    ) -> None:
        root = self.root / "reviewed-head-continuation-mismatch"
        representation, service = self.build_service(root=root)
        handoff = ExternalAgentSemanticHandoffService(
            service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            root / "audits",
        )
        binding = self.semantic_window_binding()
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
        )
        self.install_authority(handoff, provider, binding)
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/176"
            "#issuecomment-5402000000"
        )
        active_run = {
            "run_id": binding.window_run_id,
            "plan_fingerprint": binding.window_plan_fingerprint,
            "capture_receipt_fingerprint": "sha256:" + "2" * 64,
            "status_fingerprint": "sha256:" + "3" * 64,
        }
        receipt = handoff.install_reviewed_head_continuation(
            provider,
            window_binding=binding,
            reviewed_git_head="f" * 40,
            authority_ref=authority_ref,
            active_run_binding=active_run,
        )
        self.assertEqual(receipt["activation_unknown_count"], 0)
        before = self.tree_snapshot(handoff.audit_root)
        cases = (
            {
                "reviewed_git_head": "e" * 40,
                "active_run_binding": active_run,
                "provider": provider,
            },
            {
                "reviewed_git_head": "f" * 40,
                "active_run_binding": {
                    **active_run,
                    "status_fingerprint": "sha256:" + "4" * 64,
                },
                "provider": provider,
            },
            {
                "reviewed_git_head": "f" * 40,
                "active_run_binding": active_run,
                "provider": CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=600,
                    runner=FakeRunner(),
                ),
            },
            {
                "reviewed_git_head": "f" * 40,
                "active_run_binding": active_run,
                "provider": provider,
                "window_binding": replace(
                    binding,
                    reviewed_git_head="f" * 40,
                    window_upper_cursor=(2, "drift", "drift"),
                ),
            },
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(
                SemanticHandoffError
            ):
                handoff.install_reviewed_head_continuation(
                    case["provider"],
                    window_binding=case.get(
                        "window_binding",
                        replace(binding, reviewed_git_head="f" * 40),
                    ),
                    reviewed_git_head=case["reviewed_git_head"],
                    authority_ref=authority_ref,
                    active_run_binding=case["active_run_binding"],
                )
            self.assertEqual(case["provider"].runner.calls, [])
            self.assertEqual(self.tree_snapshot(handoff.audit_root), before)
        with (
            patch.object(
                _SemanticGlobalAuthority,
                "_global_attempts",
                return_value=([], True),
            ),
            self.assertRaises(SemanticHandoffError),
        ):
            handoff.install_reviewed_head_continuation(
                provider,
                window_binding=replace(
                    binding, reviewed_git_head="f" * 40
                ),
                reviewed_git_head="f" * 40,
                authority_ref=authority_ref,
                active_run_binding=active_run,
            )
        self.assertEqual(self.tree_snapshot(handoff.audit_root), before)
        next_runner = FakeRunner()
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=next_runner,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(
                binding, reviewed_git_head="f" * 40
            ),
        )
        attempt = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in handoff.audit_root.glob("semantic_run_*/attempts/*.json")
        )
        self.assertEqual(len(next_runner.calls), 1)
        self.assertEqual(attempt["global_ordinal"], 81)
        self.assertEqual(
            attempt["global_authority_fingerprint"],
            receipt["continuation_fingerprint"],
        )
        retry_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            handoff.install_reviewed_head_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=retry_runner,
                ),
                window_binding=replace(
                    binding, reviewed_git_head="f" * 40
                ),
                reviewed_git_head="f" * 40,
                authority_ref=authority_ref,
                active_run_binding=active_run,
            )
        self.assertEqual(retry_runner.calls, [])

    def test_issue_135_continuation_is_zero_call_and_starts_at_221(
        self,
    ) -> None:
        root = self.root / "issue-135-continuation"
        (
            handoff,
            timeout_provider,
            previous_window,
            _representation,
            digest,
            manifest_path,
            _audit_path,
            _diagnostic_path,
        ) = self.build_ordinal_212_timeout_fixture(root)
        previous_head = "deaee94fe8c87ec84505a7de10d6f8d35eec87a5"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["continuation"]["reviewed_git_head"] = previous_head
        payload = dict(manifest)
        payload.pop("payload_fingerprint")
        manifest["payload_fingerprint"] = _canonical_fingerprint(payload)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0",
            timeout_seconds=300,
            runner=FakeRunner(),
            diagnostic_root=timeout_provider.diagnostic_root,
        )
        status_path = root / "issue-135-failed-closed-status.json"

        def commit(resolution_id: str):
            status = {"resolution_id": resolution_id, "state": "failed_closed"}
            status_path.write_text(json.dumps(status), encoding="utf-8")
            status_path.chmod(0o600)
            return "sha256:" + "b" * 64, digest

        handoff.resolve_timeout_212(
            provider,
            authority_manifest_file=manifest_path,
            reviewed_git_head=previous_head,
            digest_binding=digest,
            commit_failed_closed_status=commit,
        )
        active_window = replace(
            previous_window, reviewed_git_head=previous_head
        )
        latest_handoff = handoff
        for ordinal in range(213, 221):
            item_root = root / f"ordinal-{ordinal:04d}"
            representation, service = self.build_service(
                root=item_root,
                source_id=f"src_{ordinal:032x}",
            )
            latest_handoff = ExternalAgentSemanticHandoffService(
                service,
                JsonlAtomicInformationStore(item_root / "atomic.jsonl"),
                handoff.audit_root,
            )
            runner = FakeRunner()
            latest_handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=active_window,
            )
            self.assertEqual(len(runner.calls), 1)

        install_runner = FakeRunner()
        new_head = "b" * 40
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
            "#issuecomment-5353999999"
        )
        continuation = latest_handoff.install_batch_governance_continuation(
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=install_runner,
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            window_binding=active_window,
            reviewed_git_head=new_head,
            authority_ref=authority_ref,
        )

        self.assertEqual(install_runner.calls, [])
        self.assertEqual(continuation["activation_total"], 220)
        self.assertEqual(continuation["activation_unknown_count"], 0)
        self.assertEqual(continuation["next_global_ordinal"], 221)
        self.assertEqual(
            continuation["previous_execution_contract"],
            continuation["execution_contract"],
        )
        latest_handoff.validate_timeout_212_resolution_digest(
            digest_binding=digest,
            failed_closed_status_fingerprint="sha256:" + "b" * 64,
            resolution_id=json.loads(
                (
                    handoff.audit_root
                    / "semantic_global_authority"
                    / "unknown-resolution-ordinal-0212.json"
                ).read_text(encoding="utf-8")
            )["resolution_id"],
        )
        unknown_166 = json.loads(
            (
                handoff.audit_root
                / "semantic_global_authority"
                / "unknown-resolution-ordinal-0166.json"
            ).read_text(encoding="utf-8")
        )
        unknown_166_digest = dict(unknown_166["digest"])
        unknown_166_status_fingerprint = unknown_166_digest.pop(
            "failed_closed_status_fingerprint"
        )
        latest_handoff.validate_unknown_resolution_digest(
            digest_binding=unknown_166_digest,
            failed_closed_status_fingerprint=unknown_166_status_fingerprint,
            resolution_id=unknown_166["resolution_id"],
        )
        historical_replay_runner = FakeRunner()
        latest_handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=historical_replay_runner,
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=None,
        )
        self.assertEqual(historical_replay_runner.calls, [])
        self.assertEqual(
            latest_handoff.install_batch_governance_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    active_window, reviewed_git_head=new_head
                ),
                reviewed_git_head=new_head,
                authority_ref=authority_ref,
            ),
            continuation,
        )

        gate_c_head = "c" * 40
        gate_c_authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/146"
            "#issuecomment-5356000000"
        )
        gate_c_runner = FakeRunner()
        gate_c_continuation = latest_handoff.install_gate_c_continuation(
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=gate_c_runner,
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            window_binding=replace(
                active_window, reviewed_git_head=new_head
            ),
            reviewed_git_head=gate_c_head,
            authority_ref=gate_c_authority_ref,
        )
        self.assertEqual(gate_c_runner.calls, [])
        self.assertEqual(gate_c_continuation["activation_total"], 220)
        self.assertEqual(gate_c_continuation["next_global_ordinal"], 221)
        self.assertEqual(
            latest_handoff.install_gate_c_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    active_window, reviewed_git_head=gate_c_head
                ),
                reviewed_git_head=gate_c_head,
                authority_ref=gate_c_authority_ref,
            ),
            gate_c_continuation,
        )

        next_root = root / "ordinal-0221"
        next_representation, next_service = self.build_service(
            root=next_root,
            source_id="src_" + "f" * 32,
        )
        next_handoff = ExternalAgentSemanticHandoffService(
            next_service,
            JsonlAtomicInformationStore(next_root / "atomic.jsonl"),
            handoff.audit_root,
        )
        old_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            next_handoff.execute(
                next_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=old_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=replace(
                    active_window, reviewed_git_head=new_head
                ),
            )
        self.assertEqual(old_runner.calls, [])
        runner_221 = FakeRunner()
        next_handoff.execute(
            next_representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner_221,
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(
                active_window, reviewed_git_head=gate_c_head
            ),
        )
        self.assertEqual(len(runner_221.calls), 1)
        attempt_221 = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in handoff.audit_root.glob(
                "semantic_run_*/attempts/*.json"
            )
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 221
        )
        self.assertEqual(
            attempt_221["global_authority_fingerprint"],
            gate_c_continuation["continuation_fingerprint"],
        )

        latest_handoff = next_handoff
        gate_c_window = replace(active_window, reviewed_git_head=gate_c_head)
        for ordinal in range(222, 298):
            item_root = root / f"ordinal-{ordinal:04d}"
            ordinal_representation, ordinal_service = self.build_service(
                root=item_root,
                source_id=f"src_{ordinal:032x}",
            )
            latest_handoff = ExternalAgentSemanticHandoffService(
                ordinal_service,
                JsonlAtomicInformationStore(item_root / "atomic.jsonl"),
                handoff.audit_root,
            )
            ordinal_runner = FakeRunner()
            latest_handoff.execute(
                ordinal_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=ordinal_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=gate_c_window,
            )
            self.assertEqual(len(ordinal_runner.calls), 1)

        segmented_head = "67d159411e968c6b0c2f787f9063a22682c10fb9"
        segmented_authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/148"
            "#issuecomment-5363000000"
        )
        segmented_runner = FakeRunner()
        segmented_continuation = (
            latest_handoff.install_segmented_gate_c_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=segmented_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=gate_c_window,
                reviewed_git_head=segmented_head,
                authority_ref=segmented_authority_ref,
            )
        )
        self.assertEqual(segmented_runner.calls, [])
        self.assertEqual(segmented_continuation["activation_total"], 297)
        self.assertEqual(segmented_continuation["next_global_ordinal"], 298)

        old_head_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            _SemanticGlobalAuthority(handoff.audit_root)._load_grant(
                gate_c_window,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=old_head_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
            )
        self.assertEqual(old_head_runner.calls, [])

        ordinal_298_root = root / "ordinal-0298"
        representation_298, service_298 = self.build_service(
            root=ordinal_298_root,
            source_id=f"src_{298:032x}",
        )
        handoff_298 = ExternalAgentSemanticHandoffService(
            service_298,
            JsonlAtomicInformationStore(ordinal_298_root / "atomic.jsonl"),
            handoff.audit_root,
        )
        runner_298 = FakeRunner()
        handoff_298.execute(
            representation_298.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner_298,
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(
                gate_c_window, reviewed_git_head=segmented_head
            ),
        )
        self.assertEqual(len(runner_298.calls), 1)
        attempt_298 = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in handoff.audit_root.glob("semantic_run_*/attempts/*.json")
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 298
        )
        self.assertEqual(
            attempt_298["global_authority_fingerprint"],
            segmented_continuation["continuation_fingerprint"],
        )

        startup_head = "c8ece3782ae3ba289d06c36d1e352ce23e0f627b"
        startup_authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-5367000000"
        )
        startup_runner = FakeRunner()
        startup_continuation = (
            handoff_298.install_governance_startup_recovery_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=startup_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    gate_c_window, reviewed_git_head=segmented_head
                ),
                reviewed_git_head=startup_head,
                authority_ref=startup_authority_ref,
                authority_manifest_fingerprint="sha256:" + "a" * 64,
                authority_manifest_raw_fingerprint="sha256:" + "b" * 64,
            )
        )
        self.assertEqual(startup_runner.calls, [])
        self.assertEqual(startup_continuation["activation_total"], 298)
        self.assertEqual(startup_continuation["activation_unknown_count"], 0)
        self.assertEqual(startup_continuation["next_global_ordinal"], 299)
        self.assertEqual(
            startup_continuation["previous_execution_contract"],
            startup_continuation["execution_contract"],
        )
        inspect_runner = FakeRunner()
        self.assertEqual(
            handoff_298.governance_startup_recovery_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=inspect_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                reviewed_git_head=startup_head,
                authority_ref=startup_authority_ref,
                authority_manifest_fingerprint="sha256:" + "a" * 64,
                authority_manifest_raw_fingerprint="sha256:" + "b" * 64,
            ),
            startup_continuation,
        )
        self.assertEqual(inspect_runner.calls, [])
        self.assertEqual(
            handoff_298.install_governance_startup_recovery_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    gate_c_window, reviewed_git_head=startup_head
                ),
                reviewed_git_head=startup_head,
                authority_ref=startup_authority_ref,
                authority_manifest_fingerprint="sha256:" + "a" * 64,
                authority_manifest_raw_fingerprint="sha256:" + "b" * 64,
            ),
            startup_continuation,
        )
        timeout_212 = json.loads(
            (
                handoff.audit_root
                / "semantic_global_authority"
                / "unknown-resolution-ordinal-0212.json"
            ).read_text(encoding="utf-8")
        )
        handoff_298.validate_timeout_212_resolution_digest(
            digest_binding=digest,
            failed_closed_status_fingerprint="sha256:" + "b" * 64,
            resolution_id=timeout_212["resolution_id"],
        )
        handoff_298.validate_unknown_resolution_digest(
            digest_binding=unknown_166_digest,
            failed_closed_status_fingerprint=unknown_166_status_fingerprint,
            resolution_id=unknown_166["resolution_id"],
        )
        failed_closed_head = "ce49d89355caab38da08b4522f416d248c60646b"
        failed_closed_authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
            "#issuecomment-5369000000"
        )
        failed_closed_runner = FakeRunner()
        failed_closed_continuation = (
            handoff_298.install_failed_closed_recovery_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=failed_closed_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    gate_c_window, reviewed_git_head=startup_head
                ),
                reviewed_git_head=failed_closed_head,
                authority_ref=failed_closed_authority_ref,
                authority_manifest_fingerprint="sha256:" + "c" * 64,
                authority_manifest_raw_fingerprint="sha256:" + "d" * 64,
            )
        )
        self.assertEqual(failed_closed_runner.calls, [])
        self.assertEqual(failed_closed_continuation["activation_total"], 298)
        self.assertEqual(
            failed_closed_continuation["activation_unknown_count"], 0
        )
        self.assertEqual(failed_closed_continuation["next_global_ordinal"], 299)
        self.assertEqual(
            handoff_298.install_failed_closed_recovery_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    gate_c_window, reviewed_git_head=failed_closed_head
                ),
                reviewed_git_head=failed_closed_head,
                authority_ref=failed_closed_authority_ref,
                authority_manifest_fingerprint="sha256:" + "c" * 64,
                authority_manifest_raw_fingerprint="sha256:" + "d" * 64,
            ),
            failed_closed_continuation,
        )
        handoff_298.validate_timeout_212_resolution_digest(
            digest_binding=digest,
            failed_closed_status_fingerprint="sha256:" + "b" * 64,
            resolution_id=timeout_212["resolution_id"],
        )
        handoff_298.validate_unknown_resolution_digest(
            digest_binding=unknown_166_digest,
            failed_closed_status_fingerprint=unknown_166_status_fingerprint,
            resolution_id=unknown_166["resolution_id"],
        )
        failed_closed_window = replace(
            gate_c_window, reviewed_git_head=failed_closed_head
        )
        latest_multi_handoff = handoff_298
        for ordinal in range(299, 303):
            item_root = root / f"ordinal-{ordinal:04d}"
            ordinal_representation, ordinal_service = self.build_service(
                root=item_root,
                source_id=f"src_{ordinal:032x}",
            )
            latest_multi_handoff = ExternalAgentSemanticHandoffService(
                ordinal_service,
                JsonlAtomicInformationStore(item_root / "atomic.jsonl"),
                handoff.audit_root,
            )
            ordinal_runner = FakeRunner()
            latest_multi_handoff.execute(
                ordinal_representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=ordinal_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=failed_closed_window,
            )
            self.assertEqual(len(ordinal_runner.calls), 1)

        multi_head = "e" * 40
        multi_authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-5370000000"
        )
        multi_runner = FakeRunner()
        multi_continuation = (
            latest_multi_handoff.install_multi_governance_startup_recovery_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=multi_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=failed_closed_window,
                reviewed_git_head=multi_head,
                authority_ref=multi_authority_ref,
                authority_manifest_fingerprint="sha256:" + "e" * 64,
                authority_manifest_raw_fingerprint="sha256:" + "f" * 64,
            )
        )
        self.assertEqual(multi_runner.calls, [])
        self.assertEqual(multi_continuation["activation_total"], 302)
        self.assertEqual(multi_continuation["next_global_ordinal"], 303)
        self.assertEqual(
            latest_multi_handoff.multi_governance_startup_recovery_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                reviewed_git_head=multi_head,
                authority_ref=multi_authority_ref,
                authority_manifest_fingerprint="sha256:" + "e" * 64,
                authority_manifest_raw_fingerprint="sha256:" + "f" * 64,
            ),
            multi_continuation,
        )

        generic_head = "f" * 40
        generic_authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/176"
            "#issuecomment-5402000000"
        )
        active_run_binding = {
            "run_id": failed_closed_window.window_run_id,
            "plan_fingerprint": (
                failed_closed_window.window_plan_fingerprint
            ),
            "capture_receipt_fingerprint": "sha256:" + "2" * 64,
            "status_fingerprint": "sha256:" + "3" * 64,
        }
        generic_runner = FakeRunner()
        generic_continuation = (
            latest_multi_handoff.install_reviewed_head_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=generic_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    failed_closed_window, reviewed_git_head=multi_head
                ),
                reviewed_git_head=generic_head,
                authority_ref=generic_authority_ref,
                active_run_binding=active_run_binding,
            )
        )
        self.assertEqual(generic_runner.calls, [])
        self.assertEqual(generic_continuation["activation_total"], 302)
        self.assertEqual(generic_continuation["activation_unknown_count"], 0)
        self.assertEqual(generic_continuation["next_global_ordinal"], 303)
        self.assertEqual(
            generic_continuation["previous_global_authority_fingerprint"],
            multi_continuation["continuation_fingerprint"],
        )
        self.assertEqual(
            generic_continuation["previous_continuation_fingerprint"],
            multi_continuation["continuation_fingerprint"],
        )
        self.assertEqual(
            latest_multi_handoff.install_reviewed_head_continuation(
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=FakeRunner(),
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                window_binding=replace(
                    failed_closed_window, reviewed_git_head=generic_head
                ),
                reviewed_git_head=generic_head,
                authority_ref=generic_authority_ref,
                active_run_binding=active_run_binding,
            ),
            generic_continuation,
        )

        ordinal_303_root = root / "ordinal-0303"
        representation_303, service_303 = self.build_service(
            root=ordinal_303_root,
            source_id=f"src_{303:032x}",
        )
        handoff_303 = ExternalAgentSemanticHandoffService(
            service_303,
            JsonlAtomicInformationStore(ordinal_303_root / "atomic.jsonl"),
            handoff.audit_root,
        )
        old_303_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            handoff_303.execute(
                representation_303.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=old_303_runner,
                    diagnostic_root=timeout_provider.diagnostic_root,
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=failed_closed_window,
            )
        self.assertEqual(old_303_runner.calls, [])
        runner_303 = FakeRunner()
        handoff_303.execute(
            representation_303.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner_303,
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(
                failed_closed_window, reviewed_git_head=generic_head
            ),
        )
        self.assertEqual(len(runner_303.calls), 1)
        attempt_303 = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in handoff.audit_root.glob("semantic_run_*/attempts/*.json")
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 303
        )
        self.assertEqual(
            attempt_303["global_authority_fingerprint"],
            generic_continuation["continuation_fingerprint"],
        )

        second_head = "1" * 40
        second_authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/176"
            "#issuecomment-5402000001"
        )
        second_active_run_binding = {
            **active_run_binding,
            "status_fingerprint": "sha256:" + "4" * 64,
        }
        second_continuation = handoff_303.install_reviewed_head_continuation(
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=FakeRunner(),
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            window_binding=replace(
                failed_closed_window, reviewed_git_head=generic_head
            ),
            reviewed_git_head=second_head,
            authority_ref=second_authority_ref,
            active_run_binding=second_active_run_binding,
        )
        self.assertEqual(second_continuation["activation_total"], 303)
        self.assertEqual(second_continuation["next_global_ordinal"], 304)
        self.assertEqual(
            second_continuation["previous_continuation_fingerprint"],
            generic_continuation["continuation_fingerprint"],
        )
        ordinal_304_root = root / "ordinal-0304"
        representation_304, service_304 = self.build_service(
            root=ordinal_304_root,
            source_id=f"src_{304:032x}",
        )
        handoff_304 = ExternalAgentSemanticHandoffService(
            service_304,
            JsonlAtomicInformationStore(ordinal_304_root / "atomic.jsonl"),
            handoff.audit_root,
        )
        runner_304 = FakeRunner()
        handoff_304.execute(
            representation_304.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner_304,
                diagnostic_root=timeout_provider.diagnostic_root,
            ),
            privacy_binding=self.privacy_binding(),
            authority_binding=replace(
                failed_closed_window, reviewed_git_head=second_head
            ),
        )
        self.assertEqual(len(runner_304.calls), 1)
        attempt_304 = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in handoff.audit_root.glob("semantic_run_*/attempts/*.json")
            if json.loads(path.read_text(encoding="utf-8")).get(
                "global_ordinal"
            )
            == 304
        )
        self.assertEqual(
            attempt_304["global_authority_fingerprint"],
            second_continuation["continuation_fingerprint"],
        )
        failed_closed_path = (
            handoff.audit_root
            / "semantic_global_authority"
            / "failed-closed-recovery-continuation.json"
        )
        failed_closed_bytes = failed_closed_path.read_bytes()
        damaged_authority = json.loads(failed_closed_bytes)
        damaged_authority["authority_manifest_fingerprint"] = (
            "sha256:" + "0" * 64
        )
        failed_closed_path.write_text(
            json.dumps(damaged_authority), encoding="utf-8"
        )
        with self.assertRaises(SemanticHandoffError):
            handoff_298.validate_unknown_resolution_digest(
                digest_binding=unknown_166_digest,
                failed_closed_status_fingerprint=(
                    unknown_166_status_fingerprint
                ),
                resolution_id=unknown_166["resolution_id"],
            )
        failed_closed_path.write_bytes(failed_closed_bytes)
        unknown_path = (
            handoff.audit_root
            / "semantic_global_authority"
            / "unknown-resolution-ordinal-0166.json"
        )
        unknown_bytes = unknown_path.read_bytes()
        damaged_unknown = json.loads(unknown_bytes)
        damaged_unknown["digest"]["failed_closed_status_fingerprint"] = (
            "sha256:" + "0" * 64
        )
        unknown_path.write_text(json.dumps(damaged_unknown), encoding="utf-8")
        with self.assertRaises(SemanticHandoffError):
            handoff_298.validate_unknown_resolution_digest(
                digest_binding=unknown_166_digest,
                failed_closed_status_fingerprint=(
                    unknown_166_status_fingerprint
                ),
                resolution_id=unknown_166["resolution_id"],
            )
        unknown_path.write_bytes(unknown_bytes)

    def test_result_only_concurrency_overlaps_and_serial_replay_is_zero_call(
        self,
    ) -> None:
        root = self.root / "result-only-concurrency"
        root.mkdir(parents=True)
        source_ids = iter(("src_" + "1" * 32, "src_" + "2" * 32))
        sources = LocalManagedSourceRepository(
            root / "managed",
            id_factory=lambda: next(source_ids),
            clock=lambda: "2026-08-20T00:00:00.000Z",
        )
        representations = LocalRepresentationRepository(root / "representations")
        representation_service = RepresentationService(sources, representations)
        built = []
        for index in (1, 2):
            external = root / f"synthetic-{index}.txt"
            external.write_text(f"synthetic-{index}", encoding="utf-8")
            source = sources.admit(
                external, metadata={"media_type": "application/synthetic"}
            ).source
            built.append(
                representation_service.build(
                    source.source_id, JsonAdapter(blocks=2)
                ).representation
            )
        information_service = RepresentationInformationService(
            sources,
            representations,
            root / "information",
            batch_size=1,
            clock=lambda: "2026-08-20T00:00:00.000Z",
        )
        handoff = ExternalAgentSemanticHandoffService(
            information_service,
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            root / "audits",
        )

        barrier = threading.Barrier(2)
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0

        class BlockingRunner(FakeRunner):
            def __call__(self, command, **kwargs):
                inner = super().__call__(command, **kwargs)

                class BlockingProcess:
                    pid = inner.pid

                    @property
                    def returncode(self):
                        return inner.returncode

                    def communicate(self, **communicate_kwargs):
                        nonlocal active, maximum_active
                        with state_lock:
                            active += 1
                            maximum_active = max(maximum_active, active)
                        try:
                            barrier.wait(timeout=5)
                            time.sleep(0.15)
                            return inner.communicate(**communicate_kwargs)
                        finally:
                            with state_lock:
                                active -= 1

                return BlockingProcess()

        runners = (BlockingRunner(), BlockingRunner())
        providers = tuple(
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner,
                diagnostic_root=root / f"diagnostics-{index}",
            )
            for index, runner in enumerate(runners)
        )
        binding = self.semantic_window_binding(batch_size=1)
        self.install_authority(handoff, providers[0], binding)
        requests = tuple(
            SemanticResultOnlyRequest(
                representation.representation_id,
                self.privacy_binding(),
                binding,
            )
            for representation in built
        )

        with patch.object(
            _SemanticRecoveryRun,
            "publish_started",
            side_effect=OSError("synthetic reserved interruption"),
        ), self.assertRaises(SemanticHandoffError):
            handoff.prepare_results(requests, providers, concurrency=2)
        self.assertEqual([len(runner.calls) for runner in runners], [0, 0])

        wall_started = time.monotonic()
        elapsed = handoff.prepare_results(requests, providers, concurrency=2)
        wall_ms = (time.monotonic() - wall_started) * 1000

        self.assertEqual(maximum_active, 2)
        self.assertEqual([len(runner.calls) for runner in runners], [1, 1])
        self.assertEqual(set(elapsed), {item.representation_id for item in built})
        self.assertLessEqual(wall_ms, sum(elapsed.values()) * 0.70)
        self.assertFalse((root / "information").exists())
        self.assertFalse((root / "atomic.jsonl").exists())
        attempts = sorted(
            (
                json.loads(path.read_text(encoding="utf-8"))
                for path in (root / "audits").glob(
                    "semantic_run_*/attempts/*.json"
                )
            ),
            key=lambda item: item["global_ordinal"],
        )
        self.assertEqual(
            [item["global_ordinal"] for item in attempts], [81, 82]
        )
        self.assertTrue(
            all(
                item["schema_version"]
                == "semantic-handoff-attempt-receipt/4.0"
                and item["state"] == "reserved_not_started"
                for item in attempts
            )
        )

        out_of_order_runner = FakeRunner()
        with self.assertRaises(SemanticHandoffError):
            handoff.execute(
                built[1].representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=out_of_order_runner,
                    diagnostic_root=root / "out-of-order-diagnostics",
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=binding,
            )
        self.assertEqual(out_of_order_runner.calls, [])
        self.assertFalse((root / "atomic.jsonl").exists())

        replay_runners = (FakeRunner(), FakeRunner())
        for expected_ordinal, representation, runner in zip(
            (81, 82), built, replay_runners, strict=True
        ):
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0",
                    timeout_seconds=300,
                    runner=runner,
                    diagnostic_root=root / "replay-diagnostics",
                ),
                privacy_binding=self.privacy_binding(),
                authority_binding=binding,
            )
            self.assertEqual(runner.calls, [])
            commit_cursor = json.loads(
                (
                    root
                    / "audits"
                    / "semantic_global_authority"
                    / "commit-cursor.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                commit_cursor["committed_global_ordinal"], expected_ordinal
            )
        self.assertEqual(
            len(
                JsonlAtomicInformationStore(root / "atomic.jsonl")
                .list_atomic_information()
            ),
            2,
        )

    def test_result_only_parallelism_four_meets_controlled_delay_ratio(self) -> None:
        root = self.root / "result-only-concurrency-four"
        root.mkdir(parents=True)
        source_ids = iter(f"src_{index:032x}" for index in range(1, 5))
        sources = LocalManagedSourceRepository(
            root / "managed",
            id_factory=lambda: next(source_ids),
            clock=lambda: "2026-08-25T00:00:00.000Z",
        )
        representations = LocalRepresentationRepository(root / "representations")
        builder = RepresentationService(sources, representations)
        built = []
        for index in range(4):
            external = root / f"synthetic-{index}.txt"
            external.write_text(f"synthetic-{index}", encoding="utf-8")
            source = sources.admit(
                external, metadata={"media_type": "application/synthetic"}
            ).source
            built.append(
                builder.build(source.source_id, JsonAdapter(blocks=1)).representation
            )
        handoff = ExternalAgentSemanticHandoffService(
            RepresentationInformationService(
                sources,
                representations,
                root / "information",
                batch_size=1,
                clock=lambda: "2026-08-25T00:00:00.000Z",
            ),
            JsonlAtomicInformationStore(root / "atomic.jsonl"),
            root / "audits",
        )
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = 0
        peak = 0

        class FourLaneRunner(FakeRunner):
            def __call__(self, command, **kwargs):
                inner = super().__call__(command, **kwargs)

                class Process:
                    pid = inner.pid

                    @property
                    def returncode(self):
                        return inner.returncode

                    def communicate(self, **communicate_kwargs):
                        nonlocal active, peak
                        with lock:
                            active += 1
                            peak = max(peak, active)
                        try:
                            barrier.wait(timeout=5)
                            time.sleep(0.15)
                            return inner.communicate(**communicate_kwargs)
                        finally:
                            with lock:
                                active -= 1

                return Process()

        runners = tuple(FourLaneRunner() for _ in range(4))
        providers = tuple(
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0",
                timeout_seconds=300,
                runner=runner,
                diagnostic_root=root / f"diagnostics-{index}",
            )
            for index, runner in enumerate(runners)
        )
        binding = self.semantic_window_binding(batch_size=1)
        self.install_authority(handoff, providers[0], binding)
        requests = tuple(
            SemanticResultOnlyRequest(
                representation.representation_id,
                self.privacy_binding(),
                binding,
            )
            for representation in built
        )

        wall_started = time.monotonic()
        elapsed = handoff.prepare_results(requests, providers, concurrency=4)
        wall_ms = (time.monotonic() - wall_started) * 1000

        self.assertEqual(peak, 4)
        self.assertEqual([len(runner.calls) for runner in runners], [1, 1, 1, 1])
        self.assertLessEqual(wall_ms, sum(elapsed.values()) * 0.45)

if __name__ == "__main__":
    unittest.main()
