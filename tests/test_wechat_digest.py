from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from archeos import wechat_capture_helper
from archeos.atomic_information import (
    ClaimAttribution,
    JsonlAtomicInformationStore,
    ingest_processing_package,
)
from archeos.digestion import (
    BusinessLanguageHumanJudgmentPort,
    CodexAtomicInformationInterpretationProvider,
    InterpretationResult,
    WorldModelOperation,
)
from archeos.digestion.providers import CodexInterpretationTimeout
from archeos.emergence import IdentityEvidence, IdentityGateService
from archeos.representation import LocalRepresentationRepository, RepresentationError
from archeos.representation_information import (
    EXTERNAL_AGENT_PROTOCOL_V1,
    EXTERNAL_AGENT_PROTOCOL_V2,
    EXTERNAL_AGENT_PROTOCOL_V3,
    EXTERNAL_AGENT_PROTOCOL_V3_1,
    EXTERNAL_AGENT_PROTOCOL_V3_2,
    EXTERNAL_AGENT_PROTOCOL_V3_3,
    EXTERNAL_AGENT_PROTOCOL_V3_4,
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisResult,
    RepresentationCandidateDraft,
    RepresentationInformationError,
    RepresentationInformationService,
    RepresentationResidueDraft,
    _analysis_batches_for_anchor_unit_ids,
    _external_agent_request,
    _units_from_representation,
)
from archeos.semantic_handoff import (
    SemanticHandoffError,
    _fingerprint,
    _package_fingerprint,
    _SemanticGlobalAuthority,
)
from archeos.source import LocalManagedSourceRepository
from archeos.wechat_capture_helper import (
    _all_cursor_rows,
    _window_upper,
)
from archeos.wechat_capture_helper import (
    _capture as capture_with_wechat_cli,
)
from archeos.wechat_capture_helper import (
    _digest as capture_digest,
)
from archeos.wechat_digest import (
    TERMINAL_ITEM_STATES,
    ZERO_CURSOR,
    CapturedAttachment,
    CapturedMessage,
    DeterministicPrivacyGate,
    ExistingSemanticHandoff,
    WechatCapture,
    WechatCliCaptureProvider,
    WechatCursor,
    WechatDigestError,
    WechatDigestRunStore,
    WechatDigestService,
    _build_plan,
    _canonical_json,
    _conversation_source_payload,
    _governance_atomic_fingerprint,
    _plan_fingerprint,
    _sha256_bytes,
)
from archeos.world_model import SQLiteWorldModelRepository


def _hash(path: Path) -> tuple[str, int]:
    content = path.read_bytes()
    return "sha256:" + hashlib.sha256(content).hexdigest(), len(content)


def attachment(
    path: Path | None,
    key: str,
    *,
    status: str = "available",
    media_type: str = "text/plain",
    filename: str = "synthetic.txt",
) -> CapturedAttachment:
    content_hash, size = _hash(path) if path is not None else (None, None)
    return CapturedAttachment(
        key,
        status,
        filename,
        media_type,
        path,
        content_hash,
        size,
    )


def message(
    number: int,
    *,
    timestamp: int | None = None,
    conversation: str = "conversation_a",
    content: str | None = None,
    attachments: tuple[CapturedAttachment, ...] = (),
) -> CapturedMessage:
    timestamp = 1_700_000_000 + number if timestamp is None else timestamp
    message_key = f"wechat_message_{number:032x}"
    conversation_key = "wechat_conversation_" + hashlib.sha256(
        conversation.encode()
    ).hexdigest()[:32]
    return CapturedMessage(
        conversation_key=conversation_key,
        provider_conversation_id=f"wxid_{conversation}",
        conversation_label=f"Synthetic {conversation}",
        is_group=False,
        message_key=message_key,
        cursor=WechatCursor(timestamp, conversation_key, message_key),
        sender_label="Synthetic Sender",
        message_type="file" if attachments else "text",
        timestamp=timestamp,
        sent_at="2023-11-14T22:13:20+00:00",
        visible_content=content or "Synthetic Project has a new verified update.",
        structured_payload=content or "Synthetic Project has a new verified update.",
        attachments=attachments,
    )


class SyntheticCaptureProvider:
    provider_version = "synthetic-1"

    def __init__(
        self,
        messages: list[CapturedMessage],
        *,
        window_seconds: int | None = None,
    ) -> None:
        self.messages = messages
        self.window_seconds = window_seconds
        self.calls: list[
            tuple[
                WechatCursor,
                WechatCursor | None,
                bool,
                WechatCursor | None,
            ]
        ] = []
        self.outputs: list[WechatCapture] = []

    def capture(
        self,
        after_cursor: WechatCursor,
        *,
        upper_bound: WechatCursor | None = None,
        all_history_upper_bound: WechatCursor | None = None,
        observe_only: bool = False,
    ) -> WechatCapture:
        if upper_bound is not None and all_history_upper_bound is not None:
            raise AssertionError("synthetic capture boundaries conflict")
        self.calls.append(
            (after_cursor, upper_bound, observe_only, all_history_upper_bound)
        )
        ordered = tuple(sorted(self.messages, key=lambda item: item.cursor))
        remaining = tuple(
            item
            for item in ordered
            if item.cursor > after_cursor
            and (
                all_history_upper_bound is None
                or item.cursor <= all_history_upper_bound
            )
        )
        if observe_only and upper_bound is None:
            observed_upper = remaining[-1].cursor if remaining else after_cursor
        elif upper_bound is not None:
            observed_upper = upper_bound
        elif remaining and self.window_seconds is not None:
            cutoff = remaining[0].timestamp + self.window_seconds
            observed_upper = tuple(
                item for item in remaining if item.timestamp < cutoff
            )[-1].cursor
        elif remaining:
            observed_upper = remaining[-1].cursor
        else:
            observed_upper = after_cursor
        selected = tuple(
            item
            for item in ordered
            if after_cursor < item.cursor <= observed_upper
        )
        if observe_only:
            selected = ()
        capture = WechatCapture(
            self.provider_version, after_cursor, observed_upper, selected
        )
        self.outputs.append(capture)
        return capture


class SuccessfulV34Process:
    def __init__(self, command: list[str], calls: list[list[str]]) -> None:
        self.command = command
        self.pid = 99_999_999
        self.returncode: int | None = None
        calls.append(command)

    def communicate(
        self,
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        del timeout
        assert input is not None
        request = json.loads(input.split("Request:\n", 1)[1])
        result = {
            "protocol_version": request["protocol_version"],
            "input_fingerprint": request["input_fingerprint"],
            "anchor_results": {
                unit["unit_id"]: {
                    "classification": "candidate",
                    "records": [
                        {
                            "statement": "Synthetic committed wave statement.",
                            "semantic_type": "observation",
                            "concerns": ["Synthetic"],
                            "supporting_evidence_unit_ids": [],
                            "context": "Synthetic complete conversation context.",
                            "confidence": 0.9,
                        }
                    ],
                }
                for unit in request["anchor_units"]
            },
        }
        result_path = Path(
            self.command[self.command.index("--output-last-message") + 1]
        )
        result_path.write_text(json.dumps(result), encoding="utf-8")
        self.returncode = 0
        return "", ""


class SuccessfulV34Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        return SuccessfulV34Process(list(command), self.calls)


class RunnerBackedExistingSemanticHandoff(ExistingSemanticHandoff):
    def __init__(self, *, workspace: Path, runner: SuccessfulV34Runner) -> None:
        self._test_runner = runner
        super().__init__(
            source_repository=LocalManagedSourceRepository(
                workspace / "01_inbox"
            ),
            representation_repository=LocalRepresentationRepository(
                workspace / "02_processing" / "representations"
            ),
            information_root=workspace / "02_processing" / "information",
            information_store=JsonlAtomicInformationStore(
                workspace / "03_information" / "atomic_information.jsonl"
            ),
            audit_root=(
                workspace / "02_processing" / "semantic_handoff_runs"
            ),
            codex_binary="codex",
            provider_version="0.147.0",
            timeout_seconds=300,
            batch_size=50,
            reviewed_git_head="6" * 40,
        )

    def _new_provider(self, lane: str) -> CodexCliRepresentationAnalysisProvider:
        lane_fingerprint = hashlib.sha256(lane.encode()).hexdigest()[:16]
        return CodexCliRepresentationAnalysisProvider(
            **self._provider_config,
            runner=self._test_runner,
            diagnostic_root=self._diagnostic_root / lane_fingerprint,
        )


class FailOnSecondCaptureProvider(SyntheticCaptureProvider):
    def capture(self, *args, **kwargs):
        if self.calls:
            raise AssertionError("resolve must not capture after manifest build")
        return super().capture(*args, **kwargs)


class FailOnSecondFullCaptureProvider(SyntheticCaptureProvider):
    def capture(self, *args, **kwargs):
        full_calls = sum(not call[2] for call in self.calls)
        if not kwargs.get("observe_only", False) and full_calls:
            raise AssertionError("frozen window must not full capture twice")
        return super().capture(*args, **kwargs)


class SyntheticAnalysisProvider:
    name = "external-agent-codex-cli"

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "all_candidate"
        self.provider_version = "0.147.0"
        self.model = "gpt-5.6-terra"
        self.reasoning_effort = "medium"
        self.fallback_policy = "none"

    def analyze(self, batch):
        self.calls += 1
        candidate_units = tuple(batch.anchor_units)
        residue_units = ()
        if self.mode == "all_residue":
            candidate_units = ()
            residue_units = tuple(batch.anchor_units)
        elif self.mode == "mixed":
            candidate_units = tuple(batch.anchor_units[::2])
            residue_units = tuple(batch.anchor_units[1::2])
        elif self.mode == "four_one":
            candidate_units = tuple(batch.anchor_units[:4])
            residue_units = tuple(batch.anchor_units[4:])
        return RepresentationAnalysisResult(
            candidates=tuple(
                RepresentationCandidateDraft(
                    statement=str(unit.content),
                    semantic_type="observation",
                    concerns=((
                        "Ambiguous Project"
                        if "ambiguous" in str(unit.content).lower()
                        else "Synthetic Project"
                    ),),
                    evidence_unit_ids=(unit.unit_id,),
                    context="Synthetic bounded context.",
                    confidence=1.0,
                )
                for unit in candidate_units
            ),
            residue=tuple(
                RepresentationResidueDraft(
                    evidence_unit_ids=(unit.unit_id,),
                    reason_not_absorbed="Synthetic unresolved detail.",
                    future_value_or_uncertainty="Preserve for later evidence.",
                )
                for unit in residue_units
            ),
        )


class _SyntheticSemanticHandoffBase:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.provider = SyntheticAnalysisProvider()
        self.failures_remaining = 0
        self.protocol_version = EXTERNAL_AGENT_PROTOCOL_V1
        self.profiled_v1 = False
        self.reviewed_git_head = "6" * 40
        self.installed_grant = None
        self.installed_extension = None
        self.installed_maintenance_continuation = None
        self.installed_batch_governance_continuation = None
        self.installed_gate_c_continuation = None
        self.installed_segmented_gate_c_continuation = None
        self.installed_governance_startup_recovery_continuation = None
        self.installed_failed_closed_recovery_continuation = None
        self.installed_multi_governance_startup_recovery_continuation = None
        self.installed_reviewed_head_continuations = []
        self.unknown_resolution = None
        self.timeout_212_resolution = None
        self.attempt_resolution = None
        self.attempt_resolution_candidate = None
        self.before_unknown_commit = None
        self.campaign_binding = None
        self.authority_bindings = []
        self.global_attempt_total = 176
        self.global_unknown = 0
        self.absolute_cap = 1000
        self.latest_representation_id = None
        self.prepared_waves = []
        self.pre_attempt_inventories = {}
        self.recovery_inspections = None

    def prepare_results(self, requests, *, parallelism):
        self.prepared_waves.append(
            (tuple(request.representation_id for request in requests), parallelism)
        )
        return {request.representation_id: 0 for request in requests}

    def inspect_recovery_wave(self, requests):
        if self.recovery_inspections is not None:
            return tuple(dict(item) for item in self.recovery_inspections)
        return tuple(
            {
                "classification": "pre_provider",
                "representation_id": request.representation_id,
                "phase": None,
                "atomic_information_ids": [],
                "package_fingerprint": None,
                "global_ordinal_range": None,
            }
            for request in requests
        )

    def validate_pre_attempt_inventory(
        self, representation_id, *, privacy_binding
    ):
        del privacy_binding
        inventory = self.pre_attempt_inventories.get(representation_id)
        if inventory is None:
            return {
                "inventory_kind": "absent",
                "semantic_run_id": "semantic_run_" + "0" * 32,
                "run_receipt_fingerprint": None,
                "attempt_count": 0,
                "reserved_count": 0,
                "started_count": 0,
                "result_count": 0,
            }
        return dict(inventory)

    def install_reviewed_head_continuation(
        self,
        *,
        window_binding,
        authority_ref,
        active_run_binding,
    ):
        previous_head = (
            self.campaign_binding.reviewed_git_head
            if self.campaign_binding is not None
            else window_binding.reviewed_git_head
        )
        ordinal = len(self.installed_reviewed_head_continuations) + 1
        expected = {
            "authority_ref": authority_ref,
            "previous_reviewed_git_head": previous_head,
            "reviewed_git_head": self.reviewed_git_head,
            "active_run": dict(active_run_binding),
            "activation_total": self.global_attempt_total,
            "activation_unknown_count": self.global_unknown,
            "activation_last_global_ordinal": self.global_attempt_total,
            "next_global_ordinal": self.global_attempt_total + 1,
            "absolute_cap": self.absolute_cap,
            "continuation_fingerprint": "sha256:" + f"{ordinal:x}" * 64,
        }
        self.installed_reviewed_head_continuations.append(expected)
        if self.campaign_binding is None:
            self.campaign_binding = SimpleNamespace(
                created_at=window_binding.campaign_created_at,
                lower_cursor=window_binding.campaign_lower_cursor,
                frozen_global_upper_cursor=(
                    window_binding.frozen_global_upper_cursor
                ),
                capture_provider_version=window_binding.capture_provider_version,
                semantic_batch_size=window_binding.semantic_batch_size,
                reviewed_git_head=self.reviewed_git_head,
            )
        else:
            self.campaign_binding = SimpleNamespace(
                created_at=self.campaign_binding.created_at,
                lower_cursor=self.campaign_binding.lower_cursor,
                frozen_global_upper_cursor=(
                    self.campaign_binding.frozen_global_upper_cursor
                ),
                capture_provider_version=(
                    self.campaign_binding.capture_provider_version
                ),
                semantic_batch_size=self.campaign_binding.semantic_batch_size,
                reviewed_git_head=self.reviewed_git_head,
            )
        return expected


class SyntheticSemanticHandoff(_SyntheticSemanticHandoffBase):

    def execute(
        self,
        representation_id: str,
        *,
        privacy_binding=None,
        authority_binding=None,
    ):
        self.latest_representation_id = representation_id
        output_root = self.workspace / "02_processing" / "information"
        package = output_root / representation_id
        if privacy_binding is not None:
            assert privacy_binding.route == "approved"
            assert authority_binding is not None or package.exists()
            if authority_binding is not None:
                self.authority_bindings.append(authority_binding)
        store = JsonlAtomicInformationStore(
            self.workspace / "03_information" / "atomic_information.jsonl"
        )
        if package.exists():
            return SimpleNamespace(ingestion=ingest_processing_package(package, store))
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("synthetic semantic failure")
        package = RepresentationInformationService(
            LocalManagedSourceRepository(self.workspace / "01_inbox"),
            LocalRepresentationRepository(
                self.workspace / "02_processing" / "representations"
            ),
            output_root,
        ).extract(representation_id, self.provider)
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            self.protocol_version == EXTERNAL_AGENT_PROTOCOL_V1
            and not self.profiled_v1
        ):
            manifest["provider"] = {"name": "external-agent-codex-cli"}
        else:
            manifest["provider"] = {
                "name": "external-agent-codex-cli",
                "provider_version": "0.147.0",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "fallback_policy": "none",
            }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        ingestion = ingest_processing_package(package, store)
        self._write_success_audits(package, representation_id)
        return SimpleNamespace(ingestion=ingestion)

    def install_global_authority(
        self,
        *,
        inventory_authority_file,
        window_binding,
    ):
        assert isinstance(inventory_authority_file, Path)
        expected = {
            "authority_ref": "sha256:" + "a" * 64,
            "baseline_total": 80,
            "max_new": 20,
            "absolute_cap": 100,
            "global_authority_fingerprint": "sha256:" + "5" * 64,
            "inventory_authority_file": str(inventory_authority_file),
        }
        if self.installed_grant is not None and self.installed_grant != expected:
            raise RuntimeError("synthetic authority drift")
        self.installed_grant = expected
        self.campaign_binding = SimpleNamespace(
            created_at=window_binding.campaign_created_at,
            lower_cursor=window_binding.campaign_lower_cursor,
            frozen_global_upper_cursor=(
                window_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=window_binding.capture_provider_version,
            semantic_batch_size=window_binding.semantic_batch_size,
            reviewed_git_head=window_binding.reviewed_git_head,
        )
        return expected

    def install_global_authority_extension(
        self,
        *,
        window_binding,
    ):
        assert self.installed_grant is not None
        assert self.campaign_binding is not None
        assert window_binding.reviewed_git_head == self.campaign_binding.reviewed_git_head
        expected = {
            "activation_total": 81,
            "activation_unknown_count": 0,
            "previous_absolute_cap": 100,
            "new_absolute_cap": 1000,
            "first_authorized_ordinal": 82,
            "last_authorized_ordinal": 1000,
            "extension_fingerprint": "sha256:" + "e" * 64,
        }
        if self.installed_extension is not None and self.installed_extension != expected:
            raise RuntimeError("synthetic authority extension drift")
        self.installed_extension = expected
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def install_maintenance_continuation(
        self,
        *,
        window_binding,
        authority_ref,
    ):
        assert self.campaign_binding is not None
        assert window_binding.reviewed_git_head in {
            self.campaign_binding.reviewed_git_head,
            self.reviewed_git_head,
        }
        expected = {
            "authority_ref": authority_ref,
            "previous_reviewed_git_head": "6" * 40,
            "reviewed_git_head": self.reviewed_git_head,
            "previous_execution_contract": {"profile": "unchanged"},
            "execution_contract": {"profile": "unchanged"},
            "campaign": {"synthetic": True},
            "window": {"synthetic": True},
            "activation_total": 176,
            "activation_unknown_count": 0,
            "activation_last_global_ordinal": 176,
            "activation_attempt_inventory_fingerprint": "sha256:" + "a" * 64,
            "next_global_ordinal": 177,
            "absolute_cap": 1000,
            "continuation_fingerprint": "sha256:" + "b" * 64,
        }
        if (
            self.installed_maintenance_continuation is not None
            and self.installed_maintenance_continuation != expected
        ):
            raise RuntimeError("synthetic maintenance continuation drift")
        self.installed_maintenance_continuation = expected
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def install_batch_governance_continuation(
        self,
        *,
        window_binding,
        authority_ref,
    ):
        if self.campaign_binding is None:
            self.campaign_binding = SimpleNamespace(
                created_at=window_binding.campaign_created_at,
                lower_cursor=window_binding.campaign_lower_cursor,
                frozen_global_upper_cursor=(
                    window_binding.frozen_global_upper_cursor
                ),
                capture_provider_version=window_binding.capture_provider_version,
                semantic_batch_size=window_binding.semantic_batch_size,
                reviewed_git_head=window_binding.reviewed_git_head,
            )
        expected = {
            "authority_ref": authority_ref,
            "activation_total": 220,
            "activation_unknown_count": 0,
            "activation_last_global_ordinal": 220,
            "next_global_ordinal": 221,
            "absolute_cap": 1000,
            "continuation_fingerprint": "sha256:" + "c" * 64,
        }
        if (
            self.installed_batch_governance_continuation is not None
            and self.installed_batch_governance_continuation != expected
        ):
            raise RuntimeError("synthetic batch governance continuation drift")
        self.installed_batch_governance_continuation = expected
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def install_gate_c_continuation(
        self,
        *,
        window_binding,
        authority_ref,
    ):
        expected = {
            "authority_ref": authority_ref,
            "previous_reviewed_git_head": "b" * 40,
            "reviewed_git_head": self.reviewed_git_head,
            "activation_total": 220,
            "activation_unknown_count": 0,
            "activation_last_global_ordinal": 220,
            "next_global_ordinal": 221,
            "absolute_cap": 1000,
            "continuation_fingerprint": "sha256:" + "d" * 64,
        }
        if (
            self.installed_gate_c_continuation is not None
            and self.installed_gate_c_continuation != expected
        ):
            raise RuntimeError("synthetic Gate C continuation drift")
        self.installed_gate_c_continuation = expected
        assert self.campaign_binding is not None
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def install_segmented_gate_c_continuation(
        self,
        *,
        window_binding,
        authority_ref,
    ):
        expected = {
            "authority_ref": authority_ref,
            "previous_reviewed_git_head": "c" * 40,
            "reviewed_git_head": self.reviewed_git_head,
            "activation_total": 297,
            "activation_unknown_count": 0,
            "activation_last_global_ordinal": 297,
            "next_global_ordinal": 298,
            "absolute_cap": 1000,
            "continuation_fingerprint": "sha256:" + "f" * 64,
        }
        if (
            self.installed_segmented_gate_c_continuation is not None
            and self.installed_segmented_gate_c_continuation != expected
        ):
            raise RuntimeError("synthetic segmented Gate C continuation drift")
        self.installed_segmented_gate_c_continuation = expected
        assert self.campaign_binding is not None
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def install_governance_startup_recovery_continuation(
        self,
        *,
        window_binding,
        authority_ref,
        authority_manifest_fingerprint,
        authority_manifest_raw_fingerprint,
    ):
        expected = {
            "authority_ref": authority_ref,
            "authority_manifest_fingerprint": authority_manifest_fingerprint,
            "authority_manifest_raw_fingerprint": (
                authority_manifest_raw_fingerprint
            ),
            "previous_reviewed_git_head": (
                "67d159411e968c6b0c2f787f9063a22682c10fb9"
            ),
            "reviewed_git_head": self.reviewed_git_head,
            "activation_total": 298,
            "activation_unknown_count": 0,
            "activation_last_global_ordinal": 298,
            "next_global_ordinal": 299,
            "absolute_cap": 1000,
            "continuation_fingerprint": "sha256:" + "7" * 64,
        }
        if (
            self.installed_governance_startup_recovery_continuation
            is not None
            and self.installed_governance_startup_recovery_continuation
            != expected
        ):
            raise RuntimeError("synthetic startup recovery continuation drift")
        self.installed_governance_startup_recovery_continuation = expected
        assert self.campaign_binding is not None
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def governance_startup_recovery_continuation(
        self,
        *,
        authority_ref,
        authority_manifest_fingerprint,
        authority_manifest_raw_fingerprint,
    ):
        observed = self.installed_governance_startup_recovery_continuation
        if observed is None:
            return None
        if (
            observed["authority_ref"] != authority_ref
            or observed["authority_manifest_fingerprint"]
            != authority_manifest_fingerprint
            or observed["authority_manifest_raw_fingerprint"]
            != authority_manifest_raw_fingerprint
            or observed["reviewed_git_head"] != self.reviewed_git_head
            or self.global_attempt_total != 298
            or self.global_unknown != 0
        ):
            raise RuntimeError("synthetic startup recovery inspection drift")
        return dict(observed)

    def install_failed_closed_recovery_continuation(
        self,
        *,
        window_binding,
        authority_ref,
        authority_manifest_fingerprint,
        authority_manifest_raw_fingerprint,
    ):
        expected = {
            "authority_ref": authority_ref,
            "authority_manifest_fingerprint": authority_manifest_fingerprint,
            "authority_manifest_raw_fingerprint": (
                authority_manifest_raw_fingerprint
            ),
            "previous_reviewed_git_head": (
                "c8ece3782ae3ba289d06c36d1e352ce23e0f627b"
            ),
            "reviewed_git_head": self.reviewed_git_head,
            "activation_total": 298,
            "activation_unknown_count": 0,
            "activation_last_global_ordinal": 298,
            "next_global_ordinal": 299,
            "absolute_cap": 1000,
            "continuation_fingerprint": "sha256:" + "8" * 64,
        }
        if (
            self.installed_failed_closed_recovery_continuation is not None
            and self.installed_failed_closed_recovery_continuation != expected
        ):
            raise RuntimeError("synthetic failed-closed continuation drift")
        assert window_binding.reviewed_git_head in {
            "c8ece3782ae3ba289d06c36d1e352ce23e0f627b",
            self.reviewed_git_head,
        }
        self.installed_failed_closed_recovery_continuation = expected
        assert self.campaign_binding is not None
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def failed_closed_recovery_continuation(
        self,
        *,
        authority_ref,
        authority_manifest_fingerprint,
        authority_manifest_raw_fingerprint,
    ):
        observed = self.installed_failed_closed_recovery_continuation
        if observed is None:
            return None
        if (
            observed["authority_ref"] != authority_ref
            or observed["authority_manifest_fingerprint"]
            != authority_manifest_fingerprint
            or observed["authority_manifest_raw_fingerprint"]
            != authority_manifest_raw_fingerprint
            or observed["reviewed_git_head"] != self.reviewed_git_head
            or self.global_attempt_total != 298
            or self.global_unknown != 0
        ):
            raise RuntimeError("synthetic failed-closed inspection drift")
        return dict(observed)

    def install_multi_governance_startup_recovery_continuation(
        self,
        *,
        window_binding,
        authority_ref,
        authority_manifest_fingerprint,
        authority_manifest_raw_fingerprint,
    ):
        expected = {
            "authority_ref": authority_ref,
            "authority_manifest_fingerprint": authority_manifest_fingerprint,
            "authority_manifest_raw_fingerprint": (
                authority_manifest_raw_fingerprint
            ),
            "previous_reviewed_git_head": (
                "ce49d89355caab38da08b4522f416d248c60646b"
            ),
            "reviewed_git_head": self.reviewed_git_head,
            "activation_total": 302,
            "activation_unknown_count": 0,
            "activation_last_global_ordinal": 302,
            "next_global_ordinal": 303,
            "absolute_cap": 1000,
            "continuation_fingerprint": "sha256:" + "9" * 64,
        }
        if (
            self.installed_multi_governance_startup_recovery_continuation
            is not None
            and self.installed_multi_governance_startup_recovery_continuation
            != expected
        ):
            raise RuntimeError("synthetic multi startup continuation drift")
        assert window_binding.reviewed_git_head in {
            "ce49d89355caab38da08b4522f416d248c60646b",
            self.reviewed_git_head,
        }
        self.installed_multi_governance_startup_recovery_continuation = expected
        assert self.campaign_binding is not None
        self.campaign_binding = SimpleNamespace(
            created_at=self.campaign_binding.created_at,
            lower_cursor=self.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=self.campaign_binding.capture_provider_version,
            semantic_batch_size=self.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.reviewed_git_head,
        )
        return expected

    def multi_governance_startup_recovery_continuation(
        self,
        *,
        authority_ref,
        authority_manifest_fingerprint,
        authority_manifest_raw_fingerprint,
    ):
        observed = (
            self.installed_multi_governance_startup_recovery_continuation
        )
        if observed is None:
            return None
        if (
            observed["authority_ref"] != authority_ref
            or observed["authority_manifest_fingerprint"]
            != authority_manifest_fingerprint
            or observed["authority_manifest_raw_fingerprint"]
            != authority_manifest_raw_fingerprint
            or observed["reviewed_git_head"] != self.reviewed_git_head
            or self.global_attempt_total != 302
            or self.global_unknown != 0
        ):
            raise RuntimeError("synthetic multi startup inspection drift")
        return dict(observed)

    def global_campaign_binding(self):
        return self.campaign_binding

    def global_attempt_summary(self, representation_id):
        if representation_id != self.latest_representation_id:
            raise RuntimeError("synthetic latest Representation mismatch")
        return {
            "global_attempt_total": self.global_attempt_total,
            "global_unknown": self.global_unknown,
            "next_global_ordinal": self.global_attempt_total + 1,
            "absolute_cap": self.absolute_cap,
        }

    def resolve_unknown(
        self,
        *,
        authority_manifest_file,
        digest_binding,
        commit_failed_closed_status,
    ):
        manifest = json.loads(Path(authority_manifest_file).read_text("utf-8"))
        assert manifest["digest"]["item_id"] == digest_binding["item_id"]
        resolution_id = "unknown_resolution_" + "a" * 32
        if self.before_unknown_commit is not None:
            self.before_unknown_commit()
        status_fingerprint, final_digest_binding = (
            commit_failed_closed_status(resolution_id)
        )
        assert final_digest_binding == digest_binding
        expected = {
            "global_ordinal": 166,
            "resolution_id": resolution_id,
            "preserved_but_unabsorbed": True,
            "digest": {
                **dict(digest_binding),
                "failed_closed_status_fingerprint": status_fingerprint,
            },
            "continuation": {"next_global_ordinal": 167},
            "resolution_receipt_fingerprint": "sha256:" + "f" * 64,
        }
        if self.unknown_resolution is not None:
            assert self.unknown_resolution == expected
        self.unknown_resolution = expected
        return expected

    def validate_unknown_resolution_digest(
        self,
        *,
        digest_binding,
        failed_closed_status_fingerprint,
        resolution_id,
    ):
        assert self.unknown_resolution is not None
        assert self.unknown_resolution["resolution_id"] == resolution_id
        assert self.unknown_resolution["digest"] == {
            **dict(digest_binding),
            "failed_closed_status_fingerprint": failed_closed_status_fingerprint,
        }
        return self.unknown_resolution

    def resolve_timeout_212(
        self,
        *,
        authority_manifest_file,
        digest_binding,
        commit_failed_closed_status,
    ):
        manifest = json.loads(Path(authority_manifest_file).read_text("utf-8"))
        assert manifest["digest"]["item_id"] == digest_binding["item_id"]
        resolution_id = "unknown_resolution_212_" + "b" * 32
        if self.before_unknown_commit is not None:
            self.before_unknown_commit()
        status_fingerprint, final_digest_binding = (
            commit_failed_closed_status(resolution_id)
        )
        assert final_digest_binding == digest_binding
        expected = {
            "global_ordinal": 212,
            "resolution_id": resolution_id,
            "preserved_but_unabsorbed": True,
            "digest": {
                **dict(digest_binding),
                "failed_closed_status_fingerprint": status_fingerprint,
            },
            "continuation": {"next_global_ordinal": 213},
            "resolution_receipt_fingerprint": "sha256:" + "e" * 64,
        }
        if self.timeout_212_resolution is not None:
            assert self.timeout_212_resolution == expected
        self.timeout_212_resolution = expected
        return expected

    def validate_timeout_212_resolution_digest(
        self,
        *,
        digest_binding,
        failed_closed_status_fingerprint,
        resolution_id,
    ):
        assert self.timeout_212_resolution is not None
        assert self.timeout_212_resolution["resolution_id"] == resolution_id
        assert self.timeout_212_resolution["digest"] == {
            **dict(digest_binding),
            "failed_closed_status_fingerprint": failed_closed_status_fingerprint,
        }
        return self.timeout_212_resolution

    def resolve_attempt(
        self,
        *,
        authority_manifest_file,
        digest_binding,
        commit_failed_closed_status,
    ):
        manifest = json.loads(Path(authority_manifest_file).read_text("utf-8"))
        assert manifest["digest"] == digest_binding
        ordinal = manifest["activation_total"]
        resolution_id = manifest["resolution_id"]
        if self.before_unknown_commit is not None:
            self.before_unknown_commit()
        status_fingerprint, final_digest_binding = (
            commit_failed_closed_status(resolution_id, ordinal)
        )
        assert final_digest_binding == digest_binding
        expected = {
            "global_ordinal": ordinal,
            "resolution_id": resolution_id,
            "preserved_but_unabsorbed": True,
            "digest": {
                **dict(digest_binding),
                "failed_closed_status_fingerprint": status_fingerprint,
            },
            "continuation": {
                "next_global_ordinal": ordinal + 1,
                "absolute_cap": self.absolute_cap,
            },
            "resolution_receipt_fingerprint": "sha256:" + "d" * 64,
        }
        if self.attempt_resolution is not None:
            assert self.attempt_resolution == expected
        self.attempt_resolution = expected
        self.global_attempt_total = ordinal
        self.global_unknown = 0
        return expected

    def build_attempt_resolution_manifest(
        self,
        *,
        candidate_file,
        authority_ref,
        observed_at,
        digest_binding,
    ):
        ordinal = self.global_attempt_total
        candidate = {
            "authority_ref": authority_ref,
            "activation_total": ordinal,
            "activation_unknown_count": 1,
            "digest": dict(digest_binding),
            "resolution_id": "attempt_resolution_" + "c" * 32,
            "failure_evidence": {"observed_at": observed_at},
            "continuation": {
                "next_global_ordinal": ordinal + 1,
                "absolute_cap": self.absolute_cap,
            },
            "payload_fingerprint": "sha256:" + "e" * 64,
        }
        Path(candidate_file).write_text(
            json.dumps(candidate, sort_keys=True), encoding="utf-8"
        )
        Path(candidate_file).chmod(0o600)
        self.attempt_resolution_candidate = candidate
        return candidate

    def validate_attempt_resolution_digest(
        self,
        *,
        digest_binding,
        failed_closed_status_fingerprint,
        resolution_id,
    ):
        if (
            self.attempt_resolution is None
            or self.attempt_resolution["resolution_id"] != resolution_id
        ):
            raise SemanticHandoffError(
                "synthetic attempt resolution receipt drift"
            )
        terminal_digest = {
            key: value
            for key, value in self.attempt_resolution["digest"].items()
            if key
            not in {
                "checkpoint_fingerprint",
                "business_tree_fingerprint",
            }
        }
        if terminal_digest != {
            **dict(digest_binding),
            "failed_closed_status_fingerprint": failed_closed_status_fingerprint,
        }:
            raise SemanticHandoffError(
                "synthetic attempt resolution digest drift"
            )
        return self.attempt_resolution

    def _write_success_audits(
        self, package: Path, representation_id: str
    ) -> None:
        manifest = json.loads(
            (package / "manifest.json").read_text(encoding="utf-8")
        )
        package_fingerprint = _package_fingerprint(package)
        audit_root = (
            self.workspace / "02_processing" / "semantic_handoff_runs"
        )
        representation_repository = LocalRepresentationRepository(
            self.workspace / "02_processing" / "representations"
        )
        representation = representation_repository.get(representation_id)
        batches = _analysis_batches_for_anchor_unit_ids(
            _units_from_representation(
                representation, representation_repository
            ),
            [batch["unit_ids"] for batch in manifest["batches"]],
        )
        for index, batch in enumerate(batches, start=1):
            anchor_unit_ids = [unit.unit_id for unit in batch.anchor_units]
            _, input_fingerprint = _external_agent_request(
                batch, protocol_version=self.protocol_version
            )
            processing_run_id = "run_" + hashlib.sha256(
                f"{package.name}:{index}".encode()
            ).hexdigest()[:32]
            audit_path = (
                audit_root / processing_run_id / "processing-run-audit.json"
            )
            audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(audit_path.parent, 0o700)
            audit = {
                "schema_version": "processing-run-audit/1.0",
                "artifact_kind": "processing_run_audit",
                "processing_run_id": processing_run_id,
                "protocol_version": self.protocol_version,
                "input_fingerprint": input_fingerprint,
                "anchor_unit_ids": anchor_unit_ids,
                "provider_route": "codex-cli",
                "provider_version": self.provider.provider_version,
                "started_at": "2026-08-18T00:00:00.000Z",
                "finished_at": "2026-08-18T00:00:01.000Z",
                "execution_status": "succeeded",
                "failure_category": None,
                "contract_failure_detail": None,
                "strict_validation_status": "passed",
                "result_fingerprint": "sha256:" + "1" * 64,
                "eligible_units": len(anchor_unit_ids),
                "covered_units": len(anchor_unit_ids),
                "unaccounted_units": 0,
                "diagnostic_schema_version": (
                    "external-agent-diagnostics/2.0"
                    if self.protocol_version
                    in {
                        EXTERNAL_AGENT_PROTOCOL_V3_1,
                        EXTERNAL_AGENT_PROTOCOL_V3_2,
                        EXTERNAL_AGENT_PROTOCOL_V3_3,
                        EXTERNAL_AGENT_PROTOCOL_V3_4,
                    }
                    else "external-agent-diagnostics/1.0"
                ),
                "elapsed_ms": 1000,
                "deadline_ms": 300000,
                "exit_code": 0,
                "termination_signal": None,
                "timeout_phase": None,
                "provider_error_category": None,
                "result_file_present": True,
                "result_size_bytes": 100,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "process_cleanup_status": "verified",
                "result_readback_status": "verified",
                "package_published": True,
                "package_fingerprint": package_fingerprint,
                "information_ingested": True,
                "durable_ingestion_status": "completed",
                "handoff_status": "completed",
                "audit_readback_status": "verified",
            }
            if (
                self.protocol_version != EXTERNAL_AGENT_PROTOCOL_V1
                or self.profiled_v1
            ):
                audit.update(
                    {
                        "model": "gpt-5.6-terra",
                        "reasoning_effort": "medium",
                        "fallback_policy": "none",
                    }
                )
            if self.protocol_version in {
                EXTERNAL_AGENT_PROTOCOL_V3_1,
                EXTERNAL_AGENT_PROTOCOL_V3_2,
                EXTERNAL_AGENT_PROTOCOL_V3_3,
                EXTERNAL_AGENT_PROTOCOL_V3_4,
            }:
                audit.update(
                    {
                        "contract_failure_stage": None,
                        "candidate_item_count": 0,
                        "residue_item_count": 0,
                        "accounting_item_count": 0,
                        "candidate_anchor_ref_count": 0,
                        "residue_anchor_ref_count": 0,
                        "duplicate_anchor_ref_count": 0,
                        "duplicate_accounting_count": 0,
                        "dual_assignment_count": 0,
                        "missing_anchor_count": 0,
                        "unknown_anchor_ref_count": 0,
                    }
                )
                if self.protocol_version == EXTERNAL_AGENT_PROTOCOL_V3_4:
                    audit.update(
                        {
                            "raw_record_count": len(anchor_unit_ids),
                            "projected_record_count": 1,
                            "duplicate_exact_body_count": 0,
                            "grouping_collision_count": 0,
                            "diagnostic_schema_version": (
                                "external-agent-diagnostics/3.0"
                            ),
                        }
                    )
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            os.chmod(audit_path, 0o600)


class ObservedOutOfOrderSemanticHandoff(SyntheticSemanticHandoff):
    """Synthetic result-only lanes with observed overlap and reverse completion."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.completion_orders: list[tuple[str, ...]] = []
        self.last_prepare_metrics: dict[str, int] = {}

    def prepare_results(self, requests, *, parallelism):
        planned = tuple(request.representation_id for request in requests)
        self.prepared_waves.append((planned, parallelism))
        if not planned:
            return {}
        barrier = threading.Barrier(len(planned))
        release = threading.Event()
        state_lock = threading.Lock()
        active = 0
        peak = 0

        def finish(representation_id: str, index: int) -> tuple[str, int]:
            nonlocal active, peak
            started = time.monotonic()
            with state_lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                if len(planned) > 1 and index == 0:
                    release.wait(timeout=5)
                else:
                    release.set()
                return representation_id, round(
                    (time.monotonic() - started) * 1000
                )
            finally:
                with state_lock:
                    active -= 1

        elapsed: dict[str, int] = {}
        completed: list[str] = []
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            futures = {
                executor.submit(finish, representation_id, index)
                for index, representation_id in enumerate(planned)
            }
            for future in as_completed(futures):
                representation_id, elapsed_ms = future.result()
                completed.append(representation_id)
                elapsed[representation_id] = elapsed_ms
        self.completion_orders.append(tuple(completed))
        self.last_prepare_metrics = {
            "semantic_peak_concurrency": peak,
            "resume_provider_calls": 0,
        }
        return elapsed


class InterruptedCommittedWaveSemanticHandoff(SyntheticSemanticHandoff):
    """Two paid packages with a first-item post-ingest interruption."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.fail_after_first_ingest = True
        self.inspect_calls = 0
        self.inspection_drift: str | None = None

    def prepare_results(self, requests, *, parallelism):
        elapsed = super().prepare_results(requests, parallelism=parallelism)
        information = RepresentationInformationService(
            LocalManagedSourceRepository(self.workspace / "01_inbox"),
            LocalRepresentationRepository(
                self.workspace / "02_processing" / "representations"
            ),
            self.workspace / "02_processing" / "information",
        )
        for request in requests:
            package = (
                self.workspace
                / "02_processing"
                / "information"
                / request.representation_id
            )
            if not package.exists():
                information.extract(request.representation_id, self.provider)
        return elapsed

    def inspect_recovery_wave(self, requests):
        self.inspect_calls += 1
        inspections = [
            {
                "classification": "recoverable_committed_result_wave",
                "representation_id": request.representation_id,
                "phase": (
                    "already_ingested_pending_status"
                    if index == 0
                    else "package_pending_ingestion"
                ),
                "atomic_information_ids": [],
                "package_fingerprint": "sha256:" + f"{index + 1:x}" * 64,
                "global_ordinal_range": [177 + index, 177 + index],
            }
            for index, request in enumerate(requests)
        ]
        if self.inspection_drift == "phase":
            inspections[-1]["phase"] = "unknown-phase"
        elif self.inspection_drift == "ordinal":
            inspections[-1]["global_ordinal_range"] = [179, 179]
        elif self.inspection_drift == "mixed":
            inspections[-1] = {
                "classification": "pre_provider",
                "representation_id": requests[-1].representation_id,
                "phase": None,
                "atomic_information_ids": [],
                "package_fingerprint": None,
                "global_ordinal_range": None,
            }
        elif self.inspection_drift == "pre_provider":
            inspections = [
                {
                    "classification": "pre_provider",
                    "representation_id": request.representation_id,
                    "phase": None,
                    "atomic_information_ids": [],
                    "package_fingerprint": None,
                    "global_ordinal_range": None,
                }
                for request in requests
            ]
        return tuple(inspections)

    def execute(
        self,
        representation_id: str,
        *,
        privacy_binding=None,
        authority_binding=None,
    ):
        result = super().execute(
            representation_id,
            privacy_binding=privacy_binding,
            authority_binding=authority_binding,
        )
        package = (
            self.workspace
            / "02_processing"
            / "information"
            / representation_id
        )
        self._write_success_audits(package, representation_id)
        if self.fail_after_first_ingest:
            self.fail_after_first_ingest = False
            raise SemanticHandoffError(
                "synthetic committed-result status interruption"
            )
        return result


class NoStructuralChangeProvider:
    name = "synthetic-no-structural-change"

    def interpret(self, atomic_information, current_world_state):
        del atomic_information, current_world_state
        return InterpretationResult(
            operations=(WorldModelOperation(kind="no_structural_change"),),
            rationale="Synthetic information remains governed Information.",
            evidence_sufficient=True,
            conflict=False,
            ambiguous=False,
        )

    def interpret_batch(self, items):
        return tuple(
            self.interpret(atomic_information, current_world_state)
            for atomic_information, current_world_state in items
        )


class BatchOnceProvider(NoStructuralChangeProvider):
    name = "synthetic-batch-once"

    def __init__(self) -> None:
        self.calls = 0
        self.batch_sizes: list[int] = []

    def interpret(self, atomic_information, current_world_state):
        del atomic_information, current_world_state
        raise AssertionError("batch workflow must not use the single-item method")

    def interpret_batch(self, items):
        batch = tuple(items)
        self.calls += 1
        self.batch_sizes.append(len(batch))
        return tuple(
            InterpretationResult(
                operations=(WorldModelOperation(kind="no_structural_change"),),
                rationale="Synthetic batch governance.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
            )
            for _ in batch
        )


class SharedObjectBatchProvider(NoStructuralChangeProvider):
    name = "synthetic-shared-object-batch"

    def __init__(self, object_id: str) -> None:
        self.object_id = object_id
        self.calls = 0

    def interpret(self, atomic_information, current_world_state):
        del atomic_information, current_world_state
        raise AssertionError("batch workflow must not use the single-item method")

    def interpret_batch(self, items):
        batch = tuple(items)
        if len(batch) != 2:
            raise AssertionError("shared-object fixture requires two items")
        self.calls += 1
        return (
            InterpretationResult(
                operations=(
                    WorldModelOperation(
                        kind="add_role",
                        target_object_id=self.object_id,
                        role="project",
                    ),
                ),
                rationale="Synthetic shared-object role update.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
            ),
            InterpretationResult(
                operations=(WorldModelOperation(kind="no_structural_change"),),
                rationale="Synthetic shared-object no-op.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
            ),
        )


class HumanReviewBatchProvider(NoStructuralChangeProvider):
    name = "synthetic-human-review-batch"

    def __init__(self) -> None:
        self.calls = 0

    def interpret(self, atomic_information, current_world_state):
        del atomic_information, current_world_state
        raise AssertionError("batch workflow must not use the single-item method")

    def interpret_batch(self, items):
        batch = tuple(items)
        if len(batch) != 2:
            raise AssertionError("human-review fixture requires two items")
        self.calls += 1
        return (
            InterpretationResult(
                operations=(WorldModelOperation(kind="conflict"),),
                rationale="Synthetic conflict requires human review.",
                evidence_sufficient=True,
                conflict=True,
                ambiguous=False,
            ),
            InterpretationResult(
                operations=(WorldModelOperation(kind="no_structural_change"),),
                rationale="Synthetic batch no-op.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
            ),
        )


class ClaimEnrichmentBatchProvider(NoStructuralChangeProvider):
    name = "synthetic-claim-enrichment-batch"

    def __init__(self) -> None:
        self.calls = 0

    def interpret(self, atomic_information, current_world_state):
        del atomic_information, current_world_state
        raise AssertionError("batch workflow must not use the single-item method")

    def interpret_batch(self, items):
        batch = tuple(items)
        if len(batch) != 2:
            raise AssertionError("claim-enrichment fixture requires two items")
        self.calls += 1
        first_information = batch[0][0]
        return (
            InterpretationResult(
                operations=(WorldModelOperation(kind="no_structural_change"),),
                rationale="Synthetic claim enrichment.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
                claim=ClaimAttribution(
                    claimant_object_id=None,
                    claimant_source_id=(
                        first_information.source_evidence[0].source_id
                    ),
                    claimant_label="Synthetic Sender",
                    stance="assert",
                    claimed_at=None,
                    attribution_confidence=1.0,
                ),
            ),
            InterpretationResult(
                operations=(WorldModelOperation(kind="no_structural_change"),),
                rationale="Synthetic batch no-op.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
            ),
        )


class FailingBatchProvider(NoStructuralChangeProvider):
    name = "synthetic-failing-batch"

    def __init__(self) -> None:
        self.calls = 0

    def interpret_batch(self, items):
        tuple(items)
        self.calls += 1
        raise RuntimeError("synthetic batch failure")


class StartupFailOnceBatchProvider(NoStructuralChangeProvider):
    name = "synthetic-startup-fail-once"

    def __init__(self, *, fail_restart: bool = False) -> None:
        self.attempts = 0
        self.successful_calls = 0
        self.fail_restart = fail_restart
        self.events: list[tuple[str, str | None]] = []

    @contextmanager
    def session(self):
        yield self

    def metrics_cursor(self):
        return len(self.events)

    def metrics_since(self, cursor):
        events = self.events[cursor:]
        categories = Counter(
            category for _kind, category in events if category is not None
        )
        return {
            "app_server_start_count": sum(
                kind == "startup" for kind, _category in events
            ),
            "thread_count": sum(kind == "thread" for kind, _category in events),
            "turn_count": sum(kind == "turn" for kind, _category in events),
            "startup_wall_ms": sum(
                kind == "startup" for kind, _category in events
            ),
            "turn_wall_ms_sum": sum(
                kind == "turn" for kind, _category in events
            ),
            "turn_wall_ms_max": int(
                any(kind == "turn" for kind, _category in events)
            ),
            "governance_wall_ms": 0,
            "timeout_count": categories.get("timeout", 0),
            "failure_count": sum(categories.values()),
            "failure_categories": dict(categories),
        }

    def invalidate(self, category):
        self.events.append(("failure", category))

    def interpret_batch(self, items):
        batch = tuple(items)
        self.attempts += 1
        if self.attempts == 1 or self.fail_restart:
            self.events.append(("startup", "startup"))
            raise RuntimeError("synthetic startup failure")
        self.events.extend(
            (("startup", None), ("thread", None), ("turn", None))
        )
        self.successful_calls += 1
        return tuple(
            InterpretationResult(
                operations=(WorldModelOperation(kind="no_structural_change"),),
                rationale="Synthetic recovered batch no-op.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
            )
            for _item in batch
        )


class StartupFailTwiceBatchProvider(StartupFailOnceBatchProvider):
    """Fail before thread creation for the first and third business items."""

    def interpret_batch(self, items):
        batch = tuple(items)
        self.attempts += 1
        if self.attempts in {1, 3} or self.fail_restart:
            self.events.append(("startup", "startup"))
            raise RuntimeError("synthetic startup failure")
        self.events.extend(
            (("startup", None), ("thread", None), ("turn", None))
        )
        self.successful_calls += 1
        return tuple(
            InterpretationResult(
                operations=(WorldModelOperation(kind="no_structural_change"),),
                rationale="Synthetic recovered batch no-op.",
                evidence_sufficient=True,
                conflict=False,
                ambiguous=False,
            )
            for _item in batch
        )


class SerialSessionProvider:
    name = "synthetic-serial-session"

    def __init__(self) -> None:
        self.session_entries = 0
        self.session_exits = 0
        self.calls = 0
        self.observed_roles: list[tuple[str, ...]] = []
        self.invalidations: list[str] = []

    @contextmanager
    def session(self):
        self.session_entries += 1
        try:
            yield self
        finally:
            self.session_exits += 1

    def metrics_cursor(self):
        return self.calls

    def metrics_since(self, cursor):
        calls = self.calls - cursor
        return {
            "app_server_start_count": int(cursor == 0 and calls > 0),
            "thread_count": calls,
            "turn_count": calls,
            "startup_wall_ms": 1 if cursor == 0 and calls > 0 else 0,
            "turn_wall_ms_sum": calls,
            "turn_wall_ms_max": int(calls > 0),
            "governance_wall_ms": 0,
            "timeout_count": 0,
            "failure_count": len(self.invalidations),
            "failure_categories": {
                category: self.invalidations.count(category)
                for category in set(self.invalidations)
            },
        }

    def invalidate(self, category):
        self.invalidations.append(category)

    def interpret(self, atomic_information, current_world_state):
        del atomic_information
        self.calls += 1
        roles = tuple(
            role
            for resolved in current_world_state.resolved_objects
            for role in resolved.roles
        )
        self.observed_roles.append(roles)
        if self.calls == 1:
            target = current_world_state.resolved_objects[0].object_id
            operations = (
                WorldModelOperation(
                    kind="add_role", target_object_id=target, role="project"
                ),
            )
        else:
            operations = (WorldModelOperation(kind="no_structural_change"),)
        return InterpretationResult(
            operations=operations,
            rationale="Synthetic serial governance.",
            evidence_sufficient=True,
            conflict=False,
            ambiguous=False,
        )

    def interpret_batch(self, items):
        return tuple(
            self.interpret(atomic_information, current_world_state)
            for atomic_information, current_world_state in items
        )


class TimeoutOnFourthTurnProvider(SerialSessionProvider):
    def metrics_since(self, cursor):
        metrics = super().metrics_since(cursor)
        metrics["timeout_count"] = self.invalidations.count("timeout")
        return metrics

    def interpret(self, atomic_information, current_world_state):
        if self.calls == 3:
            self.calls += 1
            self.invalidations.append("timeout")
            raise CodexInterpretationTimeout("synthetic governance timeout")
        return super().interpret(atomic_information, current_world_state)


class FailGovernanceOnceService(WechatDigestService):
    fail_governance = True

    def _govern(self, atomic_ids):
        if self.fail_governance:
            self.fail_governance = False
            raise RuntimeError("synthetic downstream failure")
        return super()._govern(atomic_ids)


class FailAfterGovernanceOnceService(WechatDigestService):
    fail_after_governance = True

    def _update_item(self, run_id, status, item_id, **changes):
        if self.fail_after_governance and changes.get("state") == "processed":
            self.fail_after_governance = False
            raise RuntimeError("synthetic post-governance failure")
        return super()._update_item(run_id, status, item_id, **changes)


class FailAfterProviderOnceService(WechatDigestService):
    fail_after_provider = True

    def _govern(self, atomic_ids):
        result = super()._govern(atomic_ids)
        if self.fail_after_provider:
            self.fail_after_provider = False
            raise RuntimeError("synthetic crash before governance receipt completion")
        return result


class FailAfterPersistedBatchOnceService(WechatDigestService):
    fail_after_batch = True

    def _update_item(self, run_id, status, item_id, **changes):
        result = super()._update_item(run_id, status, item_id, **changes)
        receipt = changes.get("governance_receipt")
        if (
            self.fail_after_batch
            and isinstance(receipt, dict)
            and receipt.get("phase") == "interpreted"
        ):
            self.fail_after_batch = False
            raise OSError("synthetic post-batch persistence interruption")
        return result


class FailAfterPersistedBatchProgressOnceService(WechatDigestService):
    fail_progress = True

    def _update_item(self, run_id, status, item_id, **changes):
        result = super()._update_item(run_id, status, item_id, **changes)
        receipt = changes.get("governance_receipt")
        if (
            self.fail_progress
            and isinstance(receipt, dict)
            and receipt.get("phase") == "applying"
            and receipt.get("next_index") == 1
        ):
            self.fail_progress = False
            raise OSError("synthetic post-persist progress interruption")
        return result


class FailBeforePersistedBatchProgressOnceService(WechatDigestService):
    fail_progress = True

    def _update_item(self, run_id, status, item_id, **changes):
        receipt = changes.get("governance_receipt")
        if (
            self.fail_progress
            and isinstance(receipt, dict)
            and receipt.get("phase") == "applying"
            and receipt.get("next_index") == 1
            and receipt.get("in_flight_index") is None
        ):
            self.fail_progress = False
            raise OSError("synthetic pre-persist progress interruption")
        return super()._update_item(run_id, status, item_id, **changes)


class SharedObjectInterruptedBatchService(
    FailAfterPersistedBatchProgressOnceService
):
    shared_bindings_installed = False
    shared_object_id: str

    def _govern(self, atomic_ids):
        if not self.shared_bindings_installed:
            self.shared_bindings_installed = True
            for atomic_id in atomic_ids:
                current = self.information_store.get_current(atomic_id)
                next_revision = current.revision_number + 1
                self.information_store.append_revision(
                    replace(
                        current,
                        revision_number=next_revision,
                        revision_id=f"{atomic_id}-r{next_revision:04d}",
                        related_object_ids=(self.shared_object_id,),
                        revision_reason="synthetic_shared_object_binding",
                    )
                )
        return super()._govern(atomic_ids)


class SharedObjectPreCursorBatchService(
    FailBeforePersistedBatchProgressOnceService
):
    shared_bindings_installed = False
    shared_object_id: str

    def _govern(self, atomic_ids):
        if not self.shared_bindings_installed:
            self.shared_bindings_installed = True
            for atomic_id in atomic_ids:
                current = self.information_store.get_current(atomic_id)
                next_revision = current.revision_number + 1
                self.information_store.append_revision(
                    replace(
                        current,
                        revision_number=next_revision,
                        revision_id=f"{atomic_id}-r{next_revision:04d}",
                        related_object_ids=(self.shared_object_id,),
                        revision_reason="synthetic_shared_object_binding",
                    )
                )
        return super()._govern(atomic_ids)


class FailSecondGovernanceOnceService(WechatDigestService):
    governance_calls = 0
    failed = False

    def _govern(self, atomic_ids):
        self.governance_calls += 1
        if self.governance_calls == 2 and not self.failed:
            self.failed = True
            raise RuntimeError("synthetic second-window failure")
        return super()._govern(atomic_ids)


class WechatDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        for directory in ("01_inbox", "02_processing", "03_information", "04_core"):
            (self.workspace / directory).mkdir(parents=True, exist_ok=True)
        self.semantic = SyntheticSemanticHandoff(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(
        self,
        capture: SyntheticCaptureProvider,
        *,
        service_type=WechatDigestService,
        run_store: WechatDigestRunStore | None = None,
    ) -> WechatDigestService:
        return service_type(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=NoStructuralChangeProvider(),
            run_store=run_store,
        )

    def create_object(self, name: str = "Synthetic Project") -> str:
        with SQLiteWorldModelRepository(
            self.workspace / "04_core" / "archeos.sqlite3"
        ) as repository:
            return repository.create_object(name).object_id

    def interrupted_batch_cursor_one(
        self,
    ) -> tuple[WechatDigestService, BatchOnceProvider, dict[str, object]]:
        self.create_object()
        provider = BatchOnceProvider()
        service = FailAfterPersistedBatchProgressOnceService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "applying")
        self.assertEqual(receipt["next_index"], 1)
        self.assertEqual(provider.calls, 1)
        return service, provider, receipt

    def interrupted_shared_object_batch_cursor_one(
        self,
    ) -> tuple[
        WechatDigestService,
        SharedObjectBatchProvider,
        dict[str, object],
        str,
    ]:
        object_id = self.create_object()
        provider = SharedObjectBatchProvider(object_id)
        service = SharedObjectInterruptedBatchService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        service.shared_object_id = object_id
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "applying")
        self.assertEqual(receipt["next_index"], 1)
        self.assertEqual(provider.calls, 1)
        return service, provider, receipt, object_id

    def interrupted_shared_object_batch_before_cursor(
        self,
    ) -> tuple[
        WechatDigestService,
        SharedObjectBatchProvider,
        dict[str, object],
        str,
    ]:
        object_id = self.create_object()
        provider = SharedObjectBatchProvider(object_id)
        service = SharedObjectPreCursorBatchService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        service.shared_object_id = object_id
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "applying")
        self.assertEqual(receipt["next_index"], 0)
        self.assertEqual(receipt["in_flight_index"], 0)
        self.assertEqual(provider.calls, 1)
        return service, provider, receipt, object_id

    def interrupted_human_review_batch_before_cursor(
        self,
    ) -> tuple[WechatDigestService, HumanReviewBatchProvider]:
        object_id = self.create_object()
        provider = HumanReviewBatchProvider()
        service = SharedObjectPreCursorBatchService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        service.shared_object_id = object_id
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "applying")
        self.assertEqual(receipt["in_flight_index"], 0)
        self.assertEqual(len(service.proposal_store.list_unresolved()), 1)
        self.assertEqual(provider.calls, 1)
        return service, provider

    def interrupted_claim_enrichment_batch_before_cursor(
        self,
    ) -> tuple[WechatDigestService, ClaimEnrichmentBatchProvider, str]:
        object_id = self.create_object()
        provider = ClaimEnrichmentBatchProvider()
        service = SharedObjectPreCursorBatchService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        service.shared_object_id = object_id
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        atomic_id = receipt["batch_atomic_information_ids"][0]
        self.assertEqual(receipt["phase"], "applying")
        self.assertEqual(receipt["in_flight_index"], 0)
        self.assertIsNotNone(
            service.information_store.get_current(atomic_id).claim
        )
        self.assertEqual(provider.calls, 1)
        return service, provider, atomic_id

    def interrupted_identity_review_batch_before_cursor(
        self,
    ) -> tuple[WechatDigestService, BatchOnceProvider, str]:
        self.create_object("Ambiguous Project")
        self.create_object("Ambiguous Project")
        provider = BatchOnceProvider()
        service = FailBeforePersistedBatchProgressOnceService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider(
                [
                    message(1, content="Ambiguous project update one."),
                    message(2, content="Ambiguous project update two."),
                ]
            ),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "applying")
        self.assertEqual(receipt["in_flight_index"], 0)
        proposal = service.proposal_store.list_unresolved()[0]
        self.assertEqual(
            proposal.human_review.allowed_actions,
            (
                "bind_existing",
                "create_minimal",
                "edit_identity_and_create",
                "reject",
                "defer",
            ),
        )
        self.assertEqual(provider.calls, 1)
        return service, provider, proposal.proposal_id

    @staticmethod
    def append_information_drift(
        service: WechatDigestService, atomic_information_id: str
    ) -> None:
        current = service.information_store.get_current(atomic_information_id)
        next_revision = current.revision_number + 1
        service.information_store.append_revision(
            replace(
                current,
                revision_number=next_revision,
                revision_id=(
                    f"{current.atomic_information_id}-r{next_revision:04d}"
                ),
                revision_reason="synthetic_resume_drift",
            )
        )

    @staticmethod
    def governance_business_state(service: WechatDigestService):
        information = tuple(
            revision
            for current in service.information_store.list_atomic_information()
            for revision in service.information_store.list_revisions(
                current.atomic_information_id
            )
        )
        with SQLiteWorldModelRepository(service.database) as repository:
            objects = repository.list_objects()
            return (
                information,
                service.proposal_store.list_history(),
                service.journal.list_changes(),
                objects,
                tuple(
                    item
                    for record in objects
                    for item in repository.list_names(record.object_id)
                ),
                tuple(
                    item
                    for record in objects
                    for item in repository.list_roles(record.object_id)
                ),
                tuple(
                    item
                    for record in objects
                    for item in repository.list_lifecycles(record.object_id)
                ),
                repository.list_relationships(active_only=False),
                repository.list_external_identity_mappings(),
                repository.list_apply_receipts(),
            )

    def source_count(self) -> int:
        return len(
            LocalManagedSourceRepository(
                self.workspace / "01_inbox"
            ).list_sources()
        )

    def governance_timeout_fixture(self, *, include_next: bool = True):
        self.create_object()
        messages = [
            message(index, conversation="failed")
            for index in range(1, 11)
        ]
        if include_next:
            messages.append(message(11, conversation="next"))
        capture = SyntheticCaptureProvider(messages)
        provider = TimeoutOnFourthTurnProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        item_id = next(
            key
            for key, item in status["items"].items()
            if isinstance(item, dict)
            and isinstance(item.get("governance_receipt"), dict)
            and item["governance_receipt"].get("phase") == "started"
        )
        self.assertEqual(status["failure_category"], "CodexInterpretationTimeout")
        self.assertEqual(
            len(status["items"][item_id]["atomic_information_ids"]), 10
        )
        # Preserve coverage for the historical pre-Issue-150 timeout shape.
        status["items"][item_id]["atomic_information_ids"] = []
        service.run_store.update_status(run_id, status)
        self.assertEqual(
            len(service.information_store.list_atomic_information()), 10
        )
        return service, capture, provider, run_id, item_id

    def governance_startup_recovery_fixture(
        self,
        *,
        fail_restart=False,
        service_type=WechatDigestService,
    ):
        self.create_object()
        self.semantic.provider.mode = "four_one"
        self.semantic.global_attempt_total = 298
        messages = [
            message(index, conversation="startup-recovery")
            for index in range(1, 6)
        ]
        capture = SyntheticCaptureProvider(messages)
        provider = StartupFailOnceBatchProvider(fail_restart=fail_restart)
        service = service_type(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        plan = service.run_store.plan(run_id)
        status = service.run_store.status(run_id)
        item_id = next(
            key
            for key, item in status["items"].items()
            if isinstance(item, dict)
            and isinstance(item.get("governance_receipt"), dict)
            and item["governance_receipt"].get("phase") == "started"
        )
        atomic_ids = list(status["items"][item_id]["atomic_information_ids"])
        self.assertEqual(len(atomic_ids), 4)
        status["items"][item_id]["atomic_information_ids"] = []
        service.run_store.update_status(run_id, status)
        self.semantic.latest_representation_id = status["items"][item_id][
            "representation_id"
        ]
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=plan["created_at"],
            lower_cursor=(0, "", ""),
            frozen_global_upper_cursor=(
                plan["all_history_upper_bound"]["timestamp"],
                plan["all_history_upper_bound"]["conversation_key"],
                plan["all_history_upper_bound"]["message_key"],
            ),
            capture_provider_version=plan["provider_version"],
            semantic_batch_size=plan["semantic_batch_size"],
            reviewed_git_head=(
                "67d159411e968c6b0c2f787f9063a22682c10fb9"
            ),
        )
        self.semantic.reviewed_git_head = "a" * 40
        return service, provider, run_id, item_id, atomic_ids

    def multi_governance_startup_recovery_fixture(
        self, *, service_type=WechatDigestService
    ):
        self.create_object()
        self.semantic.provider.mode = "four_one"
        self.semantic.global_attempt_total = 298
        conversations = sorted(
            ("issue-168-history", "issue-168-current"),
            key=lambda value: hashlib.sha256(value.encode()).hexdigest(),
        )
        capture = SyntheticCaptureProvider(
            [
                message(index, conversation=conversations[0])
                for index in range(1, 6)
            ]
            + [
                message(index, conversation=conversations[1])
                for index in range(6, 9)
            ]
        )
        provider = StartupFailTwiceBatchProvider()
        service = service_type(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        if isinstance(service, FailAfterPersistedBatchProgressOnceService):
            service.fail_progress = False
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        plan = service.run_store.plan(run_id)
        status = service.run_store.status(run_id)
        historical_item_id = next(
            item_id
            for item_id, item in status["items"].items()
            if isinstance(item, dict)
            and isinstance(item.get("governance_receipt"), dict)
            and item["governance_receipt"].get("phase") == "started"
        )
        historical_atomic_ids = list(
            status["items"][historical_item_id]["atomic_information_ids"]
        )
        status["items"][historical_item_id]["atomic_information_ids"] = []
        service.run_store.update_status(run_id, status)
        self.semantic.latest_representation_id = status["items"][
            historical_item_id
        ]["representation_id"]
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=plan["created_at"],
            lower_cursor=(0, "", ""),
            frozen_global_upper_cursor=(
                plan["all_history_upper_bound"]["timestamp"],
                plan["all_history_upper_bound"]["conversation_key"],
                plan["all_history_upper_bound"]["message_key"],
            ),
            capture_provider_version=plan["provider_version"],
            semantic_batch_size=plan["semantic_batch_size"],
            reviewed_git_head=(
                "67d159411e968c6b0c2f787f9063a22682c10fb9"
            ),
        )
        self.semantic.reviewed_git_head = "a" * 40
        historical_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        historical_manifest = service.build_governance_startup_recovery_manifest(
            authority_ref=historical_ref
        )
        historical_manifest_path = (
            Path(self.temporary.name) / "issue-168-history.json"
        )
        historical_manifest_path.write_text(
            json.dumps(historical_manifest), encoding="utf-8"
        )
        os.chmod(historical_manifest_path, 0o600)
        service.resolve_governance_startup_failure(
            authority_ref=historical_ref,
            authority_manifest_file=historical_manifest_path,
        )
        service.run(max_terminal_items=1)
        self.assertEqual(provider.attempts, 2)
        self.assertEqual(
            service.run_store.status(run_id)["items"][historical_item_id][
                "atomic_information_ids"
            ],
            historical_atomic_ids,
        )

        self.semantic.provider.mode = "all_candidate"
        self.semantic.global_attempt_total = 302
        self.semantic.reviewed_git_head = (
            "ce49d89355caab38da08b4522f416d248c60646b"
        )
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=self.semantic.campaign_binding.created_at,
            lower_cursor=self.semantic.campaign_binding.lower_cursor,
            frozen_global_upper_cursor=(
                self.semantic.campaign_binding.frozen_global_upper_cursor
            ),
            capture_provider_version=(
                self.semantic.campaign_binding.capture_provider_version
            ),
            semantic_batch_size=self.semantic.campaign_binding.semantic_batch_size,
            reviewed_git_head=self.semantic.reviewed_git_head,
        )
        with self.assertRaises(WechatDigestError):
            service.run(max_terminal_items=1)
        with self.assertRaisesRegex(WechatDigestError, "completion 未知"):
            service.run(max_terminal_items=1)
        status = service.run_store.status(run_id)
        current_item_id = next(
            item_id
            for item_id, item in status["items"].items()
            if item_id != historical_item_id
            and isinstance(item, dict)
            and isinstance(item.get("governance_receipt"), dict)
            and item["governance_receipt"].get("phase") == "started"
        )
        current_atomic_ids = list(
            status["items"][current_item_id]["atomic_information_ids"]
        )
        self.assertEqual(len(current_atomic_ids), 3)
        self.assertEqual(status["failure_category"], "WechatDigestError")
        self.semantic.latest_representation_id = status["items"][current_item_id][
            "representation_id"
        ]
        self.semantic.reviewed_git_head = "b" * 40
        if isinstance(service, FailAfterPersistedBatchProgressOnceService):
            service.fail_progress = True
        return (
            service,
            provider,
            run_id,
            historical_item_id,
            current_item_id,
            current_atomic_ids,
        )

    def failed_closed_recovery_fixture(self):
        self.create_object()
        self.semantic.provider.mode = "four_one"
        self.semantic.global_attempt_total = 298
        ordered_conversations = sorted(
            ("issue-154-startup", "issue-154-next"),
            key=lambda value: hashlib.sha256(value.encode()).hexdigest(),
        )
        capture = SyntheticCaptureProvider(
            [
                message(index, conversation=ordered_conversations[0])
                for index in range(1, 6)
            ]
            + [message(6, conversation=ordered_conversations[1])]
        )
        provider = StartupFailOnceBatchProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        plan = service.run_store.plan(run_id)
        status = service.run_store.status(run_id)
        startup_item_id = next(
            item_id
            for item_id, item in status["items"].items()
            if isinstance(item, dict)
            and isinstance(item.get("governance_receipt"), dict)
        )
        status["items"][startup_item_id]["atomic_information_ids"] = []
        service.run_store.update_status(run_id, status)
        self.semantic.latest_representation_id = status["items"][startup_item_id][
            "representation_id"
        ]
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=plan["created_at"],
            lower_cursor=(0, "", ""),
            frozen_global_upper_cursor=(
                plan["all_history_upper_bound"]["timestamp"],
                plan["all_history_upper_bound"]["conversation_key"],
                plan["all_history_upper_bound"]["message_key"],
            ),
            capture_provider_version=plan["provider_version"],
            semantic_batch_size=plan["semantic_batch_size"],
            reviewed_git_head=(
                "67d159411e968c6b0c2f787f9063a22682c10fb9"
            ),
        )
        self.semantic.reviewed_git_head = (
            "c8ece3782ae3ba289d06c36d1e352ce23e0f627b"
        )
        startup_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        startup_manifest = service.build_governance_startup_recovery_manifest(
            authority_ref=startup_ref
        )
        startup_manifest_path = (
            Path(self.temporary.name) / "issue-150-for-154.json"
        )
        startup_manifest_path.write_text(
            json.dumps(startup_manifest), encoding="utf-8"
        )
        os.chmod(startup_manifest_path, 0o600)
        service.resolve_governance_startup_failure(
            authority_ref=startup_ref,
            authority_manifest_file=startup_manifest_path,
        )
        service.run(max_terminal_items=1)
        prepared = service.prepare_next_semantic()
        status = service.run_store.status(run_id)
        current_item_id = next(
            item_id
            for item_id, item in status["items"].items()
            if isinstance(item, dict)
            and item.get("representation_id") == prepared.representation_id
        )
        self.assertEqual(status["items"][startup_item_id]["state"], "processed")
        self.assertEqual(
            status["items"][current_item_id]["state"], "represented"
        )
        additions = {
            "local_only": 3,
            "pending_human": 20,
            "planned": 16,
            "processed": 6,
            "unsupported": 142,
        }
        index = 0
        for state, count in additions.items():
            for _ in range(count):
                index += 1
                status["items"][f"synthetic-154-{index:03d}"] = {
                    "state": state
                }
        status["state"] = "failed"
        status["failure_category"] = "WechatDigestError"
        status["checkpoint_published"] = False
        service.run_store.update_status(run_id, status)
        historical_run_ids = []
        for historical_index, failure_variant in enumerate(
            ("semantic", "semantic", "governance", "governance", "governance"),
            start=1,
        ):
            historical_capture = SyntheticCaptureProvider(
                [
                    message(
                        1000 + historical_index,
                        conversation=f"issue-154-history-{historical_index}",
                    )
                ]
            ).capture(ZERO_CURSOR)
            historical_plan, historical_status = _build_plan(
                historical_capture,
                clock=lambda: "2026-08-21T00:00:00+00:00",
            )
            historical_run_id = str(historical_plan["run_id"])
            historical_item = next(
                iter(historical_status["items"].values())
            )
            historical_item["state"] = "failed_closed"
            if failure_variant == "semantic":
                historical_item["semantic_failure"] = {
                    "synthetic": True
                }
            else:
                historical_item["governance_failure"] = {
                    "synthetic": True
                }
            service.run_store.create(historical_plan, historical_status)
            historical_run_ids.append(historical_run_id)
        service.run_store.active_path.write_text(
            json.dumps({"active_run_id": run_id}), encoding="utf-8"
        )
        self.issue154_historical_run_ids = historical_run_ids
        self.semantic.reviewed_git_head = "9" * 40
        return service, provider, run_id, current_item_id, startup_item_id

    def maintenance_continuation_failed_state_fixture(self):
        self.create_object()
        ordered_conversations = sorted(
            (f"maintenance-stage-{index}" for index in range(6)),
            key=lambda value: hashlib.sha256(value.encode()).hexdigest(),
        )
        messages = [message(1, conversation=ordered_conversations[0])]
        messages.extend(
            message(index, conversation=ordered_conversations[1])
            for index in range(2, 12)
        )
        messages.extend(
            message(index, conversation=conversation)
            for index, conversation in enumerate(
                ordered_conversations[2:], start=12
            )
        )
        capture = SyntheticCaptureProvider(messages)
        provider = TimeoutOnFourthTurnProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        service.install_semantic_authority(
            inventory_authority_file=Path("/private/authority-a.json")
        )
        prepared = service.prepare_next_semantic()
        status = service.run_store.status(prepared.run_id)
        item_id = next(
            key
            for key, item in status["items"].items()
            if item.get("representation_id") == prepared.representation_id
        )
        manifest_path = self.workspace / "private-unknown-authority.json"
        manifest_path.write_text(
            json.dumps({"digest": {"item_id": item_id}}), encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
        service.resolve_semantic_unknown(
            authority_manifest_file=manifest_path
        )
        with self.assertRaises(WechatDigestError):
            service.run()
        service.seal_governance_timeout()
        service.prepare_next_semantic()
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        pre_failure_status = service.run_store.status(run_id)
        pre_failure_counts = Counter(
            item["state"]
            for item in pre_failure_status["items"].values()
        )
        self.assertEqual(pre_failure_counts["represented"], 1)
        self.assertEqual(pre_failure_counts["planned"], 3, pre_failure_counts)
        self.semantic.reviewed_git_head = "9" * 40
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls

        with patch.object(
            service,
            "_govern",
            side_effect=WechatDigestError("synthetic pre-Provider failure"),
        ):
            with self.assertRaises(WechatDigestError):
                service.run()

        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        counts = Counter(item["state"] for item in status["items"].values())
        failure_variants = Counter(
            "semantic"
            if item.get("semantic_failure") is not None
            else "governance"
            for item in status["items"].values()
            if item["state"] == "failed_closed"
        )
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["failure_category"], "WechatDigestError")
        self.assertFalse(status["checkpoint_published"])
        self.assertEqual(counts["represented"], 1)
        self.assertEqual(counts["planned"], 3, counts)
        self.assertEqual(failure_variants, {"semantic": 1, "governance": 1})
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)
        return service, provider, run_id

    def assert_governance_timeout_seal_rejected(self, mutate) -> None:
        service, _capture, provider, run_id, item_id = (
            self.governance_timeout_fixture(include_next=False)
        )
        mutate(service, run_id, item_id)
        status_path = service.run_store.runs_root / run_id / "status.json"
        status_before = status_path.read_bytes()
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls
        with self.assertRaises((OSError, RuntimeError, WechatDigestError)):
            service.seal_governance_timeout()
        self.assertEqual(status_path.read_bytes(), status_before)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)

    def test_no_checkpoint_plain_digest_fails_closed(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        with self.assertRaisesRegex(WechatDigestError, "首次使用"):
            self.service(capture).run()
        self.assertEqual(capture.calls, [])

    def test_since_bootstrap_and_daily_empty_incremental(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1), message(2)])
        first = self.service(capture).run(since="2023-01-01")
        self.assertEqual(first.new_messages, 2)
        self.assertEqual(first.durable_information, 2)
        self.assertEqual(first.context_objects, 1)
        self.assertTrue(first.checkpoint_published)

        second = self.service(capture).run()
        self.assertEqual(second.new_messages, 0)
        self.assertEqual(second.durable_information, 0)
        self.assertEqual(self.source_count(), 1)

    def test_governance_batch_uses_one_bounded_world_state_snapshot(
        self,
    ) -> None:
        self.create_object()
        provider = SerialSessionProvider()
        capture = SyntheticCaptureProvider([message(1), message(2)])
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        result = service.run(since="2023-01-01")

        self.assertEqual(provider.session_entries, 1)
        self.assertEqual(provider.session_exits, 1)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.observed_roles, [(), ()])
        self.assertEqual(result.governance_app_server_starts, 1)
        self.assertEqual(result.governance_threads, 2)
        self.assertEqual(result.governance_turns, 2)
        self.assertEqual(result.governance_failures, 0)
        metrics = next(
            item["governance_metrics"]
            for run_path in service.run_store.runs_root.iterdir()
            for item in service.run_store.status(run_path.name)["items"].values()
            if isinstance(item, dict) and "governance_metrics" in item
        )
        self.assertEqual(metrics["app_server_start_count"], 1)
        self.assertEqual(metrics["thread_count"], 2)
        self.assertEqual(metrics["turn_count"], 2)
        self.assertEqual(
            set(metrics),
            {
                "app_server_start_count",
                "thread_count",
                "turn_count",
                "startup_wall_ms",
                "turn_wall_ms_sum",
                "turn_wall_ms_max",
                "governance_wall_ms",
                "timeout_count",
                "failure_count",
                "failure_categories",
            },
        )
        self.assertNotIn("atomic_info_", json.dumps(metrics))
        self.assertNotIn(str(self.workspace), json.dumps(metrics))

    def test_zero_governance_path_does_not_start_lazy_app_server(self) -> None:
        capture = SyntheticCaptureProvider([])

        def forbidden_loader():
            raise AssertionError("zero-governance path must not load the SDK")

        provider = CodexAtomicInformationInterpretationProvider(
            sdk_loader=forbidden_loader
        )
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        result = service.run(from_now=True)

        self.assertEqual(result.governance_app_server_starts, 0)
        self.assertEqual(result.governance_threads, 0)
        self.assertEqual(result.governance_turns, 0)

    def test_schema_failure_destroys_session_and_stops_later_atomic(self) -> None:
        self.create_object()

        class Turn:
            def run(self):
                return SimpleNamespace(final_response="not-json")

            def interrupt(self):
                raise AssertionError("schema failure must not interrupt")

        class Thread:
            def turn(self, *_args, **_kwargs):
                return Turn()

        class Codex:
            instance = None

            def __init__(self):
                self.closed = False
                self.thread_starts = 0
                self.__class__.instance = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def thread_start(self, **_kwargs):
                self.thread_starts += 1
                return Thread()

        provider = CodexAtomicInformationInterpretationProvider(
            sdk_loader=lambda: (Codex, "deny-all", "read-only")
        )
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(since="2023-01-01")

        assert Codex.instance is not None
        self.assertTrue(Codex.instance.closed)
        self.assertEqual(Codex.instance.thread_starts, 1)
        metrics = provider.metrics_since(0)
        self.assertEqual(metrics["turn_count"], 1)
        self.assertEqual(metrics["failure_categories"], {"schema": 1})
        active_run_id = service.run_store.active_run_id()
        assert active_run_id is not None
        status = service.run_store.status(active_run_id)
        self.assertEqual(status["state"], "failed")
        item_metrics = next(
            item["governance_metrics"]
            for item in status["items"].values()
            if isinstance(item, dict) and "governance_metrics" in item
        )
        self.assertEqual(item_metrics["failure_categories"], {"schema": 1})

    def test_completed_governance_receipt_recovery_makes_zero_provider_calls(
        self,
    ) -> None:
        self.create_object()
        provider = SerialSessionProvider()
        capture = SyntheticCaptureProvider([message(1)])
        service = FailAfterGovernanceOnceService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        self.assertEqual(provider.calls, 1)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "completed")

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)

    def test_ten_atomic_information_use_one_governance_provider_call(self) -> None:
        self.create_object()
        provider = BatchOnceProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider(
                [message(index) for index in range(1, 11)]
            ),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        result = service.run(all_history=True)

        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.batch_sizes, [10])

    def test_failed_batch_leaves_identity_and_governance_state_unmodified(
        self,
    ) -> None:
        self.create_object()
        provider = FailingBatchProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)

        self.assertEqual(provider.calls, 1)
        revisions = service.information_store.list_atomic_information()
        self.assertEqual(len(revisions), 2)
        self.assertTrue(
            all(
                item.revision_number == 1 and not item.related_object_ids
                for item in revisions
            )
        )
        self.assertEqual(service.proposal_store.list_unresolved(), ())
        self.assertEqual(service.journal.list_changes(), ())

    def test_issue_135_migration_freezes_15_and_batches_only_remaining_3(
        self,
    ) -> None:
        provider = BatchOnceProvider()
        capture = SyntheticCaptureProvider(
            [message(index) for index in range(1, 19)]
        )
        service = FailAfterPersistedBatchProgressOnceService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        prepared = service.prepare_next_semantic()
        privacy = service.privacy_gate.evaluate(
            service._representation_texts(prepared.representation_id),
            semantic_completeness_known=True,
        )
        atomic_ids = service._semantic(
            prepared.run_id, prepared.representation_id, privacy
        )
        self.assertEqual(len(atomic_ids), 18)

        self.create_object()
        with SQLiteWorldModelRepository(service.database) as repository:
            identity = IdentityGateService(
                service.information_store,
                repository,
                service.proposal_store,
                service.journal,
                BusinessLanguageHumanJudgmentPort(),
            )
            for atomic_id in atomic_ids[:14]:
                current = service.information_store.get_current(atomic_id)
                identity.process(
                    atomic_id,
                    IdentityEvidence(
                        name="Synthetic Project",
                        supporting_revision_ids=(current.revision_id,),
                        identity_bases=(),
                    ),
                )

        status = service.run_store.status(prepared.run_id)
        item_id = next(
            key
            for key, item in status["items"].items()
            if item.get("representation_id") == prepared.representation_id
        )
        item = status["items"][item_id]
        item["state"] = "represented"
        item["privacy_route"] = None
        item["privacy_categories"] = []
        item["atomic_information_ids"] = []
        item["pending_human"] = False
        item["context_object_ids"] = []
        item["governance_receipt"] = {
            "schema_version": "wechat-governance-receipt/1.0",
            "phase": "started",
            "atomic_information_fingerprint": (
                _governance_atomic_fingerprint(atomic_ids)
            ),
        }
        item["governance_metrics"] = {
            "app_server_start_count": 0,
            "thread_count": 15,
            "turn_count": 15,
            "startup_wall_ms": 0,
            "turn_wall_ms_sum": 150,
            "turn_wall_ms_max": 10,
            "governance_wall_ms": 160,
            "timeout_count": 0,
            "failure_count": 1,
            "failure_categories": {"transport": 1},
        }
        status["state"] = "failed"
        status["failure_category"] = "BrokenPipeError"
        service.run_store.update_status(prepared.run_id, status)
        self.semantic.global_attempt_total = 220
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/135"
            "#issuecomment-5353999999"
        )
        authority_manifest = service.build_batch_governance_authority_manifest(
            authority_ref=authority_ref,
            completed_atomic_information_ids=atomic_ids[:15],
            remaining_atomic_information_ids=atomic_ids[15:],
        )
        authority_file = self.workspace / "issue-135-authority.json"
        authority_file.write_text(
            json.dumps(authority_manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        authority_file.chmod(0o600)

        def write_resigned_manifest(name, mutate):
            payload = json.loads(json.dumps(authority_manifest))
            mutate(payload)
            payload.pop("manifest_fingerprint", None)
            payload["manifest_fingerprint"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            path = self.workspace / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
            return path

        tampered_files = (
            write_resigned_manifest(
                "issue-135-order.json",
                lambda payload: payload[
                    "completed_atomic_information_ids"
                ].reverse(),
            ),
            write_resigned_manifest(
                "issue-135-atomic.json",
                lambda payload: payload["completed_effect_bindings"][0].update(
                    {"current_revision_fingerprint": "sha256:" + "a" * 64}
                ),
            ),
            write_resigned_manifest(
                "issue-135-journal.json",
                lambda payload: payload["completed_effect_bindings"][0].update(
                    {"journal_fingerprint": "sha256:" + "b" * 64}
                ),
            ),
            write_resigned_manifest(
                "issue-135-proposal.json",
                lambda payload: payload["completed_effect_bindings"][0].update(
                    {"proposal_history_fingerprint": "sha256:" + "c" * 64}
                ),
            ),
            write_resigned_manifest(
                "issue-135-receipt.json",
                lambda payload: payload["completed_effect_bindings"][0].update(
                    {"apply_receipts_fingerprint": "sha256:" + "d" * 64}
                ),
            ),
            write_resigned_manifest(
                "issue-135-world.json",
                lambda payload: payload["completed_effect_bindings"][0].update(
                    {"world_projection_fingerprint": "sha256:" + "e" * 64}
                ),
            ),
        )
        for tampered_file in tampered_files:
            with self.assertRaises(WechatDigestError):
                service.activate_batch_governance(
                    authority_ref=authority_ref,
                    authority_manifest_file=tampered_file,
                )
        invalid_fingerprint_file = self.workspace / "issue-135-fingerprint.json"
        invalid_fingerprint = json.loads(json.dumps(authority_manifest))
        invalid_fingerprint["manifest_fingerprint"] = "sha256:" + "f" * 64
        invalid_fingerprint_file.write_text(
            json.dumps(invalid_fingerprint), encoding="utf-8"
        )
        invalid_fingerprint_file.chmod(0o600)
        with self.assertRaises(WechatDigestError):
            service.activate_batch_governance(
                authority_ref=authority_ref,
                authority_manifest_file=invalid_fingerprint_file,
            )
        authority_file.chmod(0o644)
        with self.assertRaises(WechatDigestError):
            service.activate_batch_governance(
                authority_ref=authority_ref,
                authority_manifest_file=authority_file,
            )
        authority_file.chmod(0o600)
        authority_link = self.workspace / "issue-135-authority-link.json"
        authority_link.symlink_to(authority_file)
        with self.assertRaises(WechatDigestError):
            service.activate_batch_governance(
                authority_ref=authority_ref,
                authority_manifest_file=authority_link,
            )
        self.semantic.global_attempt_total = 221
        with self.assertRaises(WechatDigestError):
            service.activate_batch_governance(
                authority_ref=authority_ref,
                authority_manifest_file=authority_file,
            )
        self.semantic.global_attempt_total = 220
        self.assertEqual(
            service.build_batch_governance_authority_manifest(
                authority_ref=authority_ref,
                completed_atomic_information_ids=atomic_ids[:15],
                remaining_atomic_information_ids=atomic_ids[15:],
            ),
            authority_manifest,
        )
        completed_before = [
            service.information_store.list_revisions(atomic_id)
            for atomic_id in atomic_ids[:15]
        ]

        migration = service.activate_batch_governance(
            authority_ref=authority_ref,
            authority_manifest_file=authority_file,
        )
        self.assertEqual(
            self.semantic.campaign_binding.reviewed_git_head,
            self.semantic.reviewed_git_head,
        )
        campaign_head = self.semantic.reviewed_git_head

        self.assertEqual(provider.calls, 0)
        self.assertEqual(
            migration["completed_atomic_information_ids"],
            list(atomic_ids[:15]),
        )
        self.assertEqual(
            migration["remaining_atomic_information_ids"],
            list(atomic_ids[15:]),
        )
        self.assertEqual(
            service.activate_batch_governance(
                authority_ref=authority_ref,
                authority_manifest_file=authority_file,
            ),
            migration,
        )
        self.assertEqual(provider.calls, 0)
        self.semantic.reviewed_git_head = "7" * 40

        with self.assertRaises(WechatDigestError):
            service.run()
        interrupted_item = service.run_store.status(prepared.run_id)["items"][
            item_id
        ]
        self.assertEqual(
            interrupted_item["governance_receipt"]["phase"], "applying"
        )
        self.assertEqual(
            interrupted_item["governance_receipt"]["next_index"], 1
        )
        self.assertEqual(
            len(
                interrupted_item["governance_receipt"][
                    "applied_effect_fingerprints"
                ]
            ),
            1,
        )
        self.assertEqual(provider.calls, 1)
        self.semantic.reviewed_git_head = campaign_head
        self.assertEqual(
            service.activate_batch_governance(
                authority_ref=authority_ref,
                authority_manifest_file=authority_file,
            ),
            migration,
        )
        self.semantic.reviewed_git_head = "7" * 40

        result = service.run()

        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.batch_sizes, [3])
        self.assertEqual(
            [
                service.information_store.list_revisions(atomic_id)
                for atomic_id in atomic_ids[:15]
            ],
            completed_before,
        )
        final_item = service.run_store.status(prepared.run_id)["items"][item_id]
        self.assertEqual(final_item["atomic_information_ids"], list(atomic_ids))
        self.assertEqual(final_item["governance_receipt"]["phase"], "completed")
        self.assertEqual(
            final_item["governance_migration"]["legacy_governance_receipt"][
                "phase"
            ],
            "started",
        )
        self.semantic.reviewed_git_head = campaign_head
        self.assertEqual(
            service.activate_batch_governance(
                authority_ref=authority_ref,
                authority_manifest_file=authority_file,
            ),
            migration,
        )
        self.semantic.reviewed_git_head = "7" * 40
        replay = service.run()
        self.assertTrue(replay.checkpoint_published)
        self.assertEqual(provider.calls, 1)

    def test_missing_package_still_rejects_unreviewed_runtime_head_zero_call(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        service = self.service(capture)
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        calls_before = self.semantic.provider.calls
        binding = self.semantic.authority_bindings[-1]
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=binding.campaign_created_at,
            lower_cursor=binding.campaign_lower_cursor,
            frozen_global_upper_cursor=binding.frozen_global_upper_cursor,
            capture_provider_version=binding.capture_provider_version,
            semantic_batch_size=binding.semantic_batch_size,
            reviewed_git_head=binding.reviewed_git_head,
        )
        self.semantic.reviewed_git_head = "7" * 40

        with self.assertRaisesRegex(
            WechatDigestError, "frozen Semantic campaign"
        ):
            service.run()

        self.assertEqual(self.semantic.provider.calls, calls_before)

    def test_mid_apply_recovery_reuses_persisted_batch_result(self) -> None:
        service, provider, _receipt = self.interrupted_batch_cursor_one()

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)

    def test_mid_apply_recovery_accepts_persisted_shared_object_effects(
        self,
    ) -> None:
        service, provider, receipt, _object_id = (
            self.interrupted_shared_object_batch_cursor_one()
        )
        self.assertNotEqual(
            receipt["baseline_effect_fingerprints"][1],
            receipt["cursor_effect_fingerprints"][1],
        )

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)

    def test_mid_apply_recovery_rejects_post_cursor_shared_object_drift(
        self,
    ) -> None:
        service, provider, _receipt, object_id = (
            self.interrupted_shared_object_batch_cursor_one()
        )
        with SQLiteWorldModelRepository(service.database) as repository:
            repository.add_role(object_id, "brand")

        with self.assertRaisesRegex(WechatDigestError, "effect/cursor"):
            service.run()

        self.assertEqual(provider.calls, 1)

    def test_pre_cursor_recovery_converges_from_durable_apply_receipt(
        self,
    ) -> None:
        service, provider, _receipt, _object_id = (
            self.interrupted_shared_object_batch_before_cursor()
        )
        business_before = self.governance_business_state(service)

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            self.governance_business_state(service), business_before
        )

    def test_pre_cursor_recovery_does_not_duplicate_human_review(self) -> None:
        service, provider = self.interrupted_human_review_batch_before_cursor()
        business_before = self.governance_business_state(service)

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            self.governance_business_state(service), business_before
        )
        self.assertEqual(len(service.proposal_store.list_unresolved()), 1)

    def test_pre_cursor_recovery_does_not_duplicate_information_enrichment(
        self,
    ) -> None:
        service, provider, atomic_id = (
            self.interrupted_claim_enrichment_batch_before_cursor()
        )
        business_before = self.governance_business_state(service)
        revision_count = len(
            service.information_store.list_revisions(atomic_id)
        )

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            self.governance_business_state(service), business_before
        )
        self.assertEqual(
            len(service.information_store.list_revisions(atomic_id)),
            revision_count,
        )

    def test_pre_cursor_recovery_does_not_duplicate_identity_review(
        self,
    ) -> None:
        service, provider, first_proposal_id = (
            self.interrupted_identity_review_batch_before_cursor()
        )

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            sum(
                proposal.proposal_id == first_proposal_id
                for proposal in service.proposal_store.list_history()
            ),
            1,
        )
        self.assertEqual(len(service.proposal_store.list_unresolved()), 2)

    def test_pre_cursor_recovery_rejects_unproven_extra_drift_before_write(
        self,
    ) -> None:
        service, provider, _receipt, object_id = (
            self.interrupted_shared_object_batch_before_cursor()
        )
        with SQLiteWorldModelRepository(service.database) as repository:
            repository.add_role(object_id, "brand")
        business_before = self.governance_business_state(service)

        with self.assertRaisesRegex(WechatDigestError, "effect/cursor"):
            service.run()

        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            self.governance_business_state(service), business_before
        )

    def test_mid_apply_recovery_rejects_applied_prefix_drift_without_provider(
        self,
    ) -> None:
        service, provider, receipt = self.interrupted_batch_cursor_one()
        self.append_information_drift(
            service, receipt["batch_atomic_information_ids"][0]
        )

        with self.assertRaisesRegex(WechatDigestError, "effect/cursor"):
            service.run()

        self.assertEqual(provider.calls, 1)

    def test_mid_apply_recovery_rejects_unapplied_suffix_drift_without_provider(
        self,
    ) -> None:
        service, provider, receipt = self.interrupted_batch_cursor_one()
        self.append_information_drift(
            service, receipt["batch_atomic_information_ids"][1]
        )

        with self.assertRaisesRegex(WechatDigestError, "effect/cursor"):
            service.run()

        self.assertEqual(provider.calls, 1)

    def test_completed_receipt_readback_rejects_effect_drift_without_provider(
        self,
    ) -> None:
        self.create_object()
        provider = BatchOnceProvider()
        service = FailAfterGovernanceOnceService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "completed")
        self.append_information_drift(
            service, receipt["batch_atomic_information_ids"][0]
        )

        with self.assertRaisesRegex(WechatDigestError, "effect/cursor"):
            service.run()

        self.assertEqual(provider.calls, 1)

    def test_persisted_batch_result_recovers_after_apply_without_provider_call(
        self,
    ) -> None:
        self.create_object()
        provider = SerialSessionProvider()
        capture = SyntheticCaptureProvider([message(1)])
        service = FailAfterProviderOnceService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        self.assertEqual(provider.calls, 1)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(receipt["phase"], "applied")

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertIsNotNone(service.run_store.checkpoint())
        self.assertIsNone(service.run_store.active_run_id())

    def test_governance_timeout_seal_preserves_data_and_continues_next_item(
        self,
    ) -> None:
        service, _capture, provider, run_id, item_id = (
            self.governance_timeout_fixture()
        )

        def protected_snapshot():
            return {
                str(path.relative_to(self.workspace)): path.read_bytes()
                for path in self.workspace.rglob("*")
                if path.is_file()
                and "wechat_digest" not in path.relative_to(self.workspace).parts
            }

        before = protected_snapshot()
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls

        resolution = service.seal_governance_timeout()

        self.assertEqual(resolution["semantic_provider_calls"], 0)
        self.assertEqual(resolution["governance_provider_calls"], 0)
        self.assertEqual(resolution["global_attempt_total"], 176)
        self.assertEqual(resolution["global_unknown"], 0)
        self.assertEqual(resolution["next_global_ordinal"], 177)
        self.assertFalse(resolution["provider_retry_permitted"])
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)
        self.assertEqual(protected_snapshot(), before)

        sealed_status = service.run_store.status(run_id)
        sealed_item = sealed_status["items"][item_id]
        self.assertEqual(sealed_item["state"], "failed_closed")
        self.assertNotIn("semantic_failure", sealed_item)
        self.assertEqual(sealed_item["governance_receipt"]["phase"], "started")
        self.assertEqual(
            set(sealed_item["atomic_information_ids"]),
            {
                revision.atomic_information_id
                for revision in service.information_store.list_atomic_information()
                if revision.origin_source_id == sealed_item["source_id"]
            },
        )
        self.assertEqual(
            sealed_item["governance_failure"],
            {
                "failure_category": "turn_timeout",
                "preserved_but_partially_governed": True,
                "provider_retry_permitted": False,
            },
        )

        status_bytes = (
            service.run_store.runs_root / run_id / "status.json"
        ).read_bytes()
        self.assertEqual(service.seal_governance_timeout(), resolution)
        self.assertEqual(
            (service.run_store.runs_root / run_id / "status.json").read_bytes(),
            status_bytes,
        )
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)

        result = service.run()

        self.assertTrue(result.checkpoint_published)
        self.assertEqual(result.failed_closed, 1)
        self.assertEqual(result.semantic_preserved_but_unabsorbed, 0)
        self.assertEqual(result.governance_preserved_but_incomplete, 1)
        self.assertEqual(self.semantic.provider.calls, semantic_calls + 1)
        self.assertEqual(provider.calls, governance_calls + 1)
        final_item = service.run_store.status(run_id)["items"][item_id]
        self.assertEqual(final_item, sealed_item)
        self.assertIsNone(service.run_store.active_run_id())

    def test_maintenance_continuation_binds_exact_active_state_zero_calls(
        self,
    ) -> None:
        self.create_object()
        messages = [
            message(index, conversation="failed") for index in range(1, 11)
        ]
        messages.extend(
            message(10 + index, conversation=conversation)
            for index, conversation in enumerate(
                ("later-3", "later-5", "later-6", "later-7"), start=1
            )
        )
        capture = SyntheticCaptureProvider(messages)
        provider = TimeoutOnFourthTurnProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        service.install_semantic_authority(
            inventory_authority_file=Path("/private/authority-a.json")
        )
        service.seal_governance_timeout()
        service.prepare_next_semantic()
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        counts = Counter(
            item["state"] for item in status["items"].values()
        )
        self.assertEqual(counts["represented"], 1)
        self.assertEqual(counts["planned"], 3)
        self.semantic.reviewed_git_head = "9" * 40
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/127"
            "#issuecomment-1234567890"
        )
        status_before = (
            service.run_store.runs_root / run_id / "status.json"
        ).read_bytes()
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls
        connector = FailOnSecondCaptureProvider(list(capture.messages))
        service.capture_provider = connector

        continuation = service.install_semantic_maintenance_continuation(
            authority_ref=authority_ref
        )

        self.assertEqual(continuation["activation_total"], 176)
        self.assertEqual(continuation["next_global_ordinal"], 177)
        self.assertEqual(continuation["authority_ref"], authority_ref)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)
        self.assertEqual(connector.calls, [])
        self.assertEqual(
            (service.run_store.runs_root / run_id / "status.json").read_bytes(),
            status_before,
        )
        self.assertEqual(
            service.install_semantic_maintenance_continuation(
                authority_ref=authority_ref
            ),
            continuation,
        )
        with self.assertRaises(RuntimeError):
            service.install_semantic_maintenance_continuation(
                authority_ref=authority_ref.replace("1234567890", "1234567891")
            )

    def test_maintenance_continuation_accepts_exact_failed_state_zero_calls(
        self,
    ) -> None:
        service, provider, run_id = (
            self.maintenance_continuation_failed_state_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/127"
            "#issuecomment-1234567890"
        )
        status_path = service.run_store.runs_root / run_id / "status.json"
        status_before = status_path.read_bytes()
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls

        continuation = service.install_semantic_maintenance_continuation(
            authority_ref=authority_ref
        )

        self.assertEqual(continuation["activation_total"], 176)
        self.assertEqual(continuation["activation_unknown_count"], 0)
        self.assertEqual(continuation["next_global_ordinal"], 177)
        self.assertEqual(continuation["absolute_cap"], 1000)
        self.assertEqual(status_path.read_bytes(), status_before)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)

    def test_gate_c_continuation_accepts_exact_current_state_zero_calls(
        self,
    ) -> None:
        service, provider, run_id = (
            self.maintenance_continuation_failed_state_fixture()
        )
        status = json.loads(json.dumps(service.run_store.status(run_id)))
        status["state"] = "processing"
        status["failure_category"] = None
        represented_id = next(
            item_id
            for item_id, item in status["items"].items()
            if item["state"] == "represented"
        )
        status["items"][represented_id]["state"] = "pending_human"
        additions = {
            "planned": 1,
            "pending_human": 16,
            "processed": 7,
            "local_only": 3,
            "unsupported": 101,
        }
        index = 0
        for state, count in additions.items():
            for _ in range(count):
                index += 1
                status["items"][f"synthetic-gate-c-{index:03d}"] = {
                    "state": state
                }
        service.run_store.update_status(run_id, status)
        self.semantic.reviewed_git_head = "c" * 40
        self.semantic.campaign_binding = SimpleNamespace(
            created_at="2026-08-20T00:00:00Z",
            lower_cursor=(0, "", ""),
            frozen_global_upper_cursor=(999, "m", "c"),
            capture_provider_version="0.5.0",
            semantic_batch_size=40,
            reviewed_git_head="b" * 40,
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/146"
            "#issuecomment-1234567890"
        )
        status_path = service.run_store.runs_root / run_id / "status.json"
        status_before = status_path.read_bytes()
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls
        binding = SimpleNamespace(reviewed_git_head="b" * 40)
        original_capture = service.capture_provider
        assert isinstance(original_capture, SyntheticCaptureProvider)
        connector = FailOnSecondCaptureProvider(list(original_capture.messages))
        service.capture_provider = connector

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_semantic_authority_binding", return_value=binding
        ):
            continuation = service.install_semantic_gate_c_continuation(
                authority_ref=authority_ref
            )

        self.assertEqual(continuation["activation_total"], 220)
        self.assertEqual(continuation["activation_unknown_count"], 0)
        self.assertEqual(continuation["next_global_ordinal"], 221)
        self.assertEqual(status_path.read_bytes(), status_before)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)
        self.assertEqual(connector.calls, [])

    def test_segmented_gate_c_continuation_binds_current_safe_state_zero_calls(
        self,
    ) -> None:
        service, provider, run_id = (
            self.maintenance_continuation_failed_state_fixture()
        )
        status = json.loads(json.dumps(service.run_store.status(run_id)))
        status["state"] = "processing"
        status["failure_category"] = None
        for item in status["items"].values():
            if item["state"] == "failed_closed":
                item["state"] = "pending_human"
        additions = {
            "planned": 14,
            "pending_human": 18,
            "processed": 6,
            "local_only": 3,
            "unsupported": 142,
        }
        index = 0
        for state, count in additions.items():
            for _ in range(count):
                index += 1
                status["items"][f"synthetic-segment-{index:03d}"] = {
                    "state": state
                }
        service.run_store.update_status(run_id, status)
        self.semantic.reviewed_git_head = "d" * 40
        self.semantic.campaign_binding = SimpleNamespace(
            created_at="2026-08-20T00:00:00Z",
            lower_cursor=(0, "", ""),
            frozen_global_upper_cursor=(999, "m", "c"),
            capture_provider_version="0.5.0",
            semantic_batch_size=40,
            reviewed_git_head="c" * 40,
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/148"
            "#issuecomment-1234567890"
        )
        status_path = service.run_store.runs_root / run_id / "status.json"
        status_before = status_path.read_bytes()
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls
        binding = SimpleNamespace(reviewed_git_head="c" * 40)
        original_capture = service.capture_provider
        assert isinstance(original_capture, SyntheticCaptureProvider)
        connector = FailOnSecondCaptureProvider(list(original_capture.messages))
        service.capture_provider = connector

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_semantic_authority_binding", return_value=binding
        ):
            continuation = (
                service.install_semantic_segmented_gate_c_continuation(
                    authority_ref=authority_ref
                )
            )

        self.assertEqual(continuation["activation_total"], 297)
        self.assertEqual(continuation["activation_unknown_count"], 0)
        self.assertEqual(continuation["next_global_ordinal"], 298)
        self.assertEqual(status_path.read_bytes(), status_before)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)
        self.assertEqual(connector.calls, [])

    def test_governance_startup_failure_uses_latest_durable_status(self) -> None:
        self.create_object()
        self.semantic.provider.mode = "four_one"
        provider = StartupFailOnceBatchProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider(
                [
                    message(index, conversation="latest-status")
                    for index in range(1, 6)
                ]
            ),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)

        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        item = next(iter(status["items"].values()))
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["failure_category"], "RuntimeError")
        self.assertEqual(len(item["atomic_information_ids"]), 4)
        self.assertEqual(item["governance_receipt"]["phase"], "started")
        self.assertEqual(item["governance_metrics"]["thread_count"], 0)
        self.assertEqual(item["governance_metrics"]["turn_count"], 0)

    def test_governance_startup_resolution_is_zero_provider_and_restarts_once(
        self,
    ) -> None:
        service, provider, run_id, item_id, atomic_ids = (
            self.governance_startup_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        original_capture = service.capture_provider
        assert isinstance(original_capture, SyntheticCaptureProvider)
        connector = FailOnSecondCaptureProvider(list(original_capture.messages))
        service.capture_provider = connector
        manifest = service.build_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-150-authority.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        before_business = self.governance_business_state(service)
        semantic_calls = self.semantic.provider.calls
        governance_attempts = provider.attempts

        receipt = service.resolve_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )

        self.assertTrue(receipt["provider_retry_permitted"])
        self.assertEqual(receipt["max_retry_attempts"], 1)
        self.assertFalse(receipt["retry_consumed"])
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.attempts, governance_attempts)
        self.assertEqual(connector.calls, [])
        self.assertEqual(self.governance_business_state(service), before_business)
        status = service.run_store.status(run_id)
        self.assertEqual(status["state"], "processing")
        self.assertIsNone(status["failure_category"])
        self.assertEqual(status["items"][item_id]["atomic_information_ids"], atomic_ids)
        status_bytes = (
            service.run_store.runs_root / run_id / "status.json"
        ).read_bytes()
        self.assertEqual(
            service.resolve_governance_startup_failure(
                authority_ref=authority_ref,
                authority_manifest_file=manifest_path,
            ),
            receipt,
        )
        self.assertEqual(
            (service.run_store.runs_root / run_id / "status.json").read_bytes(),
            status_bytes,
        )

        result = service.run(max_terminal_items=3)

        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.attempts, governance_attempts + 1)
        self.assertEqual(provider.successful_calls, 1)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        retry = service.run_store.governance_startup_retry(run_id)
        assert retry is not None
        self.assertEqual(retry["retry_attempt"], 1)

    def test_governance_startup_retry_failure_is_consumed_without_second_call(
        self,
    ) -> None:
        service, provider, _run_id, _item_id, _atomic_ids = (
            self.governance_startup_recovery_fixture(fail_restart=True)
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        manifest = service.build_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-150-fail.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        service.resolve_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )

        with self.assertRaises(WechatDigestError):
            service.run(max_terminal_items=3)
        attempts = provider.attempts
        with self.assertRaisesRegex(WechatDigestError, "机会已消耗"):
            service.run(max_terminal_items=3)
        self.assertEqual(provider.attempts, attempts)

    def test_governance_startup_resolution_adopts_exact_receipt_after_crash(
        self,
    ) -> None:
        service, provider, run_id, _item_id, _atomic_ids = (
            self.governance_startup_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        manifest = service.build_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-150-crash.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        with patch.object(
            service.run_store,
            "update_status",
            side_effect=OSError("synthetic status interruption"),
        ):
            with self.assertRaises(OSError):
                service.resolve_governance_startup_failure(
                    authority_ref=authority_ref,
                    authority_manifest_file=manifest_path,
                )

        durable_receipt = service.run_store.governance_startup_recovery(run_id)
        assert durable_receipt is not None
        observed = service.resolve_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )
        self.assertEqual(observed, durable_receipt)
        self.assertEqual(provider.attempts, 1)

    def test_governance_startup_adopts_semantic_continuation_before_receipt(
        self,
    ) -> None:
        service, provider, run_id, item_id, atomic_ids = (
            self.governance_startup_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        manifest = service.build_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-150-semantic-crash.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        business_before = self.governance_business_state(service)
        provider_attempts = provider.attempts
        semantic_calls = self.semantic.provider.calls

        with patch.object(
            service.run_store,
            "publish_governance_startup_receipt",
            side_effect=OSError("synthetic pre-business-receipt interruption"),
        ):
            with self.assertRaises(OSError):
                service.resolve_governance_startup_failure(
                    authority_ref=authority_ref,
                    authority_manifest_file=manifest_path,
                )

        continuation = dict(
            self.semantic.installed_governance_startup_recovery_continuation
        )
        self.assertIsNone(
            service.run_store.governance_startup_recovery(run_id)
        )
        self.assertEqual(
            service.run_store.status(run_id)["items"][item_id][
                "atomic_information_ids"
            ],
            [],
        )
        receipt = service.resolve_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )

        self.assertEqual(
            self.semantic.installed_governance_startup_recovery_continuation,
            continuation,
        )
        self.assertEqual(provider.attempts, provider_attempts)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(self.governance_business_state(service), business_before)
        self.assertEqual(
            receipt["semantic_continuation_fingerprint"],
            continuation["continuation_fingerprint"],
        )
        status = service.run_store.status(run_id)
        self.assertEqual(status["state"], "processing")
        self.assertIsNone(status["failure_category"])
        self.assertEqual(
            status["items"][item_id]["atomic_information_ids"], atomic_ids
        )

    def test_governance_startup_rejects_non_pristine_evidence_zero_provider(
        self,
    ) -> None:
        service, provider, run_id, item_id, atomic_ids = (
            self.governance_startup_recovery_fixture()
        )
        status = service.run_store.status(run_id)
        status["items"][item_id]["governance_metrics"]["thread_count"] = 1
        service.run_store.update_status(run_id, status)

        with self.assertRaises(WechatDigestError):
            service.build_governance_startup_recovery_manifest(
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
                    "#issuecomment-1234567890"
                )
            )
        self.assertEqual(provider.attempts, 1)

        status["items"][item_id]["governance_metrics"]["thread_count"] = 0
        service.run_store.update_status(run_id, status)
        self.append_information_drift(service, atomic_ids[0])
        with self.assertRaises(WechatDigestError):
            service.build_governance_startup_recovery_manifest(
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
                    "#issuecomment-1234567890"
                )
            )
        self.assertEqual(provider.attempts, 1)

    def test_governance_startup_rejects_unknown_or_later_semantic_attempt(
        self,
    ) -> None:
        service, provider, _run_id, _item_id, _atomic_ids = (
            self.governance_startup_recovery_fixture()
        )
        self.semantic.global_unknown = 1
        with self.assertRaises(WechatDigestError):
            service.build_governance_startup_recovery_manifest(
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
                    "#issuecomment-1234567890"
                )
            )
        self.semantic.global_unknown = 0
        self.semantic.global_attempt_total = 299
        with self.assertRaises(WechatDigestError):
            service.build_governance_startup_recovery_manifest(
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
                    "#issuecomment-1234567890"
                )
            )
        self.assertEqual(provider.attempts, 1)

    def test_multi_governance_startup_recovery_preserves_history_and_resumes_once(
        self,
    ) -> None:
        (
            service,
            provider,
            run_id,
            _historical_item_id,
            current_item_id,
            current_atomic_ids,
        ) = self.multi_governance_startup_recovery_fixture()
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567890"
        )
        run_dir = service.run_store.runs_root / run_id
        historical_recovery_path = run_dir / "governance-startup-recovery.json"
        historical_retry_path = run_dir / "governance-startup-retry.json"
        historical_recovery_bytes = historical_recovery_path.read_bytes()
        historical_retry_bytes = historical_retry_path.read_bytes()
        before_business = self.governance_business_state(service)
        provider_attempts = provider.attempts
        semantic_calls = self.semantic.provider.calls
        original_capture = service.capture_provider
        assert isinstance(original_capture, SyntheticCaptureProvider)
        capture = FailOnSecondCaptureProvider(list(original_capture.messages))
        service.capture_provider = capture

        manifest = service.build_multi_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        self.assertEqual(len(capture.calls), 0)
        manifest_path = Path(self.temporary.name) / "issue-168-authority.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        receipt = service.resolve_multi_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )

        self.assertEqual(historical_recovery_path.read_bytes(), historical_recovery_bytes)
        self.assertEqual(historical_retry_path.read_bytes(), historical_retry_bytes)
        self.assertEqual(len(service.run_store.governance_startup_recoveries(run_id)), 2)
        self.assertEqual(len(service.run_store.governance_startup_retries(run_id)), 1)
        self.assertEqual(provider.attempts, provider_attempts)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(self.governance_business_state(service), before_business)
        self.assertEqual(service.run_store.status(run_id)["state"], "processing")
        self.assertEqual(
            service.resolve_multi_governance_startup_failure(
                authority_ref=authority_ref,
                authority_manifest_file=manifest_path,
            ),
            receipt,
        )
        self.assertEqual(provider.attempts, provider_attempts)
        self.assertEqual(len(capture.calls), 0)

        service.capture_provider = original_capture
        service.run(max_terminal_items=1)

        self.assertEqual(provider.attempts, provider_attempts + 1)
        self.assertEqual(provider.successful_calls, 2)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(len(service.run_store.governance_startup_recoveries(run_id)), 2)
        self.assertEqual(len(service.run_store.governance_startup_retries(run_id)), 2)
        status = service.run_store.status(run_id)
        self.assertEqual(status["items"][current_item_id]["state"], "processed")
        self.assertEqual(
            status["items"][current_item_id]["atomic_information_ids"],
            current_atomic_ids,
        )

    def test_multi_governance_startup_recovery_adopts_after_status_crash(
        self,
    ) -> None:
        service, provider, run_id, *_rest = (
            self.multi_governance_startup_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567890"
        )
        manifest = service.build_multi_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-168-crash.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        with patch.object(
            service.run_store,
            "update_status",
            side_effect=OSError("synthetic status interruption"),
        ):
            with self.assertRaises(OSError):
                service.resolve_multi_governance_startup_failure(
                    authority_ref=authority_ref,
                    authority_manifest_file=manifest_path,
                )
        durable = service.run_store.governance_startup_recoveries(run_id)
        self.assertEqual(len(durable), 2)
        attempts = provider.attempts

        observed = service.resolve_multi_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )

        self.assertEqual(observed, durable[-1])
        self.assertEqual(provider.attempts, attempts)
        self.assertEqual(service.run_store.status(run_id)["state"], "processing")

    def test_multi_governance_startup_rejects_corrupt_history_zero_provider(
        self,
    ) -> None:
        service, provider, run_id, *_rest = (
            self.multi_governance_startup_recovery_fixture()
        )
        directory = (
            service.run_store.runs_root
            / run_id
            / "governance-startup-recoveries"
        )
        directory.mkdir(mode=0o700)
        corrupt = directory / ("0" * 64 + ".json")
        corrupt.write_text("{", encoding="utf-8")
        os.chmod(corrupt, 0o600)
        attempts = provider.attempts

        with self.assertRaisesRegex(WechatDigestError, "receipt 损坏"):
            service.build_multi_governance_startup_recovery_manifest(
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
                    "#issuecomment-1234567890"
                )
            )

        self.assertEqual(provider.attempts, attempts)

    def test_multi_governance_startup_rejects_duplicate_current_match(
        self,
    ) -> None:
        service, provider, run_id, *_rest = (
            self.multi_governance_startup_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567890"
        )
        manifest = service.build_multi_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-168-duplicate.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        current = service.resolve_multi_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )
        duplicate = dict(current)
        duplicate["authority_ref"] = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567891"
        )
        duplicate.pop("receipt_fingerprint")
        duplicate["receipt_fingerprint"] = "sha256:" + hashlib.sha256(
            json.dumps(
                duplicate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        directory = (
            service.run_store.runs_root
            / run_id
            / "governance-startup-recoveries"
        )
        duplicate_path = directory / (
            duplicate["receipt_fingerprint"].removeprefix("sha256:")
            + ".json"
        )
        duplicate_path.write_text(
            json.dumps(
                duplicate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(duplicate_path, 0o600)
        attempts = provider.attempts

        with self.assertRaisesRegex(WechatDigestError, "重复匹配"):
            service.run(max_terminal_items=1)

        self.assertEqual(provider.attempts, attempts)

    def test_multi_governance_startup_manifest_drift_rejects_before_write(
        self,
    ) -> None:
        service, provider, run_id, *_rest = (
            self.multi_governance_startup_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567890"
        )
        manifest = service.build_multi_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        attempts = provider.attempts
        semantic_calls = self.semantic.provider.calls

        def set_manifest_fingerprint(value):
            value.pop("manifest_fingerprint", None)
            value["manifest_fingerprint"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

        cases = []
        changed_decision = json.loads(json.dumps(manifest))
        changed_decision["authority_ref"] = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567891"
        )
        cases.append(("decision", changed_decision, authority_ref))
        changed_item = json.loads(json.dumps(manifest))
        changed_item["recovery_binding"]["item_id"] += "-drift"
        cases.append(("item", changed_item, authority_ref))
        changed_order = json.loads(json.dumps(manifest))
        changed_order["recovery_binding"]["ordered_atomic_information_ids"].reverse()
        cases.append(("atomic-order", changed_order, authority_ref))
        changed_started = json.loads(json.dumps(manifest))
        changed_started["recovery_binding"][
            "governance_started_receipt_fingerprint"
        ] = "sha256:" + "0" * 64
        cases.append(("started", changed_started, authority_ref))
        changed_effect = json.loads(json.dumps(manifest))
        changed_effect["atomic_effect_bindings"][0]["effect_fingerprint"] = (
            "sha256:" + "0" * 64
        )
        cases.append(("effect", changed_effect, authority_ref))
        changed_head = json.loads(json.dumps(manifest))
        changed_head["reviewed_git_head"] = "c" * 40
        cases.append(("head", changed_head, authority_ref))
        changed_ledger = json.loads(json.dumps(manifest))
        changed_ledger["semantic_summary"]["global_attempt_total"] = 303
        cases.append(("ledger", changed_ledger, authority_ref))

        for index, (label, changed, changed_ref) in enumerate(cases):
            with self.subTest(label=label):
                set_manifest_fingerprint(changed)
                path = Path(self.temporary.name) / f"issue-168-{index}.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                os.chmod(path, 0o600)
                with self.assertRaises(WechatDigestError):
                    service.resolve_multi_governance_startup_failure(
                        authority_ref=changed_ref,
                        authority_manifest_file=path,
                    )
                self.assertEqual(provider.attempts, attempts)
                self.assertEqual(self.semantic.provider.calls, semantic_calls)
                self.assertIsNone(
                    self.semantic.installed_multi_governance_startup_recovery_continuation
                )
                self.assertEqual(
                    len(service.run_store.governance_startup_recoveries(run_id)),
                    1,
                )

    def test_multi_governance_startup_durable_drift_rejects_before_write(
        self,
    ) -> None:
        def change_plan(case, service, run_id, _item_id, _atomic_ids):
            path = service.run_store.runs_root / run_id / "plan.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["capture_fingerprint"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")

        def change_plan_receipt(case, service, run_id, _item_id, _atomic_ids):
            path = service.run_store.runs_root / run_id / "run-plan-receipt.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["plan_fingerprint"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")

        def change_status(case, service, run_id, _item_id, _atomic_ids):
            status = service.run_store.status(run_id)
            status["updated_at"] = "2099-01-01T00:00:00Z"
            service.run_store.update_status(run_id, status)

        def change_checkpoint(case, service, run_id, _item_id, _atomic_ids):
            plan = service.run_store.plan(run_id)
            service.run_store.checkpoint_path.write_text(
                json.dumps(
                    {
                        "schema_version": "wechat-digest-checkpoint/1.0",
                        "cursor": plan["upper_bound"],
                        "published_at": "2099-01-01T00:00:00Z",
                        "run_id": run_id,
                    }
                ),
                encoding="utf-8",
            )

        def change_source(case, service, _run_id, item_id, _atomic_ids):
            item = service.run_store.status(
                service.run_store.active_run_id()
            )["items"][item_id]
            source = service.source_repository.get(item["source_id"])
            path = service.source_repository.managed_root / source.managed_locator
            path.write_bytes(path.read_bytes() + b"drift")

        def change_representation(case, service, run_id, item_id, _atomic_ids):
            item = service.run_store.status(run_id)["items"][item_id]
            representation = service.representation_repository.get(
                item["representation_id"]
            )
            directory = (
                service.representation_repository.representation_root
                / representation.source_id
                / representation.representation_id
            )
            (directory / "unexpected-drift.txt").write_text(
                "drift", encoding="utf-8"
            )

        def change_package(case, service, run_id, item_id, _atomic_ids):
            item = service.run_store.status(run_id)["items"][item_id]
            package = (
                service.workspace
                / "02_processing"
                / "information"
                / item["representation_id"]
            )
            (package / "unexpected-drift.txt").write_text(
                "drift", encoding="utf-8"
            )

        def change_atomic(case, service, _run_id, _item_id, atomic_ids):
            case.append_information_drift(service, atomic_ids[0])

        def change_business(case, service, _run_id, _item_id, _atomic_ids):
            with SQLiteWorldModelRepository(service.database) as repository:
                repository.create_object("Synthetic durable drift")

        def change_history(case, service, run_id, _item_id, _atomic_ids):
            legacy = service.run_store.governance_startup_recovery(run_id)
            assert legacy is not None
            directory = (
                service.run_store.runs_root
                / run_id
                / "governance-startup-recoveries"
            )
            directory.mkdir(mode=0o700)
            path = directory / (
                legacy["receipt_fingerprint"].removeprefix("sha256:") + ".json"
            )
            path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)

        def change_ledger(case, _service, _run_id, _item_id, _atomic_ids):
            case.semantic.global_attempt_total = 303

        cases = {
            "plan": change_plan,
            "plan-receipt": change_plan_receipt,
            "status": change_status,
            "checkpoint": change_checkpoint,
            "source": change_source,
            "representation": change_representation,
            "package": change_package,
            "atomic": change_atomic,
            "business": change_business,
            "history": change_history,
            "ledger": change_ledger,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                case = type(self)(methodName=self._testMethodName)
                case.setUp()
                try:
                    (
                        service,
                        provider,
                        run_id,
                        _historical_item_id,
                        current_item_id,
                        atomic_ids,
                    ) = case.multi_governance_startup_recovery_fixture()
                    authority_ref = (
                        "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
                        "#issuecomment-1234567890"
                    )
                    manifest = (
                        service.build_multi_governance_startup_recovery_manifest(
                            authority_ref=authority_ref
                        )
                    )
                    path = Path(case.temporary.name) / f"{label}-authority.json"
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    os.chmod(path, 0o600)
                    attempts = provider.attempts
                    semantic_calls = case.semantic.provider.calls
                    mutate(
                        case,
                        service,
                        run_id,
                        current_item_id,
                        atomic_ids,
                    )
                    recovery_count = len(
                        service.run_store.governance_startup_recoveries(run_id)
                    )

                    with self.assertRaises(
                        (
                            WechatDigestError,
                            RepresentationInformationError,
                            RepresentationError,
                        )
                    ):
                        service.resolve_multi_governance_startup_failure(
                            authority_ref=authority_ref,
                            authority_manifest_file=path,
                        )

                    self.assertEqual(provider.attempts, attempts)
                    self.assertEqual(case.semantic.provider.calls, semantic_calls)
                    self.assertIsNone(
                        case.semantic.installed_multi_governance_startup_recovery_continuation
                    )
                    self.assertEqual(
                        len(service.run_store.governance_startup_recoveries(run_id)),
                        recovery_count,
                    )
                finally:
                    case.tearDown()

    def test_multi_governance_startup_retry_failure_is_consumed(
        self,
    ) -> None:
        service, provider, _run_id, *_rest = (
            self.multi_governance_startup_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567890"
        )
        manifest = service.build_multi_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-168-fail.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        service.resolve_multi_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )
        provider.fail_restart = True

        with self.assertRaises(WechatDigestError):
            service.run(max_terminal_items=1)
        attempts = provider.attempts
        with self.assertRaisesRegex(WechatDigestError, "机会已消耗"):
            service.run(max_terminal_items=1)
        self.assertEqual(provider.attempts, attempts)

    def test_multi_governance_startup_post_result_cursor_recovery_is_zero_provider(
        self,
    ) -> None:
        (
            service,
            provider,
            _run_id,
            *_rest,
        ) = self.multi_governance_startup_recovery_fixture(
            service_type=FailAfterPersistedBatchProgressOnceService
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567890"
        )
        manifest = service.build_multi_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-168-cursor.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        service.resolve_multi_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )

        with self.assertRaisesRegex(WechatDigestError, "未安全完成"):
            service.run(max_terminal_items=1)
        attempts = provider.attempts

        result = service.run(max_terminal_items=1)

        self.assertEqual(provider.attempts, attempts)
        self.assertTrue(result.replayed)

    def test_failed_closed_recovery_is_zero_provider_and_idempotent(
        self,
    ) -> None:
        service, provider, run_id, current_item_id, previous_item_id = (
            self.failed_closed_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
            "#issuecomment-1234567890"
        )
        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            manifest = service.build_failed_closed_recovery_manifest(
                authority_ref=authority_ref
            )
        self.assertEqual(
            manifest["historical_failed_closed_summary"]["total"], 5
        )
        self.assertEqual(
            Counter(
                item["state"]
                for item in manifest["recovery_binding"][
                    "current_failed_status"
                ]["items"].values()
            ),
            {
                "local_only": 3,
                "pending_human": 20,
                "planned": 16,
                "processed": 7,
                "represented": 1,
                "unsupported": 142,
            },
        )
        manifest_path = Path(self.temporary.name) / "issue-154-authority.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        status_before = service.run_store.status(run_id)
        business_before = self.governance_business_state(service)
        semantic_calls = self.semantic.provider.calls
        governance_attempts = provider.attempts

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            receipt = service.resolve_failed_closed_continuation(
                authority_ref=authority_ref,
                authority_manifest_file=manifest_path,
            )

        self.assertEqual(receipt["provider_calls"], 0)
        self.assertEqual(
            receipt["historical_failed_closed_summary"],
            manifest["historical_failed_closed_summary"],
        )
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.attempts, governance_attempts)
        self.assertEqual(self.governance_business_state(service), business_before)
        status = service.run_store.status(run_id)
        expected = json.loads(json.dumps(status_before))
        expected["state"] = "processing"
        expected["failure_category"] = None
        self.assertEqual(status, expected)
        self.assertEqual(status["items"][current_item_id]["state"], "represented")
        self.assertEqual(status["items"][previous_item_id]["state"], "processed")
        receipt_path = (
            service.run_store.runs_root / run_id / "failed-closed-recovery.json"
        )
        receipt_bytes = receipt_path.read_bytes()
        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            self.assertEqual(
                service.resolve_failed_closed_continuation(
                    authority_ref=authority_ref,
                    authority_manifest_file=manifest_path,
                ),
                receipt,
            )
        self.assertEqual(receipt_path.read_bytes(), receipt_bytes)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.attempts, governance_attempts)
        recovered_status = service.run_store.status(run_id)
        self.semantic.installed_failed_closed_recovery_continuation = None
        with self.assertRaises(WechatDigestError):
            service.resolve_failed_closed_continuation(
                authority_ref=authority_ref,
                authority_manifest_file=manifest_path,
            )
        self.assertEqual(service.run_store.status(run_id), recovered_status)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.attempts, governance_attempts)

    def test_failed_closed_recovery_adopts_continuation_after_crash(
        self,
    ) -> None:
        service, provider, run_id, _current, _previous = (
            self.failed_closed_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
            "#issuecomment-1234567890"
        )
        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            manifest = service.build_failed_closed_recovery_manifest(
                authority_ref=authority_ref
            )
        manifest_path = Path(self.temporary.name) / "issue-154-crash.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        provider_attempts = provider.attempts
        original = service.run_store.publish_governance_startup_receipt
        failed_once = False

        def fail_before_receipt(*args, **kwargs):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise OSError("synthetic pre-recovery-receipt interruption")
            return original(*args, **kwargs)

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ), patch.object(
            service.run_store,
            "publish_governance_startup_receipt",
            side_effect=fail_before_receipt,
        ):
            with self.assertRaises(OSError):
                service.resolve_failed_closed_continuation(
                    authority_ref=authority_ref,
                    authority_manifest_file=manifest_path,
                )
        continuation = dict(
            self.semantic.installed_failed_closed_recovery_continuation
        )
        self.assertIsNone(service.run_store.failed_closed_recovery(run_id))

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            receipt = service.resolve_failed_closed_continuation(
                authority_ref=authority_ref,
                authority_manifest_file=manifest_path,
            )

        self.assertEqual(
            self.semantic.installed_failed_closed_recovery_continuation,
            continuation,
        )
        self.assertEqual(receipt["provider_calls"], 0)
        self.assertEqual(provider.attempts, provider_attempts)
        self.assertEqual(service.run_store.status(run_id)["state"], "processing")

    def test_failed_closed_recovery_rejects_status_drift_zero_provider(
        self,
    ) -> None:
        service, provider, run_id, current_item_id, _previous = (
            self.failed_closed_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
            "#issuecomment-1234567890"
        )
        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            manifest = service.build_failed_closed_recovery_manifest(
                authority_ref=authority_ref
            )
        manifest_path = Path(self.temporary.name) / "issue-154-drift.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        status = service.run_store.status(run_id)
        status["items"][current_item_id]["atomic_information_ids"] = [
            "ai_drift"
        ]
        service.run_store.update_status(run_id, status)
        attempts = provider.attempts

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            with self.assertRaises(WechatDigestError):
                service.resolve_failed_closed_continuation(
                    authority_ref=authority_ref,
                    authority_manifest_file=manifest_path,
                )

        self.assertEqual(provider.attempts, attempts)
        self.assertEqual(service.run_store.status(run_id), status)

    def test_failed_closed_recovery_rejects_historical_items_in_active(
        self,
    ) -> None:
        service, provider, run_id, _current, _previous = (
            self.failed_closed_recovery_fixture()
        )
        status = service.run_store.status(run_id)
        unsupported = [
            item
            for item in status["items"].values()
            if item["state"] == "unsupported"
        ]
        for item in unsupported[:5]:
            item["state"] = "failed_closed"
        service.run_store.update_status(run_id, status)
        status_before = service.run_store.status(run_id)
        semantic_calls = self.semantic.provider.calls
        governance_attempts = provider.attempts

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            with self.assertRaisesRegex(
                WechatDigestError, "item 状态边界不匹配"
            ):
                service.build_failed_closed_recovery_manifest(
                    authority_ref=(
                        "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
                        "#issuecomment-1234567890"
                    )
                )

        self.assertEqual(service.run_store.status(run_id), status_before)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.attempts, governance_attempts)

    def test_failed_closed_recovery_rejects_historical_summary_drift(
        self,
    ) -> None:
        service, provider, run_id, _current, _previous = (
            self.failed_closed_recovery_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
            "#issuecomment-1234567890"
        )
        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            manifest = service.build_failed_closed_recovery_manifest(
                authority_ref=authority_ref
            )
        manifest_path = Path(self.temporary.name) / "issue-154-history.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        historical_run_id = self.issue154_historical_run_ids[0]
        historical_status = service.run_store.status(historical_run_id)
        historical_item = next(iter(historical_status["items"].values()))
        historical_item["semantic_failure"]["synthetic"] = "drift"
        service.run_store.update_status(
            historical_run_id, historical_status
        )
        active_status = service.run_store.status(run_id)
        semantic_calls = self.semantic.provider.calls
        governance_attempts = provider.attempts

        with patch.object(service, "_verify_plan_and_status"), patch.object(
            service, "_verify_failed_closed_item"
        ):
            drifted_summary = service._historical_failed_closed_summary(
                active_run_id=run_id
            )
            self.assertEqual(drifted_summary["total"], 5)
            self.assertNotEqual(
                drifted_summary["inventory_fingerprint"],
                manifest["historical_failed_closed_summary"][
                    "inventory_fingerprint"
                ],
            )
            with self.assertRaisesRegex(
                WechatDigestError, "manifest 与现场不匹配"
            ):
                service.resolve_failed_closed_continuation(
                    authority_ref=authority_ref,
                    authority_manifest_file=manifest_path,
                )

        self.assertEqual(service.run_store.status(run_id), active_status)
        self.assertIsNone(service.run_store.failed_closed_recovery(run_id))
        self.assertIsNone(
            self.semantic.installed_failed_closed_recovery_continuation
        )
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.attempts, governance_attempts)

    def test_governance_startup_persisted_batch_recovers_without_provider(
        self,
    ) -> None:
        service, provider, _run_id, _item_id, _atomic_ids = (
            self.governance_startup_recovery_fixture(
                service_type=FailAfterPersistedBatchOnceService
            )
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        manifest = service.build_governance_startup_recovery_manifest(
            authority_ref=authority_ref
        )
        manifest_path = Path(self.temporary.name) / "issue-150-batch.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        service.resolve_governance_startup_failure(
            authority_ref=authority_ref,
            authority_manifest_file=manifest_path,
        )

        with self.assertRaises(WechatDigestError):
            service.run(max_terminal_items=3)
        attempts = provider.attempts
        result = service.run(max_terminal_items=3)

        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.attempts, attempts)
        self.assertEqual(provider.successful_calls, 1)

    def test_maintenance_continuation_rejects_other_run_state_pairs(
        self,
    ) -> None:
        service, provider, run_id = (
            self.maintenance_continuation_failed_state_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/127"
            "#issuecomment-1234567890"
        )
        valid_status = service.run_store.status(run_id)
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls

        for state, failure_category in (
            ("processing", "WechatDigestError"),
            ("failed", None),
            ("failed", "RuntimeError"),
            ("completed", None),
        ):
            invalid_status = {
                **valid_status,
                "state": state,
                "failure_category": failure_category,
            }
            service.run_store.update_status(run_id, invalid_status)
            status_path = service.run_store.runs_root / run_id / "status.json"
            status_before = status_path.read_bytes()

            with self.assertRaisesRegex(
                WechatDigestError, "active run 状态不匹配"
            ):
                service.install_semantic_maintenance_continuation(
                    authority_ref=authority_ref
                )

            self.assertEqual(status_path.read_bytes(), status_before)
            self.assertIsNone(
                self.semantic.installed_maintenance_continuation
            )
            self.assertEqual(self.semantic.provider.calls, semantic_calls)
            self.assertEqual(provider.calls, governance_calls)

        service.run_store.update_status(run_id, valid_status)

    def test_maintenance_continuation_failed_state_requires_both_failures(
        self,
    ) -> None:
        service, provider, run_id = (
            self.maintenance_continuation_failed_state_fixture()
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/127"
            "#issuecomment-1234567890"
        )
        valid_status = service.run_store.status(run_id)
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls

        for failure_key in ("semantic_failure", "governance_failure"):
            invalid_status = json.loads(json.dumps(valid_status))
            item = next(
                candidate
                for candidate in invalid_status["items"].values()
                if candidate.get(failure_key) is not None
            )
            item["state"] = "unsupported"
            item[failure_key] = None
            service.run_store.update_status(run_id, invalid_status)
            status_path = service.run_store.runs_root / run_id / "status.json"
            status_before = status_path.read_bytes()

            with self.assertRaisesRegex(
                WechatDigestError, "active item 边界不匹配"
            ):
                service.install_semantic_maintenance_continuation(
                    authority_ref=authority_ref
                )

            self.assertEqual(status_path.read_bytes(), status_before)
            self.assertIsNone(
                self.semantic.installed_maintenance_continuation
            )
            self.assertEqual(self.semantic.provider.calls, semantic_calls)
            self.assertEqual(provider.calls, governance_calls)

        service.run_store.update_status(run_id, valid_status)

    def test_governance_timeout_seal_recovers_after_status_write_interruption(
        self,
    ) -> None:
        service, capture, provider, run_id, item_id = (
            self.governance_timeout_fixture(include_next=False)
        )

        class InterruptAfterWriteStore(WechatDigestRunStore):
            interrupt_once = True

            def update_status(self, target_run_id, status):
                super().update_status(target_run_id, status)
                item = status.get("items", {}).get(item_id, {})
                if self.interrupt_once and item.get("governance_failure"):
                    self.interrupt_once = False
                    raise OSError("synthetic post-write interruption")

        interrupted_store = InterruptAfterWriteStore(service.run_store.root)
        recovering = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
            run_store=interrupted_store,
        )
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls

        with self.assertRaisesRegex(OSError, "post-write interruption"):
            recovering.seal_governance_timeout()

        written = interrupted_store.status(run_id)["items"][item_id]
        self.assertEqual(written["state"], "failed_closed")
        resolution = recovering.seal_governance_timeout()
        self.assertEqual(resolution["governance_preserved_but_incomplete"], 1)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)

    def test_governance_timeout_seal_allows_attempt_total_at_cap(self) -> None:
        service, _capture, provider, _run_id, _item_id = (
            self.governance_timeout_fixture(include_next=False)
        )
        self.semantic.global_attempt_total = 1000
        self.semantic.absolute_cap = 1000
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls

        resolution = service.seal_governance_timeout()

        self.assertEqual(resolution["global_attempt_total"], 1000)
        self.assertEqual(resolution["absolute_cap"], 1000)
        self.assertEqual(resolution["next_global_ordinal"], 1001)
        self.assertEqual(resolution["semantic_provider_calls"], 0)
        self.assertEqual(resolution["governance_provider_calls"], 0)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)

    def test_governance_timeout_seal_rejects_missing_semantic_package(self) -> None:
        def mutate(service, run_id, item_id):
            item = service.run_store.status(run_id)["items"][item_id]
            shutil.rmtree(
                self.workspace
                / "02_processing"
                / "information"
                / item["representation_id"]
            )

        self.assert_governance_timeout_seal_rejected(mutate)

    def test_governance_timeout_seal_rejects_atomic_receipt_drift(self) -> None:
        def mutate(service, run_id, item_id):
            status = service.run_store.status(run_id)
            status["items"][item_id]["atomic_information_ids"] = [
                "atomic_" + "f" * 32
            ]
            service.run_store.update_status(run_id, status)

        self.assert_governance_timeout_seal_rejected(mutate)

    def test_governance_timeout_seal_rejects_completed_receipt(self) -> None:
        def mutate(service, run_id, item_id):
            status = service.run_store.status(run_id)
            receipt = status["items"][item_id]["governance_receipt"]
            status["items"][item_id]["governance_receipt"] = {
                **receipt,
                "phase": "completed",
                "pending_human": False,
                "context_object_ids": [],
            }
            service.run_store.update_status(run_id, status)

        self.assert_governance_timeout_seal_rejected(mutate)

    def test_governance_timeout_seal_rejects_missing_timeout_evidence(self) -> None:
        def mutate(service, run_id, item_id):
            status = service.run_store.status(run_id)
            metrics = status["items"][item_id]["governance_metrics"]
            metrics["timeout_count"] = 0
            metrics["failure_count"] = 0
            metrics["failure_categories"] = {}
            service.run_store.update_status(run_id, status)

        self.assert_governance_timeout_seal_rejected(mutate)

    def test_governance_timeout_seal_rejects_global_unknown(self) -> None:
        def mutate(_service, _run_id, _item_id):
            self.semantic.global_unknown = 1

        self.assert_governance_timeout_seal_rejected(mutate)

    def test_governance_timeout_seal_rejects_later_semantic_attempt(self) -> None:
        def mutate(_service, _run_id, _item_id):
            self.semantic.global_attempt_total += 1
            self.semantic.latest_representation_id = "repr_" + "f" * 64

        self.assert_governance_timeout_seal_rejected(mutate)

    def test_governance_timeout_seal_rejects_competing_digest_lock(self) -> None:
        service, _capture, provider, run_id, _item_id = (
            self.governance_timeout_fixture(include_next=False)
        )
        status_path = service.run_store.runs_root / run_id / "status.json"
        status_before = status_path.read_bytes()
        semantic_calls = self.semantic.provider.calls
        governance_calls = provider.calls
        with service.run_store.lock():
            with self.assertRaisesRegex(WechatDigestError, "正在运行"):
                service.seal_governance_timeout()
        self.assertEqual(status_path.read_bytes(), status_before)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)

    def test_session_cleanup_failure_prevents_checkpoint_and_next_run(self) -> None:
        self.create_object()

        class CleanupFailureProvider(SerialSessionProvider):
            @contextmanager
            def session(self):
                self.session_entries += 1
                try:
                    yield self
                finally:
                    self.session_exits += 1
                    raise RuntimeError("synthetic session cleanup failure")

        provider = CleanupFailureProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)

        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        self.assertEqual(status["failure_category"], "governance_session_cleanup")
        self.assertFalse(status["checkpoint_published"])
        self.assertIsNone(service.run_store.checkpoint())
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.session_entries, 1)
        self.assertEqual(provider.session_exits, 1)

        with self.assertRaisesRegex(WechatDigestError, "cleanup 失败"):
            service.run()

        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.session_entries, 1)
        self.assertEqual(service.run_store.active_run_id(), run_id)

        semantic_calls = self.semantic.provider.calls
        with self.assertRaisesRegex(WechatDigestError, "cleanup 失败"):
            service.prepare_next_semantic()

        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.session_entries, 1)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)

    def test_segment_cleanup_failure_blocks_next_run_without_segment_receipt(
        self,
    ) -> None:
        self.create_object()

        class CleanupFailureProvider(SerialSessionProvider):
            @contextmanager
            def session(self):
                self.session_entries += 1
                try:
                    yield self
                finally:
                    self.session_exits += 1
                    raise RuntimeError("synthetic segment cleanup failure")

        provider = CleanupFailureProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1), message(2)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True, max_terminal_items=1)

        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        self.assertEqual(status["failure_category"], "governance_session_cleanup")
        self.assertEqual(
            sum(
                isinstance(item, dict)
                and item.get("state") in TERMINAL_ITEM_STATES
                for item in status["items"].values()
            ),
            1,
        )
        self.assertFalse(
            (service.run_store.runs_root / run_id / "segments").exists()
        )
        calls_after_failure = provider.calls
        self.assertGreater(calls_after_failure, 0)

        with self.assertRaisesRegex(WechatDigestError, "cleanup 失败"):
            service.run(max_terminal_items=1)

        self.assertEqual(provider.calls, calls_after_failure)
        self.assertEqual(provider.session_entries, 1)

    def test_sigterm_during_sdk_exit_force_kills_and_blocks_recovery(self) -> None:
        self.create_object()
        operation = {
            "kind": "no_structural_change",
            "target_object_id": None,
            "secondary_object_id": None,
            "name": None,
            "role": None,
            "relation": None,
            "relationship_id": None,
            "lifecycle_state": None,
            "start_at": None,
            "actual_end_at": None,
            "target_end_at": None,
            "completion_condition": None,
        }
        handlers = {
            signal.SIGTERM: signal.SIG_DFL,
            signal.SIGALRM: signal.SIG_DFL,
        }

        def getsignal(signum):
            return handlers[signum]

        def install_signal(signum, handler):
            previous = handlers[signum]
            handlers[signum] = handler
            return previous

        class Process:
            def __init__(self):
                self.killed = False

            def kill(self):
                self.killed = True

        class Turn:
            def __init__(self, response):
                self.response = response

            def run(self):
                return SimpleNamespace(final_response=self.response)

            def interrupt(self):
                raise AssertionError("successful turn must not interrupt")

        class Thread:
            def turn(self, prompt, **_kwargs):
                payload = json.loads(prompt.split("Input:\n", 1)[1])
                atomic_ids = [
                    item["atomic_information"]["atomic_information_id"]
                    for item in payload["items"]
                ]
                response = json.dumps(
                    {
                        "results": [
                            {
                                "atomic_information_id": atomic_id,
                                "operations": [operation],
                                "rationale": "Synthetic cleanup termination.",
                                "evidence_sufficient": True,
                                "conflict": False,
                                "ambiguous": False,
                                "claim": None,
                            }
                            for atomic_id in atomic_ids
                        ]
                    }
                )
                return Turn(response)

        class Codex:
            instance = None

            def __init__(self):
                self._client = SimpleNamespace(_proc=Process())
                self.__class__.instance = self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                handler = handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)

            def thread_start(self, **_kwargs):
                return Thread()

        provider = CodexAtomicInformationInterpretationProvider(
            sdk_loader=lambda: (Codex, "deny-all", "read-only")
        )
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([message(1)]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with (
            patch(
                "archeos.digestion.providers.signal.getsignal",
                side_effect=getsignal,
            ),
            patch(
                "archeos.digestion.providers.signal.signal",
                side_effect=install_signal,
            ),
            self.assertRaises(SystemExit),
        ):
            service.run(all_history=True)

        assert Codex.instance is not None
        self.assertTrue(Codex.instance._client._proc.killed)
        self.assertIs(handlers[signal.SIGTERM], signal.SIG_DFL)
        self.assertEqual(
            provider.metrics_since(0)["failure_categories"], {"cleanup": 1}
        )
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        self.assertEqual(status["failure_category"], "governance_session_cleanup")
        self.assertFalse(status["checkpoint_published"])
        self.assertIsNone(service.run_store.checkpoint())

    def test_from_now_bootstrap_does_not_read_existing_message_bodies(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        result = self.service(capture).run(from_now=True)
        self.assertEqual(result.new_messages, 0)
        self.assertEqual(self.source_count(), 0)
        self.assertTrue(capture.calls[0][2])

    def test_all_history_and_same_timestamp_are_strictly_ordered(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider(
            [message(2, timestamp=1_700_000_000), message(1, timestamp=1_700_000_000)]
        )
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.new_messages, 2)
        payload = next(
            value
            for value in (
                json.loads((path / "plan.json").read_text(encoding="utf-8"))
                for path in (
                    self.workspace / "02_processing" / "wechat_digest" / "runs"
                ).iterdir()
            )
            if value["message_keys"]
        )
        self.assertEqual(payload["message_keys"], sorted(payload["message_keys"]))

    def test_bounded_segments_resume_without_reprocessing_completed_items(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider(
            [
                message(1, conversation="conversation_a"),
                message(2, conversation="conversation_b"),
                message(3, conversation="conversation_c"),
            ]
        )
        service = self.service(capture)

        first = service.run(all_history=True, max_terminal_items=1)
        self.assertTrue(first.segment_safe_stopped)
        self.assertEqual(first.segment_items_completed, 1)
        self.assertEqual(first.segment_remaining_items, 2)
        self.assertFalse(first.checkpoint_published)
        self.assertEqual(self.semantic.provider.calls, 1)

        second = service.run(max_terminal_items=1)
        self.assertTrue(second.segment_safe_stopped)
        self.assertEqual(second.segment_items_completed, 1)
        self.assertEqual(second.segment_remaining_items, 1)
        self.assertEqual(self.semantic.provider.calls, 2)

        final = service.run(max_terminal_items=1)
        self.assertFalse(final.segment_safe_stopped)
        self.assertTrue(final.checkpoint_published)
        self.assertEqual(final.segment_items_completed, 1)
        self.assertEqual(self.semantic.provider.calls, 3)
        self.assertIsNone(service.run_store.active_run_id())

        receipts = tuple(
            (
                self.workspace
                / "02_processing"
                / "wechat_digest"
                / "runs"
            ).glob("*/segments/segment-*.json")
        )
        self.assertEqual(len(receipts), 2)
        for path in receipts:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["schema_version"],
                "wechat-digest-run-segment-receipt/1.0",
            )
            self.assertEqual(payload["stop_reason"], "item_limit")
            self.assertEqual(payload["completed_items"], 1)
            self.assertRegex(payload["receipt_fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_segment_limit_stops_after_checkpoint_before_next_history_window(
        self,
    ) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider(
            [
                message(
                    1,
                    timestamp=1_700_000_000,
                    conversation="conversation_a",
                ),
                message(
                    2,
                    timestamp=1_700_000_100,
                    conversation="conversation_b",
                ),
            ],
            window_seconds=10,
        )
        service = self.service(capture)

        first = service.run(all_history=True, max_terminal_items=1)

        self.assertTrue(first.segment_safe_stopped)
        self.assertTrue(first.checkpoint_published)
        self.assertEqual(first.segment_items_completed, 1)
        self.assertEqual(first.segment_remaining_items, 0)
        self.assertEqual(self.semantic.provider.calls, 1)
        first_run_id = service.run_store.active_run_id()
        assert first_run_id is not None
        self.assertEqual(
            service.run_store.status(first_run_id)["state"], "completed"
        )

        final = service.run(max_terminal_items=1)

        self.assertFalse(final.segment_safe_stopped)
        self.assertTrue(final.checkpoint_published)
        self.assertEqual(self.semantic.provider.calls, 2)
        self.assertIsNone(service.run_store.active_run_id())

    def test_invalid_bounded_segment_size_fails_before_capture(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        service = self.service(capture)
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(WechatDigestError):
                service.run(all_history=True, max_terminal_items=value)
        self.assertEqual(capture.calls, [])

    def test_segment_receipt_crash_resumes_without_reprocessing_item(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider(
            [
                message(1, conversation="conversation_a"),
                message(2, conversation="conversation_b"),
            ]
        )
        failures = 1

        def fail_once() -> None:
            nonlocal failures
            if failures:
                failures -= 1
                raise RuntimeError("synthetic segment receipt interruption")

        run_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest",
            before_segment_receipt_write=fail_once,
        )
        service = self.service(capture, run_store=run_store)
        with self.assertRaises(RuntimeError):
            service.run(all_history=True, max_terminal_items=1)
        self.assertEqual(self.semantic.provider.calls, 1)
        self.assertEqual(
            tuple(run_store.runs_root.glob("*/segments/segment-*.json")), ()
        )

        result = service.run(max_terminal_items=1)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(self.semantic.provider.calls, 2)
        self.assertIsNone(run_store.active_run_id())

    def test_all_history_uses_durable_thirty_day_windows(self) -> None:
        self.create_object()
        day = 24 * 60 * 60
        capture = SyntheticCaptureProvider(
            [
                message(1, timestamp=1_700_000_000),
                message(2, timestamp=1_700_000_000 + 31 * day),
                message(3, timestamp=1_700_000_000 + 65 * day),
            ],
            window_seconds=30 * day,
        )
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.new_messages, 3)
        non_empty = [
            output
            for call, output in zip(capture.calls, capture.outputs, strict=True)
            if call[3] is not None and output.messages
        ]
        self.assertEqual(len(non_empty), 3)
        self.assertTrue(
            all(
                item.messages[-1].timestamp - item.messages[0].timestamp < 30 * day
                for item in non_empty
            )
        )
        self.assertEqual(
            WechatDigestRunStore(
                self.workspace / "02_processing" / "wechat_digest"
            ).checkpoint(),
            capture.messages[-1].cursor,
        )

    def test_all_history_freezes_one_upper_and_defers_messages_arriving_mid_run(
        self,
    ) -> None:
        self.create_object()
        day = 24 * 60 * 60

        class AppendAfterObservation(SyntheticCaptureProvider):
            appended = False

            def capture(self, *args, **kwargs):
                result = super().capture(*args, **kwargs)
                if kwargs.get("observe_only") and not self.appended:
                    self.appended = True
                    self.messages.append(
                        message(4, timestamp=1_700_000_000 + 95 * day)
                    )
                return result

        capture = AppendAfterObservation(
            [
                message(1, timestamp=1_700_000_000),
                message(2, timestamp=1_700_000_000 + 31 * day),
                message(3, timestamp=1_700_000_000 + 65 * day),
            ],
            window_seconds=30 * day,
        )
        frozen_upper = capture.messages[-1].cursor
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.new_messages, 3)
        self.assertEqual(
            self.service(capture).run_store.checkpoint(), frozen_upper
        )
        plans = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (
                self.workspace / "02_processing" / "wechat_digest" / "runs"
            ).glob("*/plan.json")
        ]
        self.assertTrue(plans)
        self.assertTrue(
            all(plan["all_history_upper_bound"] == frozen_upper.to_dict() for plan in plans)
        )
        self.assertTrue(
            all(
                json.loads(
                    (path.parent / "run-plan-receipt.json").read_text(
                        encoding="utf-8"
                    )
                )["plan_fingerprint"]
                == _plan_fingerprint(json.loads(path.read_text(encoding="utf-8")))
                for path in (
                    self.workspace / "02_processing" / "wechat_digest" / "runs"
                ).glob("*/plan.json")
            )
        )
        incremental = self.service(capture).run()
        self.assertEqual(incremental.new_messages, 1)
        self.assertEqual(
            self.service(capture).run_store.checkpoint(), capture.messages[-1].cursor
        )

    def test_all_history_resume_reuses_upper_and_missing_tail_fails_closed(
        self,
    ) -> None:
        self.create_object()
        day = 24 * 60 * 60
        capture = SyntheticCaptureProvider(
            [
                message(1, timestamp=1_700_000_000),
                message(2, timestamp=1_700_000_000 + 31 * day),
            ],
            window_seconds=30 * day,
        )
        service = self.service(capture)
        with service.run_store.lock():
            first = service._run_locked(
                since=None, from_now=False, all_history=True
            )
        self.assertEqual(first.new_messages, 1)
        active_run_id = service.run_store.active_run_id()
        assert active_run_id is not None
        frozen = service.run_store.plan(active_run_id)["all_history_upper_bound"]
        self.assertEqual(frozen, capture.messages[-1].cursor.to_dict())
        capture.messages.pop()
        before_calls = self.semantic.provider.calls
        with self.assertRaisesRegex(WechatDigestError, "边界无法继续读回"):
            service.run()
        self.assertEqual(self.semantic.provider.calls, before_calls)
        self.assertEqual(service.run_store.checkpoint(), capture.messages[-1].cursor)

    def test_second_window_failure_resumes_from_last_published_checkpoint(self) -> None:
        self.create_object()
        day = 24 * 60 * 60
        capture = SyntheticCaptureProvider(
            [
                message(1, timestamp=1_700_000_000),
                message(2, timestamp=1_700_000_000 + 31 * day),
                message(3, timestamp=1_700_000_000 + 65 * day),
            ],
            window_seconds=30 * day,
        )
        service = self.service(
            capture, service_type=FailSecondGovernanceOnceService
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        self.assertEqual(service.run_store.checkpoint(), capture.messages[0].cursor)
        sources_after_failure = self.source_count()
        result = service.run()
        self.assertTrue(result.replayed)
        self.assertEqual(result.new_messages, 2)
        self.assertGreaterEqual(self.source_count(), sources_after_failure)
        self.assertEqual(service.run_store.checkpoint(), capture.messages[-1].cursor)

    def test_fixed_upper_bound_defers_messages_arriving_after_capture(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        first = self.service(capture).run(all_history=True)
        self.assertEqual(first.new_messages, 1)
        capture.messages.append(message(2))
        second = self.service(capture).run()
        self.assertEqual(second.new_messages, 1)

    def test_attachment_is_independent_source_with_message_provenance(self) -> None:
        self.create_object()
        path = Path(self.temporary.name) / "synthetic.txt"
        path.write_text("Synthetic Project attachment evidence.", encoding="utf-8")
        item = attachment(path, "attachment_a")
        capture = SyntheticCaptureProvider([message(1, attachments=(item,))])
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.new_attachments, 1)
        self.assertEqual(self.source_count(), 2)
        representations = LocalRepresentationRepository(
            self.workspace / "02_processing" / "representations"
        )
        conversation = next(
            representation
            for representation in representations.list_for_source(
                next(
                    source.source_id
                    for source in LocalManagedSourceRepository(
                        self.workspace / "01_inbox"
                    ).list_sources()
                    if source.media_type == "application/json"
                )
            )
            if representation.adapter_name == "wechat-conversation-v2"
        )
        artifact = conversation.artifacts[0]
        payload = json.loads(
            representations.read_artifact(
                conversation.representation_id, artifact.artifact_id
            )
        )
        reference = payload["conversation"]["messages"][0]["attachment_refs"][0]
        self.assertEqual(reference["status"], "available")
        self.assertTrue(reference["source_id"].startswith("src_"))

    def test_same_attachment_bytes_in_two_messages_keep_two_sources(self) -> None:
        self.create_object()
        path = Path(self.temporary.name) / "same.txt"
        path.write_text("same bytes", encoding="utf-8")
        capture = SyntheticCaptureProvider(
            [
                message(1, attachments=(attachment(path, "attachment_a"),)),
                message(2, attachments=(attachment(path, "attachment_b"),)),
            ]
        )
        self.service(capture).run(all_history=True)
        sources = LocalManagedSourceRepository(
            self.workspace / "01_inbox"
        ).list_sources()
        attachment_sources = [source for source in sources if source.media_type == "text/plain"]
        self.assertEqual(len(attachment_sources), 2)
        self.assertEqual(len({source.content_hash for source in attachment_sources}), 1)
        self.assertEqual(len({source.source_id for source in attachment_sources}), 2)

    def test_missing_and_ambiguous_attachments_are_terminal_without_guessing(self) -> None:
        capture = SyntheticCaptureProvider(
            [
                message(
                    1,
                    attachments=(
                        attachment(None, "missing", status="missing"),
                        attachment(None, "ambiguous", status="ambiguous"),
                    ),
                )
            ]
        )
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.unsupported, 3)
        self.assertEqual(self.source_count(), 1)

    def test_unsupported_attachment_is_preserved_without_semantic_call(self) -> None:
        self.create_object()
        path = Path(self.temporary.name) / "archive.bin"
        path.write_bytes(b"synthetic binary")
        capture = SyntheticCaptureProvider(
            [
                message(
                    1,
                    attachments=(
                        attachment(
                            path,
                            "binary",
                            media_type="application/octet-stream",
                            filename="archive.bin",
                        ),
                    ),
                )
            ]
        )
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.unsupported, 2)
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(self.source_count(), 2)

    def test_privacy_local_only_makes_zero_provider_calls(self) -> None:
        capture = SyntheticCaptureProvider(
            [message(1, content="API key: synthetic-sensitive-value")]
        )
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.local_only, 1)
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(result.durable_information, 0)

    def test_privacy_gate_does_not_block_ordinary_business_names(self) -> None:
        decision = DeterministicPrivacyGate().evaluate(
            ["Synthetic Company and Project discussed an ordinary quotation."]
        )
        self.assertEqual(decision.route, "approved")

    def test_privacy_gate_keeps_incomplete_representation_local(self) -> None:
        decision = DeterministicPrivacyGate().evaluate(
            ["Synthetic ordinary content."], semantic_completeness_known=False
        )
        self.assertEqual(decision.route, "local_only")
        self.assertEqual(
            decision.categories, ("unresolved_high_sensitivity",)
        )

    def test_semantic_failure_keeps_checkpoint_and_replay_reuses_source(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        self.assertFalse(
            (self.workspace / "02_processing" / "wechat_digest" / "checkpoint.json").exists()
        )
        sources_after_failure = self.source_count()
        result = self.service(capture).run()
        self.assertTrue(result.replayed)
        self.assertEqual(self.source_count(), sources_after_failure)

    def test_atomic_information_then_downstream_failure_replays_without_duplicates(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        service = self.service(capture, service_type=FailGovernanceOnceService)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        information_path = self.workspace / "03_information" / "atomic_information.jsonl"
        before = information_path.read_text(encoding="utf-8").splitlines()
        provider_calls = self.semantic.provider.calls
        result = service.run()
        after = information_path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(result.replayed)
        before_ids = {
            json.loads(line)["atomic_information_id"] for line in before
        }
        after_ids = {json.loads(line)["atomic_information_id"] for line in after}
        self.assertEqual(before_ids, after_ids)
        self.assertEqual(len(after_ids), 1)
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_exact_replay_after_governance_does_not_duplicate_durable_state(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        service = self.service(
            capture, service_type=FailAfterGovernanceOnceService
        )
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        sources_before = self.source_count()
        representations_before = len(
            tuple(
                (self.workspace / "02_processing" / "representations").glob(
                    "repr_*"
                )
            )
        )
        information_before = (
            self.workspace / "03_information" / "atomic_information.jsonl"
        ).read_text(encoding="utf-8")
        journal_before = tuple(service.journal.list_changes())
        with SQLiteWorldModelRepository(
            self.workspace / "04_core" / "archeos.sqlite3"
        ) as repository:
            objects_before = tuple(repository.list_objects())

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertEqual(self.source_count(), sources_before)
        self.assertEqual(
            len(
                tuple(
                    (self.workspace / "02_processing" / "representations").glob(
                        "repr_*"
                    )
                )
            ),
            representations_before,
        )
        self.assertEqual(
            (
                self.workspace / "03_information" / "atomic_information.jsonl"
            ).read_text(encoding="utf-8"),
            information_before,
        )
        self.assertEqual(tuple(service.journal.list_changes()), journal_before)
        with SQLiteWorldModelRepository(
            self.workspace / "04_core" / "archeos.sqlite3"
        ) as repository:
            self.assertEqual(tuple(repository.list_objects()), objects_before)

    def test_pending_human_does_not_block_independent_conversation(self) -> None:
        self.create_object("Ambiguous Project")
        self.create_object("Ambiguous Project")
        self.create_object("Synthetic Project")
        capture = SyntheticCaptureProvider(
            [
                message(1, content="Ambiguous Project needs review."),
                message(2, conversation="conversation_b"),
            ]
        )
        result = self.service(capture).run(all_history=True)
        self.assertEqual(result.pending_human, 1)
        self.assertEqual(result.durable_information, 2)
        self.assertTrue(result.checkpoint_published)

    def test_checkpoint_publication_failure_recovers_without_skipping(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        failures = {"remaining": 1}

        def fail_once() -> None:
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise OSError("synthetic checkpoint failure")

        store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest",
            before_checkpoint_publish=fail_once,
        )
        service = self.service(capture, run_store=store)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        provider_calls = self.semantic.provider.calls
        result = service.run()
        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_durable_run_paths_are_workspace_owned_not_code_worktree_owned(self) -> None:
        capture = SyntheticCaptureProvider([])
        self.service(capture).run(from_now=True)
        run_root = self.workspace / "02_processing" / "wechat_digest"
        self.assertTrue((run_root / "checkpoint.json").is_file())
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in run_root.rglob("*.json")
        )
        self.assertNotIn(".codex/worktrees", text)

    def test_resume_uses_durable_capture_when_connector_content_changes(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            self.service(capture).run(all_history=True)
        calls_after_initial_capture = len(capture.calls)
        capture.messages[0] = replace(capture.messages[0], visible_content="changed")
        result = self.service(capture).run()
        self.assertTrue(result.replayed)
        self.assertEqual(len(capture.calls), calls_after_initial_capture)

    def test_durable_capture_snapshot_drift_fails_closed_before_resume(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        snapshot = (
            self.workspace
            / "02_processing"
            / "wechat_digest"
            / "runs"
            / run_id
            / "capture"
            / "snapshot.json"
        )
        snapshot.write_bytes(snapshot.read_bytes() + b" ")
        calls_before_resume = len(capture.calls)

        with self.assertRaisesRegex(WechatDigestError, "fingerprint"):
            self.service(capture).run()

        self.assertEqual(len(capture.calls), calls_before_resume)

    def test_snapshot_publish_interruption_resumes_without_second_capture(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])

        def interrupt() -> None:
            raise OSError("synthetic snapshot interruption")

        interrupted_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest",
            after_capture_snapshot_write=interrupt,
        )
        with self.assertRaises(WechatDigestError):
            self.service(capture, run_store=interrupted_store).run(
                all_history=True
            )
        self.assertEqual(len(capture.calls), 2)

        resumed = self.service(capture).run()

        self.assertTrue(resumed.checkpoint_published)
        self.assertEqual(len(capture.calls), 2)

    def _make_represented_pre_provider_value_error_run(
        self, *, conversation_count: int = 27
    ) -> tuple[
        WechatDigestService,
        SyntheticCaptureProvider,
        str,
        str,
    ]:
        messages = [
            message(index, conversation=f"conversation_{index:02d}")
            for index in range(1, conversation_count + 1)
        ]
        capture_provider = SyntheticCaptureProvider(messages)
        capture = WechatCapture(
            capture_provider.provider_version,
            ZERO_CURSOR,
            messages[-1].cursor,
            tuple(messages),
        )
        plan, status = _build_plan(
            capture,
            clock=lambda: "2026-08-25T00:00:00Z",
            all_history_upper_bound=messages[-1].cursor,
        )
        service = self.service(capture_provider)
        service.run_store.create(plan, status)
        conversation_plan = plan["conversations"][0]
        assert isinstance(conversation_plan, dict)
        item_id = f"conversation:{conversation_plan['conversation_key']}"
        payload = _conversation_source_payload(
            capture,
            str(conversation_plan["conversation_key"]),
            {},
        )
        representation_id = service._process_conversation(
            str(plan["run_id"]),
            status,
            item_id,
            conversation_plan,
            payload,
            prepare_only=True,
        )
        assert representation_id is not None
        failed = service.run_store.status(str(plan["run_id"]))
        failed["state"] = "failed"
        failed["failure_category"] = "ValueError"
        failed["updated_at"] = "2026-08-25T00:01:00Z"
        service.run_store.update_status(str(plan["run_id"]), failed)
        return service, capture_provider, str(plan["run_id"]), item_id

    def test_capture_upgrade_recovers_one_represented_pre_provider_item(
        self,
    ) -> None:
        service, capture_provider, run_id, item_id = (
            self._make_represented_pre_provider_value_error_run()
        )
        before = service.run_store.status(run_id)
        before_items = before["items"]
        represented = before_items[item_id]
        representation_id = represented["representation_id"]
        representation_before = service.representation_repository.get(
            representation_id
        )

        with service.run_store.lock():
            _capture, upgraded_plan, upgraded = (
                service._load_or_upgrade_active_capture(
                    run_id,
                    service.run_store.plan(run_id),
                    before,
                )
            )

        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(upgraded_plan["schema_version"], "wechat-digest-run-plan/4.0")
        self.assertEqual(upgraded["state"], "processing")
        self.assertIsNone(upgraded["failure_category"])
        self.assertFalse(upgraded["checkpoint_published"])
        self.assertEqual(upgraded["items"], before_items)
        self.assertEqual(
            service.representation_repository.get(representation_id),
            representation_before,
        )
        self.assertEqual(
            Counter(item["state"] for item in upgraded["items"].values()),
            Counter({"planned": 26, "represented": 1}),
        )

        first_capture_calls = len(capture_provider.calls)
        first_status = service.run_store.status(run_id)
        run_dir = service.run_store.runs_root / run_id
        before_replay = {
            path.relative_to(run_dir): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        with service.run_store.lock():
            _capture, replay_plan, replay_status = (
                service._load_or_upgrade_active_capture(
                    run_id,
                    service.run_store.plan(run_id),
                    first_status,
                )
            )
        self.assertEqual(len(capture_provider.calls), first_capture_calls)
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(replay_plan, upgraded_plan)
        self.assertEqual(replay_status, first_status)
        self.assertEqual(
            {
                path.relative_to(run_dir): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            },
            before_replay,
        )

    def test_reviewed_head_continuation_binds_current_represented_boundary_zero_calls(
        self,
    ) -> None:
        service, capture_provider, run_id, _item_id = (
            self._make_represented_pre_provider_value_error_run()
        )
        with service.run_store.lock():
            service._load_or_upgrade_active_capture(
                run_id,
                service.run_store.plan(run_id),
                service.run_store.status(run_id),
            )
        plan = service.run_store.plan(run_id)
        status = service.run_store.status(run_id)
        self.semantic.global_attempt_total = 361
        self.semantic.reviewed_git_head = "a" * 40
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=plan["created_at"],
            lower_cursor=service._cursor_tuple(
                WechatCursor.from_dict(plan["after_cursor"])
            ),
            frozen_global_upper_cursor=service._cursor_tuple(
                WechatCursor.from_dict(plan["all_history_upper_bound"])
            ),
            capture_provider_version=plan["provider_version"],
            semantic_batch_size=plan["semantic_batch_size"],
            reviewed_git_head="9" * 40,
        )
        run_dir = service.run_store.runs_root / run_id
        run_bytes_before = {
            path.relative_to(run_dir): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        business_before = {
            path.relative_to(self.workspace): path.read_bytes()
            for root in ("03_information", "04_core")
            for path in (self.workspace / root).rglob("*")
            if path.is_file()
        }
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/176"
            "#issuecomment-5402000000"
        )

        continuation = service.install_semantic_reviewed_head_continuation(
            authority_ref=authority_ref
        )

        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(continuation["activation_total"], 361)
        self.assertEqual(continuation["next_global_ordinal"], 362)
        self.assertEqual(continuation["authority_ref"], authority_ref)
        self.assertEqual(
            continuation["active_run"],
            {
                "run_id": run_id,
                "plan_fingerprint": status["plan_fingerprint"],
                "capture_receipt_fingerprint": plan[
                    "capture_receipt_fingerprint"
                ],
                "status_fingerprint": _sha256_bytes(
                    _canonical_json(status).encode("utf-8")
                ),
            },
        )
        self.assertEqual(
            {
                path.relative_to(run_dir): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            },
            run_bytes_before,
        )
        self.assertEqual(
            {
                path.relative_to(self.workspace): path.read_bytes()
                for root in ("03_information", "04_core")
                for path in (self.workspace / root).rglob("*")
                if path.is_file()
            },
            business_before,
        )

    def test_semantic_handoff_pre_attempt_normalizes_two_represented_then_installs(
        self,
    ) -> None:
        service, capture_provider, run_id, first_item_id = (
            self._make_represented_pre_provider_value_error_run()
        )
        with service.run_store.lock():
            capture, plan, status = service._load_or_upgrade_active_capture(
                run_id,
                service.run_store.plan(run_id),
                service.run_store.status(run_id),
            )
        second_plan = plan["conversations"][1]
        assert isinstance(second_plan, dict)
        second_item_id = f"conversation:{second_plan['conversation_key']}"
        second_payload = _conversation_source_payload(
            capture,
            str(second_plan["conversation_key"]),
            {},
        )
        second_representation_id = service._process_conversation(
            run_id,
            status,
            second_item_id,
            second_plan,
            second_payload,
            prepare_only=True,
        )
        assert second_representation_id is not None
        failed = service.run_store.status(run_id)
        first_representation_id = failed["items"][first_item_id][
            "representation_id"
        ]
        assert isinstance(first_representation_id, str)
        failed["state"] = "failed"
        failed["failure_category"] = "SemanticHandoffError"
        failed["updated_at"] = "2026-08-25T00:02:00Z"
        service.run_store.update_status(run_id, failed)

        semantic_run_id = "semantic_run_" + "a" * 32
        semantic_run = (
            self.workspace
            / "02_processing"
            / "semantic_handoff_runs"
            / semantic_run_id
        )
        semantic_run.mkdir(parents=True)
        (semantic_run / "run-receipt.json").write_text(
            json.dumps(
                {
                    "representation": {
                        "representation_id": first_representation_id
                    }
                }
            ),
            encoding="utf-8",
        )
        self.semantic.pre_attempt_inventories[first_representation_id] = {
            "inventory_kind": "run_receipt_only",
            "semantic_run_id": semantic_run_id,
            "run_receipt_fingerprint": "sha256:" + "b" * 64,
            "attempt_count": 0,
            "reserved_count": 0,
            "started_count": 0,
            "result_count": 0,
        }
        before_items = failed["items"]
        capture_calls_before = len(capture_provider.calls)

        prepared = service.prepare_next_semantic()

        normalized = service.run_store.status(run_id)
        self.assertEqual(prepared.representation_id, first_representation_id)
        self.assertEqual(normalized["state"], "processing")
        self.assertIsNone(normalized["failure_category"])
        self.assertEqual(normalized["items"], before_items)
        self.assertFalse(normalized["checkpoint_published"])
        self.assertEqual(len(capture_provider.calls), capture_calls_before)
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(
            Counter(item["state"] for item in normalized["items"].values()),
            Counter({"planned": 25, "represented": 2}),
        )

        self.semantic.global_attempt_total = 361
        self.semantic.reviewed_git_head = "a" * 40
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=plan["created_at"],
            lower_cursor=service._cursor_tuple(
                WechatCursor.from_dict(plan["after_cursor"])
            ),
            frozen_global_upper_cursor=service._cursor_tuple(
                WechatCursor.from_dict(plan["all_history_upper_bound"])
            ),
            capture_provider_version=plan["provider_version"],
            semantic_batch_size=plan["semantic_batch_size"],
            reviewed_git_head="9" * 40,
        )
        continuation = service.install_semantic_reviewed_head_continuation(
            authority_ref=(
                "https://github.com/leevi2010-cursor/ArcheOS/issues/180"
                "#issuecomment-5403529548"
            )
        )
        self.assertEqual(continuation["activation_total"], 361)
        self.assertEqual(continuation["next_global_ordinal"], 362)
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(len(capture_provider.calls), capture_calls_before)

    def test_value_error_pre_provider_accepts_two_absent_representations(
        self,
    ) -> None:
        service, capture_provider, run_id, _first_item_id = (
            self._make_represented_pre_provider_value_error_run()
        )
        plan = service.run_store.plan(run_id)
        capture = WechatCapture(
            capture_provider.provider_version,
            ZERO_CURSOR,
            capture_provider.messages[-1].cursor,
            tuple(capture_provider.messages),
        )
        status = service.run_store.status(run_id)
        second_plan = plan["conversations"][1]
        assert isinstance(second_plan, dict)
        second_item_id = f"conversation:{second_plan['conversation_key']}"
        second_representation_id = service._process_conversation(
            run_id,
            status,
            second_item_id,
            second_plan,
            _conversation_source_payload(
                capture,
                str(second_plan["conversation_key"]),
                {},
            ),
            prepare_only=True,
        )
        assert second_representation_id is not None
        failed = service.run_store.status(run_id)
        failed["state"] = "failed"
        failed["failure_category"] = "ValueError"
        service.run_store.update_status(run_id, failed)

        with service.run_store.lock():
            _capture, _plan, normalized = (
                service._load_or_upgrade_active_capture(
                    run_id,
                    service.run_store.plan(run_id),
                    service.run_store.status(run_id),
                )
            )

        self.assertEqual(normalized["state"], "processing")
        self.assertIsNone(normalized["failure_category"])
        self.assertEqual(
            Counter(item["state"] for item in normalized["items"].values()),
            Counter({"planned": 25, "represented": 2}),
        )
        self.assertEqual(self.semantic.provider.calls, 0)

    def _make_reviewed_head_multi_window_run(
        self,
    ) -> tuple[
        WechatDigestService,
        SyntheticCaptureProvider,
        str,
        str,
        WechatCursor,
    ]:
        previous_message = message(1, conversation="previous_window")
        current_messages = [
            message(index, conversation=f"conversation_{index:02d}")
            for index in range(2, 29)
        ]
        global_upper = current_messages[-1].cursor
        capture_provider = SyntheticCaptureProvider(
            [previous_message, *current_messages]
        )
        service = self.service(capture_provider)
        run_store = service.run_store
        created_at = "2026-08-25T00:00:00Z"

        previous_capture = WechatCapture(
            capture_provider.provider_version,
            ZERO_CURSOR,
            previous_message.cursor,
            (previous_message,),
        )
        previous_legacy_plan, _ = _build_plan(
            previous_capture,
            clock=lambda: created_at,
            all_history_upper_bound=global_upper,
        )
        run_store.publish_capture_pending(
            previous_legacy_plan,
            previous_capture,
            capture_ms=1,
        )
        previous_receipt = run_store.publish_capture_artifacts(
            str(previous_legacy_plan["run_id"]),
            previous_capture,
            plan_binding_fingerprint=_plan_fingerprint(previous_legacy_plan),
            capture_ms=1,
        )
        previous_plan, previous_status = _build_plan(
            previous_capture,
            clock=lambda: created_at,
            run_id=str(previous_legacy_plan["run_id"]),
            created_at=created_at,
            all_history_upper_bound=global_upper,
            capture_receipt_fingerprint=str(
                previous_receipt["receipt_fingerprint"]
            ),
        )
        previous_status["state"] = "completed"
        previous_status["checkpoint_published"] = True
        for item in previous_status["items"].values():
            item["state"] = "unsupported"
        previous_run_id = str(previous_plan["run_id"])
        run_store.create(previous_plan, previous_status)
        run_store.clear_active()
        run_store.publish_checkpoint(
            previous_run_id,
            previous_message.cursor,
        )

        current_capture = WechatCapture(
            capture_provider.provider_version,
            previous_message.cursor,
            global_upper,
            tuple(current_messages),
        )
        current_legacy_plan, _ = _build_plan(
            current_capture,
            clock=lambda: created_at,
            all_history_upper_bound=global_upper,
        )
        run_store.publish_capture_pending(
            current_legacy_plan,
            current_capture,
            capture_ms=1,
        )
        current_receipt = run_store.publish_capture_artifacts(
            str(current_legacy_plan["run_id"]),
            current_capture,
            plan_binding_fingerprint=_plan_fingerprint(current_legacy_plan),
            capture_ms=1,
        )
        current_plan, current_status = _build_plan(
            current_capture,
            clock=lambda: created_at,
            run_id=str(current_legacy_plan["run_id"]),
            created_at=created_at,
            all_history_upper_bound=global_upper,
            capture_receipt_fingerprint=str(
                current_receipt["receipt_fingerprint"]
            ),
        )
        current_run_id = str(current_plan["run_id"])
        run_store.create(current_plan, current_status)
        conversation_plan = current_plan["conversations"][0]
        assert isinstance(conversation_plan, dict)
        item_id = f"conversation:{conversation_plan['conversation_key']}"
        payload = _conversation_source_payload(
            current_capture,
            str(conversation_plan["conversation_key"]),
            {},
        )
        representation_id = service._process_conversation(
            current_run_id,
            current_status,
            item_id,
            conversation_plan,
            payload,
            prepare_only=True,
        )
        assert representation_id is not None
        self.semantic.global_attempt_total = 361
        self.semantic.reviewed_git_head = "a" * 40
        self.semantic.campaign_binding = SimpleNamespace(
            created_at=created_at,
            lower_cursor=service._cursor_tuple(ZERO_CURSOR),
            frozen_global_upper_cursor=service._cursor_tuple(global_upper),
            capture_provider_version=current_plan["provider_version"],
            semantic_batch_size=current_plan["semantic_batch_size"],
            reviewed_git_head="9" * 40,
        )
        return (
            service,
            capture_provider,
            current_run_id,
            previous_run_id,
            previous_message.cursor,
        )

    def test_reviewed_head_continuation_preserves_matching_previous_checkpoint(
        self,
    ) -> None:
        (
            service,
            capture_provider,
            run_id,
            _previous_run_id,
            expected_checkpoint,
        ) = self._make_reviewed_head_multi_window_run()
        checkpoint_bytes_before = service.run_store.checkpoint_path.read_bytes()
        plan_before = service.run_store.plan(run_id)
        status_before = service.run_store.status(run_id)

        continuation = service.install_semantic_reviewed_head_continuation(
            authority_ref=(
                "https://github.com/leevi2010-cursor/ArcheOS/issues/178"
                "#issuecomment-5403000000"
            )
        )

        self.assertEqual(continuation["activation_total"], 361)
        self.assertEqual(len(self.semantic.installed_reviewed_head_continuations), 1)
        self.assertEqual(capture_provider.calls, [])
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(service.run_store.checkpoint(), expected_checkpoint)
        self.assertEqual(
            service.run_store.checkpoint_path.read_bytes(),
            checkpoint_bytes_before,
        )
        self.assertEqual(service.run_store.plan(run_id), plan_before)
        self.assertEqual(service.run_store.status(run_id), status_before)

    def test_reviewed_head_continuation_rejects_checkpoint_mismatch_before_write(
        self,
    ) -> None:
        (
            service,
            capture_provider,
            run_id,
            previous_run_id,
            _expected_checkpoint,
        ) = self._make_reviewed_head_multi_window_run()
        service.run_store.publish_checkpoint(previous_run_id, ZERO_CURSOR)
        digest_root = service.run_store.root
        with service.run_store.lock():
            pass
        before = {
            path.relative_to(digest_root): path.read_bytes()
            for path in digest_root.rglob("*")
            if path.is_file()
        }
        business_before = {
            path.relative_to(self.workspace): path.read_bytes()
            for root in ("03_information", "04_core")
            for path in (self.workspace / root).rglob("*")
            if path.is_file()
        }

        with self.assertRaisesRegex(WechatDigestError, "checkpoint binding"):
            service.install_semantic_reviewed_head_continuation(
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/178"
                    "#issuecomment-5403000000"
                )
            )

        self.assertEqual(self.semantic.installed_reviewed_head_continuations, [])
        self.assertEqual(capture_provider.calls, [])
        self.assertEqual(self.semantic.provider.calls, 0)
        self.assertEqual(
            {
                path.relative_to(digest_root): path.read_bytes()
                for path in digest_root.rglob("*")
                if path.is_file()
            },
            before,
        )
        self.assertEqual(
            {
                path.relative_to(self.workspace): path.read_bytes()
                for root in ("03_information", "04_core")
                for path in (self.workspace / root).rglob("*")
                if path.is_file()
            },
            business_before,
        )

    def test_capture_upgrade_keeps_all_planned_value_error_recovery(self) -> None:
        captured_message = message(1)
        capture_provider = SyntheticCaptureProvider([captured_message])
        capture = WechatCapture(
            capture_provider.provider_version,
            ZERO_CURSOR,
            captured_message.cursor,
            (captured_message,),
        )
        plan, status = _build_plan(
            capture,
            clock=lambda: "2026-08-25T00:00:00Z",
        )
        status["state"] = "failed"
        status["failure_category"] = "ValueError"
        service = self.service(capture_provider)
        service.run_store.create(plan, status)

        with service.run_store.lock():
            _capture, _plan, upgraded = service._load_or_upgrade_active_capture(
                str(plan["run_id"]), plan, status
            )

        self.assertEqual(upgraded["state"], "processing")
        self.assertIsNone(upgraded["failure_category"])
        self.assertFalse(upgraded["checkpoint_published"])
        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_capture_upgrade_rejects_represented_semantic_or_governance_trace_before_write(
        self,
    ) -> None:
        cases = (
            "semantic_package",
            "semantic_run_receipt",
            "semantic_attempt",
            "semantic_started",
            "semantic_result",
            "atomic_information",
            "governance_receipt",
            "governance_metrics",
            "semantic_failure",
            "governance_failure",
        )
        for index, case in enumerate(cases):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(case=case):
                service, capture_provider, run_id, item_id = (
                    self._make_represented_pre_provider_value_error_run()
                )
                status_path = service.run_store.runs_root / run_id / "status.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                item = status["items"][item_id]
                representation_id = item["representation_id"]
                if case == "semantic_package":
                    (
                        self.workspace
                        / "02_processing"
                        / "information"
                        / representation_id
                    ).mkdir(parents=True)
                elif case.startswith("semantic_"):
                    run_dir = (
                        self.workspace
                        / "02_processing"
                        / "semantic_handoff_runs"
                        / ("semantic_run_" + "a" * 32)
                    )
                    run_dir.mkdir(parents=True)
                    (run_dir / "run-receipt.json").write_text(
                        json.dumps(
                            {
                                "representation": {
                                    "representation_id": representation_id
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    marker_directories = {
                        "semantic_attempt": "attempts",
                        "semantic_started": "started",
                        "semantic_result": "results/batch_0001",
                    }
                    marker_directory = marker_directories.get(case)
                    if marker_directory is not None:
                        marker_root = run_dir / marker_directory
                        marker_root.mkdir(parents=True)
                        (marker_root / "trace.json").write_text(
                            json.dumps({"synthetic": True}),
                            encoding="utf-8",
                        )
                elif case == "atomic_information":
                    item["atomic_information_ids"] = ["info_" + "a" * 64]
                else:
                    item[case] = {"synthetic": True}
                if case not in {"semantic_package", "semantic_run_receipt"}:
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                run_dir = service.run_store.runs_root / run_id
                before = {
                    path.relative_to(run_dir): path.read_bytes()
                    for path in run_dir.rglob("*")
                    if path.is_file()
                }

                with service.run_store.lock(), self.assertRaises(WechatDigestError):
                    service._load_or_upgrade_active_capture(
                        run_id,
                        service.run_store.plan(run_id),
                        service.run_store.status(run_id),
                    )

                after = {
                    path.relative_to(run_dir): path.read_bytes()
                    for path in run_dir.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertEqual(capture_provider.calls, [])
                self.assertEqual(self.semantic.provider.calls, 0)

    def test_capture_upgrade_rejects_unverified_or_ambiguous_pre_provider_shape(
        self,
    ) -> None:
        cases = (
            "source_drift",
            "representation_drift",
            "multiple_represented",
            "unknown_state",
        )
        for index, case in enumerate(cases):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(case=case):
                service, capture_provider, run_id, item_id = (
                    self._make_represented_pre_provider_value_error_run()
                )
                status_path = service.run_store.runs_root / run_id / "status.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if case == "source_drift":
                    source_id = status["items"][item_id]["source_id"]
                    source = service.source_repository.get(source_id)
                    managed_path = (
                        self.workspace / "01_inbox" / source.managed_locator
                    )
                    managed_path.write_bytes(managed_path.read_bytes() + b"x")
                elif case == "representation_drift":
                    representation = status["items"][item_id]["representation_id"]
                    manifest_path = next(
                        (
                            self.workspace
                            / "02_processing"
                            / "representations"
                        ).glob(f"*/{representation}/manifest.json")
                    )
                    manifest_path.write_bytes(manifest_path.read_bytes() + b"x")
                elif case == "multiple_represented":
                    second_id = next(
                        key
                        for key, item in status["items"].items()
                        if key != item_id and item["state"] == "planned"
                    )
                    status["items"][second_id]["state"] = "represented"
                    status["items"][second_id]["representation_id"] = status[
                        "items"
                    ][item_id]["representation_id"]
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                else:
                    status["items"][item_id]["state"] = "processing"
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                run_dir = service.run_store.runs_root / run_id
                before = {
                    path.relative_to(run_dir): path.read_bytes()
                    for path in run_dir.rglob("*")
                    if path.is_file()
                }

                with service.run_store.lock(), self.assertRaises(WechatDigestError):
                    service._load_or_upgrade_active_capture(
                        run_id,
                        service.run_store.plan(run_id),
                        service.run_store.status(run_id),
                    )

                after = {
                    path.relative_to(run_dir): path.read_bytes()
                    for path in run_dir.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertEqual(capture_provider.calls, [])
                self.assertEqual(self.semantic.provider.calls, 0)

    def test_legacy_v3_capture_upgrade_adopts_every_artifact_breakpoint(self) -> None:
        hooks = (
            "after_capture_snapshot_write",
            "after_capture_index_write",
            "after_capture_summary_write",
            "after_capture_receipt_write",
            "before_upgrade_status_write",
        )
        for index, hook_name in enumerate(hooks):
            with self.subTest(hook=hook_name):
                capture = SyntheticCaptureProvider([message(1)])
                canonical = WechatCapture(
                    capture.provider_version,
                    ZERO_CURSOR,
                    capture.messages[0].cursor,
                    tuple(capture.messages),
                )
                plan, status = _build_plan(
                    canonical,
                    clock=lambda: "2026-08-25T00:00:00Z",
                )
                failed = [True]

                def interrupt(failed: list[bool] = failed) -> None:
                    if failed.pop():
                        raise RuntimeError("synthetic legacy capture breakpoint")

                run_store = WechatDigestRunStore(
                    self.workspace
                    / "02_processing"
                    / f"wechat_digest_legacy_capture_{index}",
                    **{hook_name: interrupt},
                )
                run_store.create(plan, status)
                service = self.service(capture, run_store=run_store)
                with self.assertRaises((WechatDigestError, RuntimeError)):
                    service.run()
                self.assertEqual(len(capture.calls), 1)
                setattr(run_store, hook_name, None)
                self.semantic.failures_remaining = 1
                try:
                    service.run()
                except WechatDigestError:
                    pass
                self.assertEqual(len(capture.calls), 1)
                self.assertEqual(
                    run_store.plan(str(plan["run_id"]))["schema_version"],
                    "wechat-digest-run-plan/4.0",
                )

    def test_production_shaped_durable_snapshot_benchmark_contract(self) -> None:
        missing_attachments = {
            number: attachment(
                None,
                f"missing_{number:032x}",
                status="missing",
                filename=f"fixture-{number}.bin",
            )
            for number in range(100)
        }
        messages = [
            message(
                number + 1,
                conversation=f"conversation_{number % 45:02d}",
                attachments=(missing_attachments[number // 10],)
                if number % 10 == 0
                else (),
            )
            for number in range(1000)
        ]
        capture = WechatCapture(
            "synthetic-production-shape/1.0",
            ZERO_CURSOR,
            messages[-1].cursor,
            tuple(messages),
        )
        plan, _status = _build_plan(
            capture,
            clock=lambda: "2026-08-25T00:00:00Z",
        )
        store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat-digest-benchmark"
        )
        started = time.monotonic()
        store.publish_capture_pending(plan, capture, capture_ms=1)
        receipt = store.publish_capture_artifacts(
            str(plan["run_id"]),
            capture,
            plan_binding_fingerprint=_plan_fingerprint(plan),
            capture_ms=1,
        )
        publish_ms = round((time.monotonic() - started) * 1000)
        readback_started = time.monotonic()
        loaded, loaded_receipt = store.load_capture_artifacts(
            str(plan["run_id"]),
            expected_plan_binding=_plan_fingerprint(plan),
        )
        readback_ms = round((time.monotonic() - readback_started) * 1000)

        self.assertEqual(len(loaded.messages), 1000)
        self.assertEqual(receipt["conversation_count"], 45)
        self.assertEqual(receipt["attachment_count"], 100)
        self.assertEqual(loaded_receipt, receipt)
        self.assertGreater(
            (
                self.workspace
                / "02_processing"
                / "wechat-digest-benchmark"
                / "runs"
                / str(plan["run_id"])
                / "capture"
                / "snapshot.json"
            ).stat().st_size,
            0,
        )
        self.assertGreaterEqual(publish_ms, 0)
        self.assertGreaterEqual(readback_ms, 0)

    def test_govern_item_serializes_two_real_thread_entries(self) -> None:
        first_entered = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        active_observations: list[int] = []

        class ControlledGovernanceService(WechatDigestService):
            def _govern_item_active(
                self,
                run_id,
                status,
                item_id,
                atomic_ids,
            ):
                del run_id, status
                with self._governance_observation_lock:
                    active_observations.append(self._governance_active)
                if item_id == "first":
                    first_entered.set()
                    if not release_first.wait(timeout=5):
                        raise AssertionError("first governance entry was not released")
                else:
                    second_entered.set()
                return False, tuple(atomic_ids)

        service = ControlledGovernanceService(
            workspace=self.workspace,
            capture_provider=SyntheticCaptureProvider([]),
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=NoStructuralChangeProvider(),
        )
        results: dict[str, tuple[bool, tuple[str, ...]]] = {}

        def invoke(item_id: str, atomic_id: str) -> None:
            if item_id == "second":
                second_started.set()
            results[item_id] = service._govern_item(
                "run_" + "1" * 32,
                {},
                item_id,
                (atomic_id,),
            )

        first = threading.Thread(target=invoke, args=("first", "atomic_first"))
        second = threading.Thread(target=invoke, args=("second", "atomic_second"))
        first.start()
        self.assertTrue(first_entered.wait(timeout=5))
        second.start()
        self.assertTrue(second_started.wait(timeout=5))
        self.assertFalse(second_entered.wait(timeout=0.05))
        with service._governance_observation_lock:
            self.assertEqual(service._governance_active, 1)
        self.assertEqual(
            service._segment_performance["governance_peak_concurrency"], 1
        )

        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(active_observations, [1, 1])
        self.assertEqual(
            results,
            {
                "first": (False, ("atomic_first",)),
                "second": (False, ("atomic_second",)),
            },
        )
        with service._governance_observation_lock:
            self.assertEqual(service._governance_active, 0)
        self.assertEqual(
            service._segment_performance["governance_peak_concurrency"], 1
        )

    def test_production_shaped_service_max3_uses_one_capture_and_serial_governance(
        self,
    ) -> None:
        shared_object_id = self.create_object()
        with SQLiteWorldModelRepository(
            self.workspace / "04_core" / "archeos.sqlite3"
        ) as repository:
            repository.add_role(shared_object_id, "brand")
            objects_before = repository.list_objects()
            names_before = repository.list_names(shared_object_id)
            roles_before = repository.list_roles(shared_object_id)
            apply_receipts_before = repository.list_apply_receipts()
        missing_attachments = {
            number: attachment(
                None,
                f"service_fixture_{number:032x}",
                status="missing",
                filename=f"fixture-{number}.bin",
            )
            for number in range(100)
        }
        messages = [
            message(
                number + 1,
                conversation=f"conversation_{number % 45:02d}",
                attachments=(missing_attachments[number // 10],)
                if number % 10 == 0
                else (),
            )
            for number in range(1000)
        ]
        capture = FailOnSecondFullCaptureProvider(messages)
        semantic = ObservedOutOfOrderSemanticHandoff(self.workspace)

        class BoundedBenchmarkAnalysisProvider(SyntheticAnalysisProvider):
            def analyze(self, batch):
                self.mode = "four_one" if self.calls < 2 else "all_residue"
                return super().analyze(batch)

        semantic.provider = BoundedBenchmarkAnalysisProvider()
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: semantic,
            interpretation_provider=NoStructuralChangeProvider(),
            semantic_parallelism=2,
        )
        proposals_before = service.proposal_store.list_history()
        journal_before = service.journal.list_changes()

        results = [service.run(all_history=True, max_terminal_items=3)]
        while service.run_store.active_run_id() is not None:
            results.append(service.run(max_terminal_items=3))

        self.assertEqual(sum(not call[2] for call in capture.calls), 1)
        self.assertEqual(sum(item.capture_provider_calls for item in results), 1)
        self.assertEqual(sum(item.upper_bound_probe_calls for item in results), 1)
        self.assertTrue(all(item.completed_window_connector_replays == 0 for item in results))
        self.assertTrue(all(item.segment_items_completed <= 3 for item in results))
        self.assertEqual(results[-1].new_messages, 1000)
        self.assertEqual(results[-1].new_attachments, 100)
        self.assertGreater(results[-1].snapshot_bytes, 0)
        self.assertEqual(max(item.semantic_parallelism for item in results), 2)
        self.assertEqual(max(item.semantic_peak_concurrency for item in results), 2)
        self.assertEqual(max(item.governance_peak_concurrency for item in results), 1)
        self.assertEqual(sum(item.resume_provider_calls for item in results), 0)
        self.assertTrue(
            any(
                completed != planned
                for (planned, _parallelism), completed in zip(
                    semantic.prepared_waves,
                    semantic.completion_orders,
                    strict=True,
                )
                if len(planned) > 1
            )
        )

        information = service.information_store.list_atomic_information()
        self.assertEqual(len(information), 8)
        self.assertEqual(
            len({item.atomic_information_id for item in information}),
            len(information),
        )
        revision_history = {
            item.atomic_information_id: service.information_store.list_revisions(
                item.atomic_information_id
            )
            for item in information
        }
        self.assertTrue(
            all(len(revisions) == 2 for revisions in revision_history.values())
        )
        self.assertTrue(
            all(shared_object_id in item.related_object_ids for item in information)
        )
        with SQLiteWorldModelRepository(service.database) as repository:
            objects = repository.list_objects()
            self.assertEqual(objects, objects_before)
            names = repository.list_names(shared_object_id)
            roles = repository.list_roles(shared_object_id)
            self.assertEqual(names, names_before)
            self.assertEqual(roles, roles_before)
            apply_receipts = repository.list_apply_receipts()
            effect_bindings = {
                item.atomic_information_id: service._governance_effect_binding(
                    repository, item.atomic_information_id
                )
                for item in information
            }
        proposals = service.proposal_store.list_history()
        journal = service.journal.list_changes()
        self.assertEqual(proposals, proposals_before)
        self.assertEqual(journal[: len(journal_before)], journal_before)
        self.assertEqual(apply_receipts[: len(apply_receipts_before)], apply_receipts_before)
        identity_bind_journal = tuple(
            item
            for item in journal[len(journal_before) :]
            if item.operation == "bind_existing"
        )
        self.assertEqual(len(journal) - len(journal_before), 8)
        self.assertEqual(len(identity_bind_journal), 8)
        self.assertFalse(
            any(item.operation == "bind_atomic_information" for item in journal)
        )
        self.assertEqual(
            {item.atomic_information_id for item in identity_bind_journal},
            set(revision_history),
        )
        new_apply_receipts = apply_receipts[len(apply_receipts_before) :]
        self.assertEqual(len(new_apply_receipts), 8)
        identity_receipt_records = [
            json.loads(receipt.payload)["records"][0]
            for receipt in new_apply_receipts
        ]
        self.assertTrue(
            all(
                record["operation"] == "bind_existing"
                for record in identity_receipt_records
            )
        )
        self.assertEqual(
            {item.change_id for item in identity_bind_journal},
            {record["change_id"] for record in identity_receipt_records},
        )
        self.assertEqual(
            {
                record["atomic_information_id"]
                for record in identity_receipt_records
            },
            set(revision_history),
        )
        self.assertEqual(set(effect_bindings), set(revision_history))
        self.assertEqual(
            len(
                {
                    binding["effect_fingerprint"]
                    for binding in effect_bindings.values()
                }
            ),
            8,
        )
        self.assertTrue(
            all(
                binding["atomic_information_id"] == atomic_id
                and binding["revision_count"] == 2
                and binding["journal_count"] == 1
                and binding["apply_receipt_count"] == 1
                for atomic_id, binding in effect_bindings.items()
            )
        )
        self.assertEqual(
            len({item.proposal_id for item in proposals}), len(proposals)
        )
        self.assertEqual(
            len({item.change_id for item in journal}), len(journal)
        )

    def test_failed_two_lane_committed_wave_resumes_without_provider_calls(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider(
            [
                message(1, conversation="committed-wave-a"),
                message(2, conversation="committed-wave-b"),
            ]
        )
        semantic = InterruptedCommittedWaveSemanticHandoff(self.workspace)
        self.semantic = semantic
        service = self.service(capture)

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True, max_terminal_items=2)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        failed = service.run_store.status(run_id)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["failure_category"], "SemanticHandoffError")
        self.assertEqual(
            [item["state"] for item in failed["items"].values()],
            ["represented", "planned"],
        )
        provider_calls = semantic.provider.calls
        atomic_before = service.information_store.list_atomic_information()
        self.assertTrue(atomic_before)

        result = service.run(max_terminal_items=2)

        self.assertEqual(semantic.provider.calls, provider_calls)
        self.assertEqual(semantic.inspect_calls, 1)
        self.assertEqual(result.resume_provider_calls, 0)
        final = service.run_store.status(run_id)
        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["checkpoint_published"])
        self.assertTrue(
            all(
                item["state"] in {"processed", "pending_human"}
                for item in final["items"].values()
            )
        )
        information = service.information_store.list_atomic_information()
        self.assertGreater(len(information), len(atomic_before))
        self.assertEqual(
            len({item.atomic_information_id for item in information}),
            len(information),
        )

    def test_reviewed_head_install_preserves_pristine_suffix_then_resumes_two(
        self,
    ) -> None:
        self.workspace = self.workspace.resolve()
        self.workspace.chmod(0o700)
        for directory in (
            "01_inbox",
            "02_processing",
            "03_information",
            "04_core",
        ):
            (self.workspace / directory).chmod(0o700)
        capture_provider = SyntheticCaptureProvider(
            [
                message(index, conversation=f"install-wave-{index}")
                for index in range(1, 5)
            ]
        )
        runner = SuccessfulV34Runner()
        semantic = RunnerBackedExistingSemanticHandoff(
            workspace=self.workspace,
            runner=runner,
        )
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture_provider,
            semantic_handoff_factory=lambda: semantic,
            interpretation_provider=NoStructuralChangeProvider(),
            semantic_batch_size=50,
            semantic_parallelism=2,
        )
        capture = capture_provider.capture(ZERO_CURSOR)
        plan, _status = service._persist_new_capture_plan(
            capture,
            all_history_upper_bound=capture.upper_bound,
        )
        run_id = str(plan["run_id"])
        binding = service._semantic_authority_binding(run_id)
        authority = _SemanticGlobalAuthority(semantic.service.audit_root)
        _total, inventory_fingerprint, counts = authority._legacy_inventory(())
        manifest = {
            "schema_version": "semantic-handoff-inventory-authority/1.0",
            "artifact_kind": "semantic_handoff_inventory_authority",
            "authority_ref": "sha256:" + "5" * 64,
            "reviewed_git_head": binding.reviewed_git_head,
            "campaign": {
                "created_at": binding.campaign_created_at,
                "lower_cursor": list(binding.campaign_lower_cursor),
                "frozen_global_upper_cursor": list(
                    binding.frozen_global_upper_cursor
                ),
                "capture_provider_version": binding.capture_provider_version,
                "semantic_batch_size": binding.semantic_batch_size,
            },
            "expected_raw_provider_labels": [],
            "historical_provider_version_counts": counts,
            "local_total": 0,
            "legacy_inventory_fingerprint": inventory_fingerprint,
            "baseline_total": 80,
            "max_new": 20,
            "absolute_cap": 100,
        }
        manifest["payload_fingerprint"] = _fingerprint(manifest)
        authority_file = self.workspace / "synthetic-install-authority.json"
        authority_file.write_text(json.dumps(manifest), encoding="utf-8")
        authority_file.chmod(0o600)
        semantic.install_global_authority(
            inventory_authority_file=authority_file,
            window_binding=binding,
        )
        original_persist = semantic.service._persist_audits
        persist_calls = 0

        def interrupt_after_first_package(*args, **kwargs):
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                raise OSError("synthetic package durable interruption")
            return original_persist(*args, **kwargs)

        with patch.object(
            semantic.service,
            "_persist_audits",
            side_effect=interrupt_after_first_package,
        ), self.assertRaises(WechatDigestError):
            service.run(max_terminal_items=2)

        failed = service.run_store.status(run_id)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["failure_category"], "SemanticHandoffError")
        self.assertEqual(
            [item["state"] for item in failed["items"].values()],
            ["represented", "planned", "planned", "planned"],
        )
        inspections = service._inspect_committed_result_wave(
            run_id,
            plan,
            failed,
        )
        assert inspections is not None
        self.assertEqual(
            [item["phase"] for item in inspections.values()],
            ["package_pending_ingestion", "result_pending_package"],
        )
        representation_ids = [
            str(item["representation_id"])
            for item in inspections.values()
        ]
        attempt_summary = semantic.service.global_attempt_summary(
            representation_ids[-1]
        )
        self.assertEqual(attempt_summary["global_attempt_total"], 82)
        self.assertEqual(attempt_summary["global_unknown"], 0)
        run_dir = service.run_store.runs_root / run_id
        run_bytes_before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        business_bytes_before = {
            path.relative_to(self.workspace).as_posix(): path.read_bytes()
            for root in (
                "01_inbox",
                "02_processing/representations",
                "02_processing/information",
                "03_information",
                "04_core",
            )
            for path in (self.workspace / root).rglob("*")
            if path.is_file()
        }
        semantic.reviewed_git_head = "7" * 40

        continuation = service.install_semantic_reviewed_head_continuation(
            authority_ref=(
                "https://github.com/leevi2010-cursor/ArcheOS/issues/188"
                "#issuecomment-5411902640"
            )
        )

        self.assertEqual(continuation["previous_reviewed_git_head"], "6" * 40)
        self.assertEqual(continuation["reviewed_git_head"], "7" * 40)
        self.assertEqual(continuation["activation_total"], 82)
        self.assertEqual(continuation["activation_unknown_count"], 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(service.run_store.status(run_id), failed)
        self.assertEqual(service.run_store.plan(run_id), plan)
        self.assertIsNone(service.run_store.checkpoint())
        self.assertEqual(
            semantic.service.global_attempt_summary(representation_ids[-1]),
            attempt_summary,
        )
        self.assertEqual(
            {
                path.relative_to(run_dir).as_posix(): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            },
            run_bytes_before,
        )
        self.assertEqual(
            {
                path.relative_to(self.workspace).as_posix(): path.read_bytes()
                for root in (
                    "01_inbox",
                    "02_processing/representations",
                    "02_processing/information",
                    "03_information",
                    "04_core",
                )
                for path in (self.workspace / root).rglob("*")
                if path.is_file()
            },
            business_bytes_before,
        )

        resumed = service.run(max_terminal_items=2)

        self.assertEqual(resumed.resume_provider_calls, 0)
        self.assertEqual(resumed.segment_items_completed, 2)
        self.assertFalse(resumed.checkpoint_published)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(
            semantic.service.global_attempt_summary(representation_ids[-1]),
            attempt_summary,
        )
        cursor_path = (
            semantic.service.audit_root
            / "semantic_global_authority"
            / "commit-cursor.json"
        )
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["committed_global_ordinal"], 82)
        resumed_status = service.run_store.status(run_id)
        self.assertEqual(
            [item["state"] for item in resumed_status["items"].values()],
            ["processed", "processed", "planned", "planned"],
        )
        pristine = tuple(resumed_status["items"].values())[2:]
        self.assertTrue(
            all(
                item.get("representation_id") is None
                and item.get("atomic_information_ids") == []
                and item.get("governance_receipt") is None
                for item in pristine
            )
        )
        self.assertEqual(len(service.source_repository.list_sources()), 2)
        self.assertEqual(
            len(service.information_store.list_atomic_information()),
            2,
        )
        self.assertIsNone(service.run_store.checkpoint())

    def test_real_package_result_wave_resumes_one_cursor_at_a_time(self) -> None:
        self.workspace = self.workspace.resolve()
        self.workspace.chmod(0o700)
        for directory in (
            "01_inbox",
            "02_processing",
            "03_information",
            "04_core",
        ):
            (self.workspace / directory).chmod(0o700)
        capture_provider = SyntheticCaptureProvider(
            [
                message(1, conversation="real-wave-a"),
                message(2, conversation="real-wave-b"),
            ]
        )
        runner = SuccessfulV34Runner()
        semantic = RunnerBackedExistingSemanticHandoff(
            workspace=self.workspace,
            runner=runner,
        )
        service = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture_provider,
            semantic_handoff_factory=lambda: semantic,
            interpretation_provider=NoStructuralChangeProvider(),
            semantic_batch_size=50,
            semantic_parallelism=2,
        )
        capture = capture_provider.capture(ZERO_CURSOR)
        plan, _status = service._persist_new_capture_plan(
            capture,
            all_history_upper_bound=capture.upper_bound,
        )
        run_id = str(plan["run_id"])
        binding = service._semantic_authority_binding(run_id)
        authority = _SemanticGlobalAuthority(semantic.service.audit_root)
        _total, inventory_fingerprint, counts = authority._legacy_inventory(())
        manifest = {
            "schema_version": "semantic-handoff-inventory-authority/1.0",
            "artifact_kind": "semantic_handoff_inventory_authority",
            "authority_ref": "sha256:" + "5" * 64,
            "reviewed_git_head": binding.reviewed_git_head,
            "campaign": {
                "created_at": binding.campaign_created_at,
                "lower_cursor": list(binding.campaign_lower_cursor),
                "frozen_global_upper_cursor": list(
                    binding.frozen_global_upper_cursor
                ),
                "capture_provider_version": binding.capture_provider_version,
                "semantic_batch_size": binding.semantic_batch_size,
            },
            "expected_raw_provider_labels": [],
            "historical_provider_version_counts": counts,
            "local_total": 0,
            "legacy_inventory_fingerprint": inventory_fingerprint,
            "baseline_total": 80,
            "max_new": 20,
            "absolute_cap": 100,
        }
        manifest["payload_fingerprint"] = _fingerprint(manifest)
        authority_file = self.workspace / "synthetic-inventory-authority.json"
        authority_file.write_text(json.dumps(manifest), encoding="utf-8")
        authority_file.chmod(0o600)
        semantic.install_global_authority(
            inventory_authority_file=authority_file,
            window_binding=binding,
        )

        original_persist = semantic.service._persist_audits
        persist_calls = 0

        def interrupt_after_first_package(*args, **kwargs):
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                raise OSError("synthetic package durable interruption")
            return original_persist(*args, **kwargs)

        with patch.object(
            semantic.service,
            "_persist_audits",
            side_effect=interrupt_after_first_package,
        ), self.assertRaises(WechatDigestError):
            service.run(max_terminal_items=2)

        failed = service.run_store.status(run_id)
        self.assertEqual(failed["failure_category"], "SemanticHandoffError")
        self.assertEqual(
            [item["state"] for item in failed["items"].values()],
            ["represented", "planned"],
        )
        inspections = service._inspect_committed_result_wave(
            run_id,
            plan,
            failed,
        )
        assert inspections is not None
        self.assertEqual(
            [item["phase"] for item in inspections.values()],
            ["package_pending_ingestion", "result_pending_package"],
        )
        representation_ids = [
            str(item["representation_id"])
            for item in inspections.values()
        ]
        package_root = self.workspace / "02_processing" / "information"
        first_package = package_root / representation_ids[0]
        second_package = package_root / representation_ids[1]
        first_package_bytes = {
            path.relative_to(first_package).as_posix(): path.read_bytes()
            for path in sorted(first_package.rglob("*"))
            if path.is_file()
        }
        self.assertTrue(first_package_bytes)
        self.assertFalse(second_package.exists())
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(service.information_store.list_atomic_information(), ())
        self.assertEqual(len(capture_provider.calls), 1)
        attempt_summary = semantic.service.global_attempt_summary(
            representation_ids[1]
        )
        self.assertEqual(
            attempt_summary,
            {
                "global_attempt_total": 82,
                "global_unknown": 0,
                "next_global_ordinal": 83,
                "absolute_cap": 100,
            },
        )

        run_dir = service.run_store.runs_root / run_id

        def durable_business_bytes() -> dict[str, bytes]:
            roots = (
                "01_inbox",
                "02_processing/representations",
                "02_processing/information",
                "03_information",
                "04_core",
            )
            return {
                path.relative_to(self.workspace).as_posix(): path.read_bytes()
                for root in roots
                for path in (self.workspace / root).rglob("*")
                if path.is_file()
            }

        run_bytes_before_install = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        business_bytes_before_install = durable_business_bytes()
        semantic_runs_before_install = {
            path.relative_to(semantic.service.audit_root).as_posix(): (
                path.read_bytes()
            )
            for path in semantic.service.audit_root.rglob("*")
            if path.is_file()
            and "semantic_global_authority" not in path.parts
        }
        semantic.reviewed_git_head = "7" * 40
        with self.assertRaises(WechatDigestError):
            service._inspect_committed_result_wave(run_id, plan, failed)
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/188"
            "#issuecomment-5411672370"
        )

        continuation = service.install_semantic_reviewed_head_continuation(
            authority_ref=authority_ref
        )

        self.assertEqual(continuation["previous_reviewed_git_head"], "6" * 40)
        self.assertEqual(continuation["reviewed_git_head"], "7" * 40)
        self.assertEqual(continuation["activation_total"], 82)
        self.assertEqual(continuation["activation_unknown_count"], 0)
        self.assertEqual(continuation["next_global_ordinal"], 83)
        self.assertEqual(continuation["authority_ref"], authority_ref)
        self.assertEqual(
            continuation["active_run"],
            {
                "run_id": run_id,
                "plan_fingerprint": failed["plan_fingerprint"],
                "capture_receipt_fingerprint": plan[
                    "capture_receipt_fingerprint"
                ],
                "status_fingerprint": _sha256_bytes(
                    _canonical_json(failed).encode("utf-8")
                ),
            },
        )
        self.assertEqual(
            service.install_semantic_reviewed_head_continuation(
                authority_ref=authority_ref
            ),
            continuation,
        )
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(service.run_store.checkpoint(), None)
        self.assertEqual(
            service.run_store.status(run_id),
            failed,
        )
        self.assertEqual(
            {
                path.relative_to(run_dir).as_posix(): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            },
            run_bytes_before_install,
        )
        self.assertEqual(durable_business_bytes(), business_bytes_before_install)
        self.assertEqual(
            {
                path.relative_to(semantic.service.audit_root).as_posix(): (
                    path.read_bytes()
                )
                for path in semantic.service.audit_root.rglob("*")
                if path.is_file()
                and "semantic_global_authority" not in path.parts
            },
            semantic_runs_before_install,
        )
        self.assertEqual(
            len(
                tuple(
                    (
                        semantic.service.audit_root
                        / "semantic_global_authority"
                    ).glob("reviewed-head-continuation-*.json")
                )
            ),
            1,
        )
        source_ids = [str(item["source_id"]) for item in plan["conversations"]]
        self.assertEqual(len(service.source_repository.list_sources()), 2)
        self.assertEqual(
            sum(
                len(service.representation_repository.list_for_source(source_id))
                for source_id in source_ids
            ),
            2,
        )

        original_update = service._update_item
        interrupted_after_commit = False

        def interrupt_before_semantic_status(*args, **changes):
            nonlocal interrupted_after_commit
            if (
                not interrupted_after_commit
                and "atomic_information_ids" in changes
                and "state" not in changes
            ):
                interrupted_after_commit = True
                raise SemanticHandoffError(
                    "synthetic post-commit status interruption"
                )
            return original_update(*args, **changes)

        with patch.object(
            service,
            "_update_item",
            side_effect=interrupt_before_semantic_status,
        ), self.assertRaises(WechatDigestError):
            service.run(max_terminal_items=1)

        self.assertTrue(interrupted_after_commit)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(capture_provider.calls), 1)
        after_commit = service.run_store.status(run_id)
        self.assertEqual(after_commit["state"], "failed")
        self.assertEqual(
            after_commit["failure_category"],
            "SemanticHandoffError",
        )
        self.assertEqual(
            [item["state"] for item in after_commit["items"].values()],
            ["represented", "planned"],
        )
        cursor_path = (
            semantic.service.audit_root
            / "semantic_global_authority"
            / "commit-cursor.json"
        )
        self.assertTrue(cursor_path.exists())
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["committed_global_ordinal"], 81)
        after_commit_inspections = service._inspect_committed_result_wave(
            run_id,
            plan,
            after_commit,
        )
        assert after_commit_inspections is not None
        self.assertEqual(
            [item["phase"] for item in after_commit_inspections.values()],
            ["already_ingested_pending_status", "result_pending_package"],
        )
        self.assertEqual(
            {
                path.relative_to(first_package).as_posix(): path.read_bytes()
                for path in sorted(first_package.rglob("*"))
                if path.is_file()
            },
            first_package_bytes,
        )
        self.assertFalse(second_package.exists())
        first_atomic = service.information_store.list_atomic_information()
        self.assertEqual(len(first_atomic), 1)
        self.assertTrue(
            all(
                item.get("governance_receipt") is None
                for item in after_commit["items"].values()
            )
        )

        leading_prefix_run_bytes = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        leading_prefix_business_bytes = durable_business_bytes()
        cursor_bytes = cursor_path.read_bytes()
        semantic.reviewed_git_head = "8" * 40
        leading_prefix_continuation = (
            service.install_semantic_reviewed_head_continuation(
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/188"
                    "#issuecomment-5411672371"
                )
            )
        )
        self.assertEqual(
            leading_prefix_continuation["previous_reviewed_git_head"],
            "7" * 40,
        )
        self.assertEqual(
            leading_prefix_continuation["reviewed_git_head"],
            "8" * 40,
        )
        self.assertEqual(leading_prefix_continuation["activation_total"], 82)
        self.assertEqual(leading_prefix_continuation["next_global_ordinal"], 83)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(cursor_path.read_bytes(), cursor_bytes)
        self.assertEqual(service.run_store.status(run_id), after_commit)
        self.assertEqual(
            {
                path.relative_to(run_dir).as_posix(): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            },
            leading_prefix_run_bytes,
        )
        self.assertEqual(durable_business_bytes(), leading_prefix_business_bytes)

        resumed = service.run(max_terminal_items=2)

        self.assertEqual(resumed.resume_provider_calls, 0)
        self.assertEqual(resumed.segment_items_completed, 2)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(capture_provider.calls), 1)
        self.assertEqual(
            semantic.service.global_attempt_summary(representation_ids[1]),
            attempt_summary,
        )
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["committed_global_ordinal"], 82)
        final = service.run_store.status(run_id)
        final_items_by_representation = {
            item.get("representation_id"): item
            for item in final["items"].values()
        }
        self.assertEqual(
            set(final_items_by_representation),
            set(representation_ids),
        )
        self.assertTrue(
            all(
                item["state"] in TERMINAL_ITEM_STATES
                and item.get("governance_receipt") is not None
                for item in final_items_by_representation.values()
            )
        )
        self.assertTrue(second_package.is_dir())
        self.assertEqual(
            {
                path.relative_to(first_package).as_posix(): path.read_bytes()
                for path in sorted(first_package.rglob("*"))
                if path.is_file()
            },
            first_package_bytes,
        )
        information = service.information_store.list_atomic_information()
        self.assertEqual(len(information), 2)
        self.assertEqual(
            len({item.atomic_information_id for item in information}),
            2,
        )
        self.assertEqual(len(service.source_repository.list_sources()), 2)
        self.assertEqual(
            sum(
                len(service.representation_repository.list_for_source(source_id))
                for source_id in source_ids
            ),
            2,
        )
        self.assertEqual(service.proposal_store.list_history(), ())
        self.assertEqual(service.journal.list_changes(), ())
        self.assertEqual(service.run_store.checkpoint(), capture.upper_bound)
        service._verify_plan_and_status(run_id, capture, plan, final)

        reopened_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest"
        )
        reopened = WechatDigestService(
            workspace=self.workspace,
            capture_provider=capture_provider,
            semantic_handoff_factory=lambda: semantic,
            interpretation_provider=NoStructuralChangeProvider(),
            semantic_batch_size=50,
            semantic_parallelism=2,
            run_store=reopened_store,
        )
        reopened_plan = reopened_store.plan(run_id)
        reopened_status = reopened_store.status(run_id)
        reopened_capture, _receipt = reopened_store.load_capture_artifacts(
            run_id,
            plan=reopened_plan,
        )
        reopened._verify_plan_and_status(
            run_id,
            reopened_capture,
            reopened_plan,
            reopened_status,
        )
        self.assertEqual(reopened_store.checkpoint(), capture.upper_bound)
        self.assertEqual(len(capture_provider.calls), 1)

    def test_committed_wave_drift_fails_before_recovery_status_write(self) -> None:
        for case in ("phase", "ordinal", "status"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary) / "workspace"
                for directory in (
                    "01_inbox",
                    "02_processing",
                    "03_information",
                    "04_core",
                ):
                    (workspace / directory).mkdir(parents=True, exist_ok=True)
                capture = SyntheticCaptureProvider(
                    [
                        message(1, conversation=f"{case}-a"),
                        message(2, conversation=f"{case}-b"),
                    ]
                )
                semantic = InterruptedCommittedWaveSemanticHandoff(workspace)
                service = WechatDigestService(
                    workspace=workspace,
                    capture_provider=capture,
                    semantic_handoff_factory=lambda: semantic,
                    interpretation_provider=NoStructuralChangeProvider(),
                )
                with self.assertRaises(WechatDigestError):
                    service.run(all_history=True, max_terminal_items=2)
                run_id = service.run_store.active_run_id()
                assert run_id is not None
                status_path = service.run_store.runs_root / run_id / "status.json"
                if case == "status":
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    next(
                        item
                        for item in status["items"].values()
                        if item["state"] == "planned"
                    )["atomic_information_ids"] = [
                        "atomic_info_" + "0" * 32
                    ]
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                else:
                    semantic.inspection_drift = case
                before = status_path.read_bytes()
                provider_calls = semantic.provider.calls

                with self.assertRaises(WechatDigestError):
                    service.run(max_terminal_items=2)

                self.assertEqual(status_path.read_bytes(), before)
                self.assertEqual(semantic.provider.calls, provider_calls)
                self.assertIsNone(service.run_store.checkpoint())

    def test_reviewed_head_continuation_rejects_invalid_failed_wave_before_write(
        self,
    ) -> None:
        for case in (
            "phase",
            "ordinal",
            "status",
            "mixed",
            "pre_provider",
            "other_failure",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary) / "workspace"
                for directory in (
                    "01_inbox",
                    "02_processing",
                    "03_information",
                    "04_core",
                ):
                    (workspace / directory).mkdir(parents=True, exist_ok=True)
                capture = SyntheticCaptureProvider(
                    [
                        message(1, conversation=f"install-{case}-a"),
                        message(2, conversation=f"install-{case}-b"),
                    ]
                )
                semantic = InterruptedCommittedWaveSemanticHandoff(workspace)
                service = WechatDigestService(
                    workspace=workspace,
                    capture_provider=capture,
                    semantic_handoff_factory=lambda: semantic,
                    interpretation_provider=NoStructuralChangeProvider(),
                )
                with self.assertRaises(WechatDigestError):
                    service.run(all_history=True, max_terminal_items=2)
                run_id = service.run_store.active_run_id()
                assert run_id is not None
                plan = service.run_store.plan(run_id)
                status_path = service.run_store.runs_root / run_id / "status.json"
                if case == "status":
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    next(
                        item
                        for item in status["items"].values()
                        if item["state"] == "planned"
                    )["atomic_information_ids"] = [
                        "atomic_info_" + "0" * 32
                    ]
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                elif case == "other_failure":
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    status["failure_category"] = "WechatDigestError"
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                else:
                    semantic.inspection_drift = case
                semantic.reviewed_git_head = "7" * 40
                semantic.campaign_binding = SimpleNamespace(
                    created_at=plan["created_at"],
                    lower_cursor=service._cursor_tuple(
                        WechatCursor.from_dict(plan["after_cursor"])
                    ),
                    frozen_global_upper_cursor=service._cursor_tuple(
                        WechatCursor.from_dict(plan["all_history_upper_bound"])
                    ),
                    capture_provider_version=plan["provider_version"],
                    semantic_batch_size=plan["semantic_batch_size"],
                    reviewed_git_head="6" * 40,
                )
                before = status_path.read_bytes()
                provider_calls = semantic.provider.calls
                capture_calls = len(capture.calls)

                with self.assertRaises(WechatDigestError):
                    service.install_semantic_reviewed_head_continuation(
                        authority_ref=(
                            "https://github.com/leevi2010-cursor/ArcheOS/"
                            "issues/188#issuecomment-5411672370"
                        )
                    )

                self.assertEqual(status_path.read_bytes(), before)
                self.assertEqual(semantic.provider.calls, provider_calls)
                self.assertEqual(len(capture.calls), capture_calls)
                self.assertEqual(
                    semantic.installed_reviewed_head_continuations,
                    [],
                )
                self.assertIsNone(service.run_store.checkpoint())

    def test_prepare_requires_active_run_without_capture(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        with self.assertRaisesRegex(WechatDigestError, "不存在可恢复"):
            self.service(capture).prepare_next_semantic()
        self.assertEqual(capture.calls, [])

    def test_prepare_is_idempotent_and_keeps_checkpoint_unpublished(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        first = service.prepare_next_semantic()
        second = service.prepare_next_semantic()
        self.assertEqual(first, second)
        self.assertEqual(self.semantic.provider.calls, 0)
        status = service.run_store.status(first.run_id)
        self.assertFalse(status["checkpoint_published"])
        self.assertEqual(service.run_store.active_run_id(), first.run_id)

    def test_unknown_resolution_marks_one_item_failed_closed_and_skips_it(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider(
            [
                message(1, conversation="failed"),
                message(2, conversation="next"),
            ]
        )
        service = self.service(capture)
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        prepared = service.prepare_next_semantic()
        status = service.run_store.status(prepared.run_id)
        item_id = next(
            key
            for key, value in status["items"].items()
            if value.get("representation_id") == prepared.representation_id
        )
        manifest_path = self.workspace / "private-unknown-authority.json"
        manifest_path.write_text(
            json.dumps({"digest": {"item_id": item_id}}), encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
        provider_calls = self.semantic.provider.calls
        resolution = service.resolve_semantic_unknown(
            authority_manifest_file=manifest_path
        )
        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertEqual(resolution["global_ordinal"], 166)
        failed_status = service.run_store.status(prepared.run_id)
        failed_item = failed_status["items"][item_id]
        self.assertEqual(failed_item["state"], "failed_closed")
        self.assertEqual(failed_item["atomic_information_ids"], [])
        self.assertFalse(failed_item["pending_human"])
        self.assertEqual(failed_item["context_object_ids"], [])
        self.assertFalse(
            (
                self.workspace
                / "02_processing"
                / "information"
                / prepared.representation_id
            ).exists()
        )
        self.assertEqual(
            service.resolve_semantic_unknown(
                authority_manifest_file=manifest_path
            ),
            resolution,
        )
        self.assertEqual(self.semantic.provider.calls, provider_calls)

        result = service.run()
        self.assertEqual(self.semantic.provider.calls, provider_calls + 1)
        self.assertEqual(result.failed_closed, 1)
        self.assertEqual(result.semantic_preserved_but_unabsorbed, 1)
        self.assertEqual(result.governance_preserved_but_incomplete, 0)
        self.assertTrue(result.checkpoint_published)
        self.assertIsNone(service.run_store.active_run_id())

    def test_unknown_resolution_rejects_representation_drift_before_status_write(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider([message(1, conversation="failed")])
        service = self.service(capture)
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        prepared = service.prepare_next_semantic()
        status = service.run_store.status(prepared.run_id)
        item_id = next(
            key
            for key, value in status["items"].items()
            if value.get("representation_id") == prepared.representation_id
        )
        manifest_path = self.workspace / "private-unknown-authority.json"
        manifest_path.write_text(
            json.dumps({"digest": {"item_id": item_id}}), encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
        item = status["items"][item_id]
        representation_manifest_path = (
            self.workspace
            / "02_processing"
            / "representations"
            / item["source_id"]
            / prepared.representation_id
            / "manifest.json"
        )
        original_manifest = representation_manifest_path.read_bytes()
        parsed_manifest = json.loads(original_manifest)
        artifact_path = (
            representation_manifest_path.parent
            / parsed_manifest["artifacts"][0]["locator"]
        )
        original_artifact = artifact_path.read_bytes()
        provider_calls = self.semantic.provider.calls

        def generated_at_drift() -> None:
            payload = json.loads(original_manifest)
            payload["representation"]["generated_at"] = (
                "2026-08-20T00:00:00.000Z"
            )
            representation_manifest_path.write_text(
                json.dumps(payload), encoding="utf-8"
            )

        def inventory_drift() -> None:
            payload = json.loads(original_manifest)
            artifact_id = payload["artifacts"][0]["artifact_id"]
            payload["artifacts"][0]["artifact_id"] = (
                artifact_id[:-1]
                + ("0" if artifact_id[-1] != "0" else "1")
            )
            representation_manifest_path.write_text(
                json.dumps(payload), encoding="utf-8"
            )

        def artifact_bytes_drift() -> None:
            artifact_path.write_bytes(original_artifact + b"drift")

        for label, mutate in (
            ("generated_at", generated_at_drift),
            ("inventory", inventory_drift),
            ("artifact_bytes", artifact_bytes_drift),
        ):
            with self.subTest(label=label):
                self.semantic.before_unknown_commit = mutate
                try:
                    with self.assertRaises(WechatDigestError):
                        service.resolve_semantic_unknown(
                            authority_manifest_file=manifest_path
                        )
                finally:
                    representation_manifest_path.write_bytes(original_manifest)
                    artifact_path.write_bytes(original_artifact)
                    self.semantic.before_unknown_commit = None
                self.assertEqual(
                    service.run_store.status(prepared.run_id), status
                )
                self.assertIsNone(self.semantic.unknown_resolution)
                self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_timeout_212_resolution_marks_item_preserved_without_provider(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider(
            [message(1, conversation="timeout-212")]
        )
        service = self.service(capture)
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        prepared = service.prepare_next_semantic()
        status = service.run_store.status(prepared.run_id)
        item_id = next(
            key
            for key, value in status["items"].items()
            if value.get("representation_id") == prepared.representation_id
        )
        manifest_path = self.workspace / "private-timeout-212-authority.json"
        manifest_path.write_text(
            json.dumps({"digest": {"item_id": item_id}}),
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        provider_calls = self.semantic.provider.calls
        resolution = service.resolve_semantic_timeout_212(
            authority_manifest_file=manifest_path
        )
        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertEqual(resolution["global_ordinal"], 212)
        failed_item = service.run_store.status(prepared.run_id)["items"][
            item_id
        ]
        self.assertEqual(failed_item["state"], "failed_closed")
        self.assertEqual(
            failed_item["semantic_failure"]["failure_category"], "timeout"
        )
        self.assertEqual(failed_item["atomic_information_ids"], [])
        self.assertFalse(failed_item["pending_human"])
        self.assertEqual(failed_item["context_object_ids"], [])
        self.assertFalse(
            (
                self.workspace
                / "02_processing"
                / "information"
                / prepared.representation_id
            ).exists()
        )
        self.assertEqual(
            service.resolve_semantic_timeout_212(
                authority_manifest_file=manifest_path
            ),
            resolution,
        )
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_generic_attempt_resolution_preserves_other_items_and_business_state(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider(
            [
                message(index, conversation=f"conversation-{index:02d}")
                for index in range(1, 124)
            ]
        )
        service = self.service(capture)
        self.semantic.global_attempt_total = 371
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        plan = service.run_store.plan(run_id)
        status = service.run_store.status(run_id)
        status = {**status, "failure_category": "SemanticHandoffError"}
        represented_ids = [
            item_id
            for item_id, value in status["items"].items()
            if value.get("state") == "represented"
        ]
        self.assertEqual(len(represented_ids), 1)
        target_id = represented_ids[0]
        terminal_distribution = (
            ["pending_human"] * 10
            + ["processed"] * 8
            + ["unsupported"] * 89
            + ["planned"] * 15
        )
        updated_items = dict(status["items"])
        for item_id, state in zip(
            (key for key in updated_items if key != target_id),
            terminal_distribution,
            strict=True,
        ):
            updated_items[item_id] = {
                **updated_items[item_id],
                "state": state,
            }
        status = {**status, "items": updated_items}
        service.run_store.update_status(run_id, status)
        status_verifier = patch.object(service, "_verify_plan_and_status")
        status_verifier.start()
        self.addCleanup(status_verifier.stop)
        represented = [
            (item_id, item)
            for item_id, item in status["items"].items()
            if item.get("state") == "represented"
        ]
        self.assertEqual(len(represented), 1)
        item_id, item = represented[0]
        self.assertEqual(
            Counter(
                value.get("state")
                for key, value in status["items"].items()
                if key != item_id
            ),
            Counter(
                {
                    "pending_human": 10,
                    "processed": 8,
                    "unsupported": 89,
                    "planned": 15,
                }
            ),
        )
        pre_status_fingerprint = _sha256_bytes(
            _canonical_json(status).encode("utf-8")
        )
        binding = service._attempt_resolution_digest_binding(
            run_id=run_id,
            plan=plan,
            item_id=item_id,
            item=item,
            pre_status_fingerprint=pre_status_fingerprint,
        )
        manifest_path = self.workspace / "private-attempt-authority.json"
        extra_nonterminal_id = next(
            key
            for key, value in status["items"].items()
            if key != item_id and value.get("state") == "planned"
        )
        damaged_items = dict(status["items"])
        damaged_items[extra_nonterminal_id] = {
            **damaged_items[extra_nonterminal_id],
            "state": "represented",
        }
        service.run_store.update_status(
            run_id, {**status, "items": damaged_items}
        )
        with self.assertRaisesRegex(WechatDigestError, "唯一绑定"):
            service.build_semantic_attempt_resolution_manifest(
                candidate_file=manifest_path,
                authority_ref=(
                    "https://github.com/leevi2010-cursor/ArcheOS/issues/184"
                    "#issuecomment-5407691736"
                ),
                observed_at="2026-08-25T12:00:00Z",
            )
        service.run_store.update_status(run_id, status)
        candidate = service.build_semantic_attempt_resolution_manifest(
            candidate_file=manifest_path,
            authority_ref=(
                "https://github.com/leevi2010-cursor/ArcheOS/issues/184"
                "#issuecomment-5407349371"
            ),
            observed_at="2026-08-25T12:00:00Z",
        )
        self.assertEqual(candidate["digest"], binding)
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.semantic.provider.calls, 0)
        plan_before = service.run_store.plan(run_id)
        capture_before = service.run_store.capture_receipt(run_id)
        checkpoint_before = service.run_store.checkpoint()
        source_before = service.source_repository.get(item["source_id"])
        representation_before = service.representation_repository.get(
            item["representation_id"]
        )
        provider_calls = self.semantic.provider.calls

        plan_path = service.run_store.runs_root / run_id / "plan.json"
        plan_bytes = plan_path.read_bytes()
        representation_manifest_path = (
            self.workspace
            / "02_processing"
            / "representations"
            / item["source_id"]
            / item["representation_id"]
            / "manifest.json"
        )
        representation_manifest = json.loads(
            representation_manifest_path.read_text("utf-8")
        )
        artifact_path = (
            representation_manifest_path.parent
            / representation_manifest["artifacts"][0]["locator"]
        )
        artifact_bytes = artifact_path.read_bytes()
        checkpoint_path = service.run_store.checkpoint_path

        def drift_plan() -> None:
            damaged = json.loads(plan_bytes)
            damaged["created_at"] = "2026-08-25T13:00:00Z"
            plan_path.write_text(json.dumps(damaged), encoding="utf-8")

        def drift_representation() -> None:
            artifact_path.write_bytes(artifact_bytes + b"drift")

        def drift_checkpoint() -> None:
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "schema_version": "wechat-digest-checkpoint/1.0",
                        "cursor": plan["upper_bound"],
                        "published_at": "2026-08-25T13:00:00Z",
                        "run_id": run_id,
                    }
                ),
                encoding="utf-8",
            )

        for label, mutate, restore in (
            ("plan", drift_plan, lambda: plan_path.write_bytes(plan_bytes)),
            (
                "representation",
                drift_representation,
                lambda: artifact_path.write_bytes(artifact_bytes),
            ),
            (
                "checkpoint",
                drift_checkpoint,
                lambda: checkpoint_path.unlink(missing_ok=True),
            ),
        ):
            with self.subTest(drift=label):
                self.semantic.before_unknown_commit = mutate
                try:
                    with self.assertRaises(WechatDigestError):
                        service.resolve_semantic_attempt(
                            authority_manifest_file=manifest_path
                        )
                finally:
                    restore()
                    self.semantic.before_unknown_commit = None
                self.assertEqual(service.run_store.status(run_id), status)
                self.assertIsNone(self.semantic.attempt_resolution)
                self.assertEqual(self.semantic.provider.calls, provider_calls)

        receipt = service.resolve_semantic_attempt(
            authority_manifest_file=manifest_path
        )

        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertEqual(receipt["global_ordinal"], 371)
        final_status = service.run_store.status(run_id)
        final_item = final_status["items"][item_id]
        self.assertEqual(final_status["state"], "processing")
        self.assertIsNone(final_status["failure_category"])
        self.assertEqual(final_item["state"], "failed_closed")
        self.assertTrue(
            final_item["semantic_failure"]["preserved_but_unabsorbed"]
        )
        self.assertEqual(
            Counter(
                value.get("state")
                for key, value in final_status["items"].items()
                if key != item_id
            ),
            Counter(
                {
                    "pending_human": 10,
                    "processed": 8,
                    "unsupported": 89,
                    "planned": 15,
                }
            ),
        )
        self.assertEqual(service.run_store.plan(run_id), plan_before)
        self.assertEqual(service.run_store.capture_receipt(run_id), capture_before)
        self.assertEqual(service.run_store.checkpoint(), checkpoint_before)
        self.assertEqual(
            service.source_repository.get(item["source_id"]), source_before
        )
        self.assertEqual(
            service.representation_repository.get(item["representation_id"]),
            representation_before,
        )
        self.assertEqual(
            service.resolve_semantic_attempt(
                authority_manifest_file=manifest_path
            ),
            receipt,
        )
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_generic_attempt_resolution_accepts_any_planned_tail_count(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider(
            [
                message(index, conversation=f"small-{index:02d}")
                for index in range(1, 4)
            ]
        )
        service = self.service(capture)
        self.semantic.global_attempt_total = 500
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        status = service.run_store.status(run_id)
        status = {**status, "failure_category": "SemanticHandoffError"}
        service.run_store.update_status(run_id, status)
        represented = [
            item_id
            for item_id, value in status["items"].items()
            if value.get("state") == "represented"
        ]
        self.assertEqual(len(represented), 1)
        candidate_path = self.workspace / "small-attempt-authority.json"
        service.build_semantic_attempt_resolution_manifest(
            candidate_file=candidate_path,
            authority_ref=(
                "https://github.com/leevi2010-cursor/ArcheOS/issues/999"
                "#issuecomment-5407349999"
            ),
            observed_at="2026-08-25T13:00:00Z",
        )
        receipt = service.resolve_semantic_attempt(
            authority_manifest_file=candidate_path
        )
        final = service.run_store.status(run_id)
        self.assertEqual(receipt["global_ordinal"], 500)
        self.assertEqual(
            [
                value.get("state")
                for key, value in final["items"].items()
                if key != represented[0]
            ],
            ["planned", "planned"],
        )
        self.assertEqual(self.semantic.provider.calls, 0)
        business_before = self.governance_business_state(service)
        processed = service.run()
        self.assertTrue(processed.checkpoint_published)
        self.assertIsNotNone(service.run_store.checkpoint())
        self.assertTrue(
            any(
                revision.origin_source_id
                != final["items"][represented[0]]["source_id"]
                for revision in service.information_store.list_atomic_information()
            )
        )
        self.assertNotEqual(
            self.governance_business_state(service), business_before
        )
        provider_calls = self.semantic.provider.calls
        completed_status = service.run_store.status(run_id)
        service._load_active_capture_artifacts(
            run_id,
            service.run_store.plan(run_id),
            completed_status,
        )
        self.assertEqual(self.semantic.provider.calls, provider_calls)

        target_id = represented[0]
        target_item = completed_status["items"][target_id]
        source_id = target_item["source_id"]
        representation_id = target_item["representation_id"]
        plan = service.run_store.plan(run_id)

        damaged_status = json.loads(json.dumps(completed_status))
        damaged_status["items"][target_id]["semantic_failure"][
            "preserved_but_unabsorbed"
        ] = False
        service.run_store.update_status(run_id, damaged_status)
        with self.assertRaises(WechatDigestError):
            service._load_active_capture_artifacts(
                run_id, plan, service.run_store.status(run_id)
            )
        service.run_store.update_status(run_id, completed_status)

        original_source_get = service.source_repository.get
        source = original_source_get(source_id)
        with patch.object(
            service.source_repository,
            "get",
            side_effect=lambda candidate: (
                replace(source, content_hash="sha256:" + "0" * 64)
                if candidate == source_id
                else original_source_get(candidate)
            ),
        ), self.assertRaises(WechatDigestError):
            service._load_active_capture_artifacts(run_id, plan, completed_status)

        original_representation_get = service.representation_repository.get
        representation = original_representation_get(representation_id)
        with patch.object(
            service.representation_repository,
            "get",
            side_effect=lambda candidate: (
                replace(representation, source_id="src_" + "0" * 32)
                if candidate == representation_id
                else original_representation_get(candidate)
            ),
        ), self.assertRaises(WechatDigestError):
            service._load_active_capture_artifacts(run_id, plan, completed_status)

        original_read_artifact = service.representation_repository.read_artifact
        with patch.object(
            service.representation_repository,
            "read_artifact",
            side_effect=lambda candidate, artifact_id: (
                original_read_artifact(candidate, artifact_id) + b"drift"
                if candidate == representation_id
                else original_read_artifact(candidate, artifact_id)
            ),
        ), self.assertRaises(WechatDigestError):
            service._load_active_capture_artifacts(run_id, plan, completed_status)

        assert self.semantic.attempt_resolution is not None
        original_receipt = self.semantic.attempt_resolution
        damaged_receipt = json.loads(json.dumps(original_receipt))
        damaged_receipt["digest"]["source_fingerprint"] = (
            "sha256:" + "0" * 64
        )
        self.semantic.attempt_resolution = damaged_receipt
        try:
            with self.assertRaises(WechatDigestError):
                service._load_active_capture_artifacts(
                    run_id, plan, completed_status
                )
        finally:
            self.semantic.attempt_resolution = original_receipt
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_prepare_replays_existing_package_without_provider(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1, conversation="a"), message(2, conversation="b")])
        service = self.service(capture, service_type=FailAfterGovernanceOnceService)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        provider_calls = self.semantic.provider.calls
        prepared = service.prepare_next_semantic()
        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertFalse(service.run_store.status(prepared.run_id)["checkpoint_published"])

    def test_prepare_converges_unsupported_attachment_without_provider(self) -> None:
        attachment_path = self.workspace / "synthetic.bin"
        attachment_path.write_bytes(b"synthetic")
        capture = SyntheticCaptureProvider([message(1, attachments=(attachment(attachment_path, "attachment_1", media_type="application/x-synthetic"),)), message(2, conversation="a"), message(3, conversation="b")])
        self.create_object()
        service = self.service(capture, service_type=FailAfterGovernanceOnceService)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        provider_calls = self.semantic.provider.calls
        prepared = service.prepare_next_semantic()
        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertEqual(service.run_store.status(prepared.run_id)["items"]["attachment:attachment_1"]["state"], "unsupported")

    def test_prepare_returns_first_canonical_batch_for_multi_batch_item(self) -> None:
        capture = SyntheticCaptureProvider([message(number) for number in range(1, 42)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        prepared = service.prepare_next_semantic(batch_size=40)
        self.assertEqual(len(prepared.anchor_unit_ids), 40)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        self.assertEqual(next(iter(service.run_store.status(run_id)["items"].values()))["state"], "represented")
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_prepare_ignores_connector_drift_and_uses_durable_capture(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        calls_before_prepare = len(capture.calls)
        capture.messages[0] = replace(capture.messages[0], visible_content="changed")
        prepared = service.prepare_next_semantic()
        self.assertTrue(prepared.representation_id)
        self.assertEqual(len(capture.calls), calls_before_prepare)
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_prepare_rejects_tampered_plan_and_status_before_provider(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        plan_path = service.run_store.runs_root / run_id / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["conversations"][0]["content_hash"] = "sha256:" + "0" * 64
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(WechatDigestError, "receipt"):
            service.prepare_next_semantic()
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_semantic_batch_size_is_durable_and_legacy_is_40(self) -> None:
        capture = SyntheticCaptureProvider([message(number) for number in range(1, 42)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        plan_path = service.run_store.runs_root / run_id / "plan.json"
        plan = json.loads(plan_path.read_text())
        self.assertEqual(plan["semantic_batch_size"], 40)
        with self.assertRaisesRegex(WechatDigestError, "batch size"):
            service.prepare_next_semantic(batch_size=100)
        plan.pop("semantic_batch_size")
        plan["schema_version"] = "wechat-digest-run-plan/1.0"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(WechatDigestError, "显式升级"):
            service.prepare_next_semantic(batch_size=40)

    def _make_active_legacy_run(
        self, capture: SyntheticCaptureProvider, *, run_store: WechatDigestRunStore | None = None
    ) -> tuple[WechatDigestService, str]:
        self.semantic.failures_remaining = 1
        service = self.service(capture, run_store=run_store)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        run_dir = service.run_store.runs_root / run_id
        plan_path = run_dir / "plan.json"
        status_path = run_dir / "status.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["schema_version"] = "wechat-digest-run-plan/1.0"
        plan.pop("semantic_batch_size")
        plan.pop("all_history_upper_bound")
        plan.pop("capture_receipt_fingerprint", None)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.pop("plan_fingerprint")
        status_path.write_text(json.dumps(status), encoding="utf-8")
        (run_dir / "run-plan-receipt.json").unlink()
        return service, run_id

    def _make_active_v2_run(
        self,
        capture: SyntheticCaptureProvider,
        *,
        run_store: WechatDigestRunStore | None = None,
    ) -> tuple[WechatDigestService, str]:
        self.semantic.failures_remaining = 1
        service = self.service(capture, run_store=run_store)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        run_dir = service.run_store.runs_root / run_id
        plan_path = run_dir / "plan.json"
        status_path = run_dir / "status.json"
        receipt_path = run_dir / "run-plan-receipt.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["schema_version"] = "wechat-digest-run-plan/2.0"
        plan.pop("all_history_upper_bound")
        plan.pop("capture_receipt_fingerprint", None)
        plan.pop("capture_receipt_fingerprint", None)
        fingerprint = _plan_fingerprint(plan)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["plan_fingerprint"] = fingerprint
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["plan_fingerprint"] = fingerprint
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        status_path.write_text(json.dumps(status), encoding="utf-8")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return service, run_id

    def _make_active_v2_processed_run(
        self,
        *,
        mode: str,
        messages: list[CapturedMessage],
        suffix: str,
        protocol_version: str = EXTERNAL_AGENT_PROTOCOL_V1,
        profiled_v1: bool = False,
    ) -> tuple[WechatDigestService, SyntheticSemanticHandoff, str]:
        workspace = Path(self.temporary.name) / f"workspace_{suffix}"
        for directory in (
            "01_inbox",
            "02_processing",
            "03_information",
            "04_core",
        ):
            (workspace / directory).mkdir(parents=True, exist_ok=True)
        semantic = SyntheticSemanticHandoff(workspace)
        semantic.provider.mode = mode
        semantic.protocol_version = protocol_version
        semantic.profiled_v1 = profiled_v1
        with SQLiteWorldModelRepository(
            workspace / "04_core" / "archeos.sqlite3"
        ) as repository:
            repository.create_object("Synthetic Project")
        failures = [True]

        def interrupt_checkpoint() -> None:
            if failures.pop():
                raise RuntimeError("synthetic post-semantic interruption")

        run_store = WechatDigestRunStore(
            workspace / "02_processing" / "wechat_digest",
            before_checkpoint_publish=interrupt_checkpoint,
        )
        service = WechatDigestService(
            workspace=workspace,
            capture_provider=SyntheticCaptureProvider(messages),
            semantic_handoff_factory=lambda: semantic,
            interpretation_provider=NoStructuralChangeProvider(),
            run_store=run_store,
        )
        with self.assertRaisesRegex(WechatDigestError, "安全完成"):
            service.run(all_history=True)
        run_store.before_checkpoint_publish = None
        run_id = run_store.active_run_id()
        assert run_id is not None
        run_dir = run_store.runs_root / run_id
        plan_path = run_dir / "plan.json"
        status_path = run_dir / "status.json"
        receipt_path = run_dir / "run-plan-receipt.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["schema_version"] = "wechat-digest-run-plan/2.0"
        plan.pop("all_history_upper_bound")
        plan.pop("capture_receipt_fingerprint", None)
        fingerprint = _plan_fingerprint(plan)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["plan_fingerprint"] = fingerprint
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["plan_fingerprint"] = fingerprint
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        status_path.write_text(json.dumps(status), encoding="utf-8")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return service, semantic, run_id

    def test_active_v2_all_history_requires_explicit_zero_provider_upgrade(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        service, _ = self._make_active_v2_run(capture)
        provider_calls = self.semantic.provider.calls
        with self.assertRaisesRegex(WechatDigestError, "显式冻结"):
            service.run()
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_active_v2_upgrade_freezes_upper_and_receipt_binds_scope(self) -> None:
        day = 24 * 60 * 60
        capture = SyntheticCaptureProvider(
            [message(1)], window_seconds=30 * day
        )
        service, run_id = self._make_active_v2_run(capture)
        capture.messages.append(
            message(2, timestamp=1_700_000_000 + 31 * day)
        )
        provider_calls = self.semantic.provider.calls
        self.assertEqual(service.upgrade_active_v2_all_history(), run_id)
        plan = service.run_store.plan(run_id)
        status = service.run_store.status(run_id)
        receipt = service.run_store.plan_receipt(run_id)
        self.assertEqual(plan["schema_version"], "wechat-digest-run-plan/3.0")
        self.assertEqual(
            plan["all_history_upper_bound"], capture.messages[-1].cursor.to_dict()
        )
        self.assertEqual(status["plan_fingerprint"], _plan_fingerprint(plan))
        self.assertEqual(receipt["plan_fingerprint"], _plan_fingerprint(plan))
        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertEqual(service.upgrade_active_v2_all_history(), run_id)
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_active_v2_upgrade_accepts_strict_candidate_residue_receipts(
        self,
    ) -> None:
        cases = (
            ("all_residue", [message(1)], 0),
            ("all_candidate", [message(1)], 1),
            ("mixed", [message(1), message(2)], 1),
        )
        for mode, messages, expected_ids in cases:
            with self.subTest(mode=mode):
                service, semantic, run_id = self._make_active_v2_processed_run(
                    mode=mode, messages=messages, suffix=mode
                )
                calls = semantic.provider.calls
                self.assertEqual(
                    service.upgrade_active_v2_all_history(), run_id
                )
                status = service.run_store.status(run_id)
                item = next(iter(status["items"].values()))
                self.assertEqual(
                    len(item["atomic_information_ids"]), expected_ids
                )
                self.assertEqual(semantic.provider.calls, calls)

    def test_active_v2_upgrade_accepts_historical_audit_contracts(self) -> None:
        protocols = (
            (EXTERNAL_AGENT_PROTOCOL_V1, False),
            (EXTERNAL_AGENT_PROTOCOL_V1, True),
            (EXTERNAL_AGENT_PROTOCOL_V2, False),
            (EXTERNAL_AGENT_PROTOCOL_V3, False),
            (EXTERNAL_AGENT_PROTOCOL_V3_1, False),
            (EXTERNAL_AGENT_PROTOCOL_V3_2, False),
            (EXTERNAL_AGENT_PROTOCOL_V3_3, False),
            (EXTERNAL_AGENT_PROTOCOL_V3_4, False),
        )
        for index, (protocol_version, profiled_v1) in enumerate(protocols):
            with self.subTest(protocol=protocol_version):
                service, semantic, run_id = self._make_active_v2_processed_run(
                    mode="all_residue",
                    messages=[message(1)],
                    suffix=f"protocol_{index}",
                    protocol_version=protocol_version,
                    profiled_v1=profiled_v1,
                )
                calls = semantic.provider.calls
                self.assertEqual(
                    service.upgrade_active_v2_all_history(), run_id
                )
                self.assertEqual(semantic.provider.calls, calls)

    def test_active_v2_upgrade_accepts_nine_current_and_thirty_one_historical_profiles(
        self,
    ) -> None:
        messages = []
        message_number = 1
        for conversation_index in range(1, 41):
            for _ in range(41 if conversation_index <= 9 else 1):
                messages.append(
                    message(
                        message_number,
                        conversation=f"conversation_{conversation_index}",
                    )
                )
                message_number += 1
        service, semantic, run_id = self._make_active_v2_processed_run(
            mode="all_residue",
            messages=messages,
            suffix="nine_current_thirty_one_historical",
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V3_4,
        )
        package_root = service.workspace / "02_processing" / "information"
        audit_root = (
            service.workspace / "02_processing" / "semantic_handoff_runs"
        )
        packages = sorted(path for path in package_root.iterdir() if path.is_dir())
        self.assertEqual(len(packages), 40)
        audit_by_package: dict[str, list[tuple[Path, dict[str, object]]]] = {}
        for audit_path in audit_root.glob("*/processing-run-audit.json"):
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit_by_package.setdefault(
                str(audit["package_fingerprint"]), []
            ).append((audit_path, audit))
        self.assertEqual(sum(map(len, audit_by_package.values())), 49)
        historical_packages = [
            package
            for package in packages
            if len(audit_by_package[_package_fingerprint(package)]) == 1
        ]
        self.assertEqual(len(historical_packages), 31)
        for package in historical_packages:
            old_fingerprint = _package_fingerprint(package)
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["provider"]["provider_version"] = "0.146.0"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            new_fingerprint = _package_fingerprint(package)
            matching = audit_by_package[old_fingerprint]
            self.assertEqual(len(matching), 1)
            audit_path, audit = matching[0]
            audit["provider_version"] = "0.146.0"
            audit["package_fingerprint"] = new_fingerprint
            audit_path.write_text(json.dumps(audit), encoding="utf-8")

        calls = semantic.provider.calls
        self.assertEqual(service.upgrade_active_v2_all_history(), run_id)
        self.assertEqual(semantic.provider.calls, calls)

    def test_active_v2_upgrade_accepts_pre_diagnostics_v1_audit(self) -> None:
        service, semantic, run_id = self._make_active_v2_processed_run(
            mode="all_candidate",
            messages=[message(1)],
            suffix="pre_diagnostics_v1",
        )
        package = next(
            (
                service.workspace / "02_processing" / "information"
            ).iterdir()
        )
        manifest = json.loads(
            (package / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["provider"], {"name": "external-agent-codex-cli"}
        )
        audit_path = next(
            (
                service.workspace
                / "02_processing"
                / "semantic_handoff_runs"
            ).glob("*/processing-run-audit.json")
        )
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        for field in (
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
            "contract_failure_detail",
        ):
            audit.pop(field)
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        calls = semantic.provider.calls
        self.assertEqual(service.upgrade_active_v2_all_history(), run_id)
        self.assertEqual(semantic.provider.calls, calls)

    def test_active_v2_upgrade_rejects_profiled_v2_prediagnostics_mix(self) -> None:
        service, semantic, run_id = self._make_active_v2_processed_run(
            mode="all_candidate",
            messages=[message(1)],
            suffix="profiled_v2_prediagnostics_mix",
            protocol_version=EXTERNAL_AGENT_PROTOCOL_V2,
        )
        audit_path = next(
            (
                service.workspace
                / "02_processing"
                / "semantic_handoff_runs"
            ).glob("*/processing-run-audit.json")
        )
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
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
        ):
            audit.pop(field)
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        run_root = service.run_store.runs_root / run_id
        before = {
            path.relative_to(run_root): path.read_bytes()
            for path in run_root.rglob("*")
            if path.is_file()
        }
        calls = semantic.provider.calls

        with self.assertRaisesRegex(WechatDigestError, "semantic receipt"):
            service.upgrade_active_v2_all_history()

        self.assertEqual(semantic.provider.calls, calls)
        self.assertEqual(
            {
                path.relative_to(run_root): path.read_bytes()
                for path in run_root.rglob("*")
                if path.is_file()
            },
            before,
        )

    def test_active_v2_upgrade_rejects_semantic_receipt_tamper(self) -> None:
        cases = (
            "missing_status_id",
            "extra_status_id",
            "missing_package",
            "missing_audit",
            "cleanup",
            "store",
            "missing_field",
            "missing_contract_field",
            "impossible_legacy_shape",
            "extra_field",
            "protocol",
            "fingerprint",
            "batch_order",
            "diagnostic_version",
            "provider_route",
            "provider_version",
            "provider_profile",
            "run_path",
        )
        for case in cases:
            with self.subTest(case=case):
                service, semantic, run_id = self._make_active_v2_processed_run(
                    mode="all_candidate",
                    messages=[message(1), message(2)],
                    suffix=case,
                    protocol_version=(
                        EXTERNAL_AGENT_PROTOCOL_V3_1
                        if case
                        in {
                            "missing_contract_field",
                            "provider_profile",
                            "provider_version",
                        }
                        else EXTERNAL_AGENT_PROTOCOL_V1
                    ),
                )
                status_path = service.run_store.runs_root / run_id / "status.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                item = next(iter(status["items"].values()))
                if case == "missing_status_id":
                    item["atomic_information_ids"] = []
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                elif case == "extra_status_id":
                    item["atomic_information_ids"].append(
                        "atomic_info_" + "0" * 32
                    )
                    status_path.write_text(json.dumps(status), encoding="utf-8")
                elif case == "missing_package":
                    package = (
                        service.workspace
                        / "02_processing"
                        / "information"
                        / item["representation_id"]
                    )
                    package.rename(package.with_name(package.name + ".missing"))
                elif case in {"missing_audit", "cleanup"}:
                    audit_path = next(
                        (
                            service.workspace
                            / "02_processing"
                            / "semantic_handoff_runs"
                        ).glob("*/processing-run-audit.json")
                    )
                    if case == "missing_audit":
                        audit_path.unlink()
                    else:
                        audit = json.loads(
                            audit_path.read_text(encoding="utf-8")
                        )
                        audit["process_cleanup_status"] = "not_verified"
                        audit_path.write_text(
                            json.dumps(audit), encoding="utf-8"
                        )
                elif case in {
                    "missing_field",
                    "missing_contract_field",
                    "impossible_legacy_shape",
                    "extra_field",
                    "protocol",
                    "fingerprint",
                    "batch_order",
                    "diagnostic_version",
                    "provider_route",
                    "provider_version",
                    "provider_profile",
                    "run_path",
                }:
                    audit_path = next(
                        (
                            service.workspace
                            / "02_processing"
                            / "semantic_handoff_runs"
                        ).glob("*/processing-run-audit.json")
                    )
                    audit = json.loads(
                        audit_path.read_text(encoding="utf-8")
                    )
                    if case == "missing_field":
                        audit.pop("input_fingerprint")
                    elif case == "missing_contract_field":
                        audit.pop("contract_failure_detail")
                    elif case == "impossible_legacy_shape":
                        for field in (
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
                        ):
                            audit.pop(field)
                    elif case == "extra_field":
                        audit["unexpected_field"] = "synthetic"
                    elif case == "protocol":
                        audit["protocol_version"] = (
                            "external-agent-semantic-handoff/999.0"
                        )
                    elif case == "fingerprint":
                        audit["input_fingerprint"] = "sha256:" + "0" * 64
                    elif case == "batch_order":
                        audit["anchor_unit_ids"] = list(
                            reversed(audit["anchor_unit_ids"])
                        )
                    elif case == "diagnostic_version":
                        audit["diagnostic_schema_version"] = (
                            "external-agent-diagnostics/999.0"
                        )
                    elif case == "provider_route":
                        audit["provider_route"] = "other-route"
                    elif case == "provider_version":
                        audit["provider_version"] = "0.999.0"
                    elif case == "provider_profile":
                        audit["model"] = "other-model"
                    else:
                        audit["processing_run_id"] = "run_" + "0" * 32
                    audit_path.write_text(
                        json.dumps(audit), encoding="utf-8"
                    )
                else:
                    information_path = (
                        service.workspace
                        / "03_information"
                        / "atomic_information.jsonl"
                    )
                    lines = information_path.read_text(encoding="utf-8").splitlines()
                    payload = json.loads(lines[0])
                    payload["origin_candidate_id"] = "candidate_" + "0" * 64
                    lines[0] = json.dumps(payload)
                    information_path.write_text(
                        "\n".join(lines) + "\n", encoding="utf-8"
                    )
                calls = semantic.provider.calls
                with self.assertRaisesRegex(
                    WechatDigestError, "semantic|Atomic Information"
                ):
                    service.upgrade_active_v2_all_history()
                self.assertEqual(semantic.provider.calls, calls)

    def test_active_v2_upgrade_rejects_empty_pending_human_receipt(self) -> None:
        service, semantic, run_id = self._make_active_v2_processed_run(
            mode="all_residue", messages=[message(1)], suffix="pending_empty"
        )
        status_path = service.run_store.runs_root / run_id / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        item = next(iter(status["items"].values()))
        item["state"] = "pending_human"
        item["pending_human"] = True
        status_path.write_text(json.dumps(status), encoding="utf-8")
        calls = semantic.provider.calls
        with self.assertRaisesRegex(WechatDigestError, "pending_human"):
            service.upgrade_active_v2_all_history()
        self.assertEqual(semantic.provider.calls, calls)

    def test_active_v2_upgrade_keeps_represented_package_recoverable(self) -> None:
        service, semantic, run_id = self._make_active_v2_processed_run(
            mode="all_candidate", messages=[message(1)], suffix="represented"
        )
        status_path = service.run_store.runs_root / run_id / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        item = next(iter(status["items"].values()))
        item["state"] = "represented"
        item["atomic_information_ids"] = []
        status_path.write_text(json.dumps(status), encoding="utf-8")
        calls = semantic.provider.calls
        self.assertEqual(service.upgrade_active_v2_all_history(), run_id)
        upgraded_item = next(
            iter(service.run_store.status(run_id)["items"].values())
        )
        self.assertEqual(upgraded_item["state"], "represented")
        self.assertEqual(semantic.provider.calls, calls)

    def test_active_v2_upgrade_interruption_reuses_first_frozen_upper(self) -> None:
        day = 24 * 60 * 60
        capture = SyntheticCaptureProvider(
            [message(1), message(2, timestamp=1_700_000_000 + 31 * day)],
            window_seconds=30 * day,
        )
        fail_once = [True]

        def interrupt_before_status() -> None:
            if fail_once.pop():
                raise RuntimeError("synthetic v2 upgrade interruption")

        run_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest",
            before_upgrade_status_write=interrupt_before_status,
        )
        service, run_id = self._make_active_v2_run(
            capture, run_store=run_store
        )
        with self.assertRaisesRegex(RuntimeError, "upgrade interruption"):
            service.upgrade_active_v2_all_history()
        first_plan = run_store.plan(run_id)
        first_upper = first_plan["all_history_upper_bound"]
        status_path = run_store.runs_root / run_id / "status.json"
        interrupted_status = json.loads(
            status_path.read_text(encoding="utf-8")
        )
        interrupted_status["plan_fingerprint"] = _plan_fingerprint(first_plan)
        status_path.write_text(
            json.dumps(interrupted_status), encoding="utf-8"
        )
        capture.messages.append(
            message(3, timestamp=1_700_000_000 + 65 * day)
        )
        run_store.before_upgrade_status_write = None
        self.assertEqual(service.upgrade_active_v2_all_history(), run_id)
        self.assertEqual(
            run_store.plan(run_id)["all_history_upper_bound"], first_upper
        )
        self.assertEqual(
            run_store.plan_receipt(run_id)["plan_fingerprint"],
            _plan_fingerprint(run_store.plan(run_id)),
        )
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_active_v2_upgrade_rejects_upper_tamper_after_plan_write(self) -> None:
        day = 24 * 60 * 60
        capture = SyntheticCaptureProvider(
            [message(1), message(2, timestamp=1_700_000_000 + 31 * day)],
            window_seconds=30 * day,
        )
        fail_once = [True]

        def interrupt_before_status() -> None:
            if fail_once.pop():
                raise RuntimeError("synthetic plan-only interruption")

        run_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest",
            before_upgrade_status_write=interrupt_before_status,
        )
        service, run_id = self._make_active_v2_run(
            capture, run_store=run_store
        )
        with self.assertRaisesRegex(RuntimeError, "plan-only"):
            service.upgrade_active_v2_all_history()
        plan_path = run_store.runs_root / run_id / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["all_history_upper_bound"] = plan["upper_bound"]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        run_store.before_upgrade_status_write = None
        with self.assertRaisesRegex(WechatDigestError, "pending receipt|plan"):
            service.upgrade_active_v2_all_history()
        self.assertEqual(run_store.plan_receipt(run_id)["phase"], "pending")
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_active_v2_upgrade_recovers_each_pending_transaction_stage(
        self,
    ) -> None:
        day = 24 * 60 * 60
        cases = (
            (
                "after_upgrade_pending_receipt_write",
                "wechat-digest-run-plan/2.0",
                "previous_plan_fingerprint",
            ),
            (
                "before_upgrade_status_write",
                "wechat-digest-run-plan/3.0",
                "previous_plan_fingerprint",
            ),
            (
                "before_upgrade_commit_receipt_write",
                "wechat-digest-run-plan/3.0",
                "target_plan_fingerprint",
            ),
        )
        for index, (hook_name, plan_schema, status_fingerprint_key) in enumerate(
            cases
        ):
            with self.subTest(hook=hook_name):
                capture = SyntheticCaptureProvider(
                    [
                        message(1),
                        message(2, timestamp=1_700_000_000 + 31 * day),
                    ],
                    window_seconds=30 * day,
                )
                failed = [True]

                def interrupt(failed: list[bool] = failed) -> None:
                    if failed.pop():
                        raise RuntimeError("synthetic upgrade transaction")

                run_store = WechatDigestRunStore(
                    self.workspace
                    / "02_processing"
                    / f"wechat_digest_upgrade_{index}",
                    **{hook_name: interrupt},
                )
                service, run_id = self._make_active_v2_run(
                    capture, run_store=run_store
                )
                with self.assertRaisesRegex(RuntimeError, "transaction"):
                    service.upgrade_active_v2_all_history()
                pending = run_store.plan_receipt(run_id)
                self.assertEqual(pending["phase"], "pending")
                self.assertEqual(
                    run_store.plan(run_id)["schema_version"], plan_schema
                )
                self.assertEqual(
                    run_store.status(run_id)["plan_fingerprint"],
                    pending[status_fingerprint_key],
                )
                first_upper = pending["all_history_upper_bound"]
                capture.messages.append(
                    message(3, timestamp=1_700_000_000 + 65 * day)
                )
                setattr(run_store, hook_name, None)
                self.assertEqual(
                    service.upgrade_active_v2_all_history(), run_id
                )
                committed_plan = run_store.plan(run_id)
                committed_receipt = run_store.plan_receipt(run_id)
                self.assertEqual(
                    committed_plan["all_history_upper_bound"], first_upper
                )
                self.assertEqual(committed_receipt["phase"], "committed")
                self.assertEqual(
                    committed_receipt["plan_fingerprint"],
                    _plan_fingerprint(committed_plan),
                )
                self.assertEqual(self.semantic.provider.calls, 0)

    def test_run_create_recovers_each_partial_write_before_active_publish(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider([message(1)]).capture(ZERO_CURSOR)
        plan, status = _build_plan(
            capture, clock=lambda: "2026-08-18T00:00:00+00:00"
        )
        cases = (
            ("before_create_status_write", (True, False, False)),
            ("before_create_receipt_write", (True, True, False)),
            ("before_create_active_write", (True, True, True)),
        )
        for index, (hook_name, expected_files) in enumerate(cases):
            with self.subTest(hook=hook_name):
                failed = [True]

                def interrupt(failed: list[bool] = failed) -> None:
                    if failed.pop():
                        raise RuntimeError("synthetic create interruption")

                root = (
                    self.workspace
                    / "02_processing"
                    / f"wechat_digest_create_{index}"
                )
                run_store = WechatDigestRunStore(
                    root, **{hook_name: interrupt}
                )
                with self.assertRaisesRegex(RuntimeError, "create interruption"):
                    run_store.create(plan, status)
                self.assertIsNone(run_store.active_run_id())
                run_dir = run_store.runs_root / str(plan["run_id"])
                self.assertEqual(
                    tuple(
                        (run_dir / name).exists()
                        for name in (
                            "plan.json",
                            "status.json",
                            "run-plan-receipt.json",
                        )
                    ),
                    expected_files,
                )
                setattr(run_store, hook_name, None)
                run_store.create(plan, status)
                self.assertEqual(run_store.active_run_id(), plan["run_id"])
                self.assertEqual(run_store.plan(str(plan["run_id"])), plan)
                self.assertEqual(run_store.status(str(plan["run_id"])), status)
                self.assertEqual(
                    run_store.plan_receipt(str(plan["run_id"]))[
                        "plan_fingerprint"
                    ],
                    _plan_fingerprint(plan),
                )

    def test_run_create_rejects_tampered_existing_status_and_receipt(self) -> None:
        capture = SyntheticCaptureProvider([message(1)]).capture(ZERO_CURSOR)
        plan, status = _build_plan(
            capture, clock=lambda: "2026-08-18T00:00:00+00:00"
        )
        for field in ("status", "receipt"):
            with self.subTest(field=field):
                root = (
                    self.workspace / "02_processing" / f"wechat_digest_{field}"
                )
                run_store = WechatDigestRunStore(root)
                run_store.create(plan, status)
                run_store.clear_active()
                path = (
                    run_store.runs_root
                    / str(plan["run_id"])
                    / (
                        "status.json"
                        if field == "status"
                        else "run-plan-receipt.json"
                    )
                )
                value = json.loads(path.read_text(encoding="utf-8"))
                if field == "status":
                    value["state"] = "failed"
                else:
                    value["plan_fingerprint"] = "sha256:" + "0" * 64
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(
                    WechatDigestError, "状态不一致|receipt 不一致"
                ):
                    run_store.create(plan, status)
                self.assertIsNone(run_store.active_run_id())

    def test_receipt_rejects_missing_fingerprint_and_impossible_create_order(
        self,
    ) -> None:
        capture = SyntheticCaptureProvider([message(1)]).capture(ZERO_CURSOR)
        plan, status = _build_plan(
            capture, clock=lambda: "2026-08-18T00:00:00+00:00"
        )
        run_id = str(plan["run_id"])
        root = self.workspace / "02_processing" / "wechat_digest_receipt"
        run_store = WechatDigestRunStore(root)
        run_store.create(plan, status)
        receipt_path = (
            run_store.runs_root / run_id / "run-plan-receipt.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["plan_fingerprint"] = None
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(WechatDigestError, "receipt 损坏"):
            run_store.plan_receipt(run_id)

        impossible_root = (
            self.workspace / "02_processing" / "wechat_digest_impossible"
        )
        impossible_store = WechatDigestRunStore(impossible_root)
        impossible_dir = impossible_store.runs_root / run_id
        impossible_dir.mkdir(parents=True)
        (impossible_dir / "plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        (impossible_dir / "run-plan-receipt.json").write_text(
            json.dumps(
                {
                    "schema_version": "wechat-digest-run-plan-receipt/2.0",
                    "run_id": run_id,
                    "phase": "committed",
                    "plan_fingerprint": _plan_fingerprint(plan),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WechatDigestError, "顺序损坏"):
            impossible_store.create(plan, status)
        self.assertFalse((impossible_dir / "status.json").exists())
        self.assertIsNone(impossible_store.active_run_id())

    def test_all_history_upper_tamper_fails_before_semantic_provider(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        service, run_id = self._make_active_v2_run(capture)
        service.upgrade_active_v2_all_history()
        plan_path = service.run_store.runs_root / run_id / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["all_history_upper_bound"] = ZERO_CURSOR.to_dict()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        provider_calls = self.semantic.provider.calls
        with self.assertRaisesRegex(WechatDigestError, "边界|receipt"):
            service.run()
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_global_semantic_authority_binds_active_all_history_plan(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        provider_calls = self.semantic.provider.calls
        connector = FailOnSecondCaptureProvider(list(capture.messages))
        service.capture_provider = connector
        grant = service.install_semantic_authority(
            inventory_authority_file=Path("/private/authority-a.json"),
        )
        self.assertEqual(grant["baseline_total"], 80)
        self.assertEqual(grant["max_new"], 20)
        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertEqual(connector.calls, [])
        self.assertEqual(
            service.install_semantic_authority(
                inventory_authority_file=Path("/private/authority-a.json"),
            ),
            grant,
        )
        with self.assertRaises(RuntimeError):
            service.install_semantic_authority(
                inventory_authority_file=Path("/private/authority-b.json"),
            )
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_cap1000_semantic_extension_requires_terminal_active_window(
        self,
    ) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        failures = {"remaining": 1}

        def fail_checkpoint_once() -> None:
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise OSError("synthetic checkpoint interruption")

        run_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest",
            before_checkpoint_publish=fail_checkpoint_once,
        )
        service = self.service(capture, run_store=run_store)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        provider_calls = self.semantic.provider.calls
        service.install_semantic_authority(
            inventory_authority_file=Path("/private/authority-a.json"),
        )
        self.semantic.reviewed_git_head = "7" * 40
        extension = service.install_semantic_authority_extension()
        self.assertEqual(extension["activation_total"], 81)
        self.assertEqual(extension["new_absolute_cap"], 1000)
        self.assertEqual(self.semantic.provider.calls, provider_calls)
        self.assertEqual(
            service.install_semantic_authority_extension(), extension
        )
        self.assertEqual(self.semantic.provider.calls, provider_calls)

        status_path = run_store.runs_root / run_store.active_run_id() / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        first = next(iter(status["items"].values()))
        first["state"] = "represented"
        status_path.write_text(json.dumps(status), encoding="utf-8")
        with self.assertRaisesRegex(WechatDigestError, "terminal"):
            service.install_semantic_authority_extension()
        self.assertEqual(self.semantic.provider.calls, provider_calls)

    def test_global_authority_uses_durable_completed_window_chain(self) -> None:
        day = 24 * 60 * 60
        capture = SyntheticCaptureProvider(
            [
                message(1, timestamp=1_700_000_000),
                message(2, timestamp=1_700_000_000 + 31 * day),
            ],
            window_seconds=30 * day,
        )
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        service.install_semantic_authority(
            inventory_authority_file=Path("/private/authority-a.json"),
        )
        result = service.run()
        self.assertGreaterEqual(result.new_messages, 2)
        plans = sorted(
            (
                WechatCursor.from_dict(plan["after_cursor"]),
                str(plan["run_id"]),
            )
            for plan in (
                service.run_store.plan(path.name)
                for path in service.run_store.runs_root.iterdir()
                if path.is_dir()
            )
        )
        self.assertEqual(len(plans), 2)
        first_run_id = plans[0][1]
        first_status = service.run_store.status(first_run_id)
        self.assertEqual(first_status["state"], "completed")
        self.assertTrue(first_status["checkpoint_published"])
        self.assertEqual(self.semantic.provider.calls, 2)
        final_binding = self.semantic.authority_bindings[-1]
        self.assertEqual(len(final_binding.completed_window_chain), 1)
        completed = final_binding.completed_window_chain[0]
        self.assertEqual(completed.window_run_id, first_run_id)
        self.assertEqual(
            completed.window_upper_cursor,
            final_binding.window_after_cursor,
        )
        checkpoint = service.run_store._read_json(
            service.run_store.checkpoint_path
        )
        self.assertEqual(
            checkpoint["run_id"], plans[-1][1]
        )
        self.assertIsNone(service.run_store.active_run_id())

    def test_completed_window_w_1_5_10_is_summary_only_and_connector_zero(
        self,
    ) -> None:
        for window_count in (1, 5, 10):
            with self.subTest(windows=window_count):
                root = (
                    self.workspace
                    / "02_processing"
                    / f"wechat_digest_completed_w_{window_count}"
                )
                run_store = WechatDigestRunStore(root)
                messages = [
                    message(
                        index + 1,
                        timestamp=1_700_000_000 + index,
                        conversation=f"conversation_{index:02d}",
                    )
                    for index in range(window_count + 1)
                ]
                global_upper = messages[-1].cursor
                after = ZERO_CURSOR
                previous_run_id = ""
                previous_cursor = ZERO_CURSOR
                for index, captured_message in enumerate(messages):
                    capture = WechatCapture(
                        "synthetic/1.0",
                        after,
                        captured_message.cursor,
                        (captured_message,),
                    )
                    legacy_plan, _ = _build_plan(
                        capture,
                        clock=lambda: "2026-08-25T00:00:00Z",
                        all_history_upper_bound=global_upper,
                    )
                    run_store.publish_capture_pending(
                        legacy_plan, capture, capture_ms=1
                    )
                    receipt = run_store.publish_capture_artifacts(
                        str(legacy_plan["run_id"]),
                        capture,
                        plan_binding_fingerprint=_plan_fingerprint(legacy_plan),
                        capture_ms=1,
                    )
                    plan, status = _build_plan(
                        capture,
                        clock=lambda: "2026-08-25T00:00:00Z",
                        run_id=str(legacy_plan["run_id"]),
                        created_at="2026-08-25T00:00:00Z",
                        all_history_upper_bound=global_upper,
                        capture_receipt_fingerprint=str(
                            receipt["receipt_fingerprint"]
                        ),
                    )
                    if index < window_count:
                        status["state"] = "completed"
                        status["checkpoint_published"] = True
                        for item in status["items"].values():
                            item["state"] = "unsupported"
                    run_store.create(plan, status)
                    if index < window_count:
                        run_store.clear_active()
                        previous_run_id = str(plan["run_id"])
                        previous_cursor = captured_message.cursor
                    after = captured_message.cursor

                run_store.publish_checkpoint(previous_run_id, previous_cursor)

                capture_provider = SyntheticCaptureProvider(messages)
                service = self.service(
                    capture_provider, run_store=run_store
                )
                with patch.object(
                    run_store,
                    "load_capture_artifacts",
                    side_effect=AssertionError(
                        "completed history must not deep-read snapshot"
                    ),
                ):
                    binding = service._semantic_authority_binding(
                        str(plan["run_id"])
                    )
                self.assertEqual(len(binding.completed_window_chain), window_count)
                self.assertEqual(capture_provider.calls, [])

    def test_upgrade_rejects_tampered_legacy_binding_before_any_write(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        service, run_id = self._make_active_legacy_run(capture)
        run_dir = service.run_store.runs_root / run_id
        status_path = run_dir / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        item = next(iter(status["items"].values()))
        item["source_id"] = "src_" + "0" * 32
        status_path.write_text(json.dumps(status), encoding="utf-8")
        before = {
            path.name: path.read_bytes()
            for path in run_dir.iterdir()
            if path.is_file()
        }
        with self.assertRaisesRegex(WechatDigestError, "binding"):
            service.upgrade_active_v1()
        after = {
            path.name: path.read_bytes()
            for path in run_dir.iterdir()
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_upgrade_recovers_after_plan_write_before_status_and_commits_receipt_last(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        fail_once = [True]

        def interrupt_before_status() -> None:
            if fail_once.pop():
                raise RuntimeError("synthetic status write interruption")

        run_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest",
            before_upgrade_status_write=interrupt_before_status,
        )
        service, run_id = self._make_active_legacy_run(capture, run_store=run_store)
        with self.assertRaisesRegex(RuntimeError, "status write interruption"):
            service.upgrade_active_v1()
        run_dir = run_store.runs_root / run_id
        self.assertEqual(
            json.loads((run_dir / "plan.json").read_text())["schema_version"],
            "wechat-digest-run-plan/3.0",
        )
        self.assertFalse((run_dir / "run-plan-receipt.json").exists())
        run_store.before_upgrade_status_write = None
        self.assertEqual(service.upgrade_active_v1(), run_id)
        self.assertTrue((run_dir / "run-plan-receipt.json").exists())
        self.assertEqual(self.semantic.provider.calls, 0)
        with self.assertRaisesRegex(WechatDigestError, "已经完成升级"):
            service.upgrade_active_v1()

    def test_upgrade_receipt_schema_tamper_fails_closed(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        service, run_id = self._make_active_legacy_run(capture)
        self.assertEqual(service.upgrade_active_v1(), run_id)
        receipt_path = service.run_store.runs_root / run_id / "run-plan-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["schema_version"] = "wechat-digest-run-plan-receipt/0.0"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(WechatDigestError, "receipt"):
            service.prepare_next_semantic()
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_upgrade_readback_rejects_disk_plan_tamper_after_commit(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        run_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest"
        )
        service, run_id = self._make_active_legacy_run(capture, run_store=run_store)

        def tamper_plan() -> None:
            path = run_store.runs_root / run_id / "plan.json"
            plan = json.loads(path.read_text(encoding="utf-8"))
            plan["conversations"][0]["content_hash"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(plan), encoding="utf-8")

        run_store.after_upgrade_receipt_write = tamper_plan
        with self.assertRaisesRegex(WechatDigestError, "读回|不一致"):
            service.upgrade_active_v1()
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_upgrade_readback_rejects_active_tamper_after_commit(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        run_store = WechatDigestRunStore(
            self.workspace / "02_processing" / "wechat_digest"
        )
        service, _ = self._make_active_legacy_run(capture, run_store=run_store)

        def tamper_active() -> None:
            run_store.active_path.write_text(
                json.dumps({"active_run_id": None}), encoding="utf-8"
            )

        run_store.after_upgrade_receipt_write = tamper_active
        with self.assertRaisesRegex(WechatDigestError, "active run 读回"):
            service.upgrade_active_v1()
        self.assertEqual(self.semantic.provider.calls, 0)

    def test_concurrent_digest_fails_without_reading_capture(self) -> None:
        capture = SyntheticCaptureProvider([])
        service = self.service(capture)
        with (
            service.run_store.lock(),
            self.assertRaisesRegex(WechatDigestError, "正在运行"),
        ):
            service.run(from_now=True)
        self.assertEqual(capture.calls, [])


class WechatCaptureHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "msg.db"
        self.database_key = "msg/msg.db"
        self.username = "wxid_synthetic"
        self.table_name = "Msg_" + hashlib.md5(
            self.username.encode()
        ).hexdigest()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                f"CREATE TABLE [{self.table_name}] ("
                "local_id INTEGER PRIMARY KEY, local_type INTEGER, "
                "create_time INTEGER, real_sender_id TEXT, "
                "raw_content TEXT, compression INTEGER)"
            )
            connection.executemany(
                f"INSERT INTO [{self.table_name}] VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, 1, 100, "sender", "message-1", 0),
                    (2, 1, 200, "sender", "message-2", 0),
                    (3, 1, 200, "sender", "message-3", 0),
                    (4, 1, 200, "sender", "message-4", 0),
                    (5, 1, 300, "sender", "message-5", 0),
                ],
            )
            connection.commit()
        self.app = SimpleNamespace(
            cache={self.database_key: str(self.database)},
            msg_db_keys=(self.database_key,),
            decrypted_dir=self.root,
            db_dir=self.root,
            display_name_fn=lambda value: value,
        )
        conversation_key = capture_digest(
            "wechat_conversation", self.username
        )
        self.cursors = {
            local_id: (
                timestamp,
                conversation_key,
                capture_digest(
                    "wechat_message",
                    self.username,
                    self.database_key,
                    local_id,
                    timestamp,
                ),
            )
            for local_id, timestamp in (
                (1, 100),
                (2, 200),
                (3, 200),
                (4, 200),
                (5, 300),
            )
        }
        self.modules = self._provider_modules()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _provider_modules(self) -> dict[str, ModuleType]:
        root = ModuleType("wechat_cli")
        core = ModuleType("wechat_cli.core")
        contacts = ModuleType("wechat_cli.core.contacts")
        context = ModuleType("wechat_cli.core.context")
        messages = ModuleType("wechat_cli.core.messages")
        contacts.get_contact_names = lambda *_args: {
            self.username: "Synthetic Conversation"
        }
        context.AppContext = lambda _config_path: self.app
        messages._split_msg_type = lambda value: (int(value), 0)
        messages._parse_int = lambda value: int(value)
        messages._parse_xml_root = lambda _value: None
        messages._load_name2id_maps = lambda _connection: {}
        messages.decompress_content = lambda value, _compression: value
        messages._format_message_text = (
            lambda _local_id,
            _local_type,
            content,
            _is_group,
            _username,
            _display_name,
            _names,
            _display_name_fn,
            **_kwargs: (None, content)
        )
        messages._resolve_sender_label = lambda *_args, **_kwargs: (
            "Synthetic Sender"
        )

        def query_messages(
            connection,
            table_name,
            *,
            start_ts,
            end_ts,
            limit,
        ):
            self.assertIsNone(limit)
            return connection.execute(
                f"SELECT local_id, local_type, create_time, "
                f"real_sender_id, raw_content, compression "
                f"FROM [{table_name}] "
                "WHERE create_time >= ? AND create_time <= ? "
                "ORDER BY create_time ASC, local_id ASC",
                (start_ts, end_ts),
            ).fetchall()

        messages._query_messages = query_messages
        root.core = core
        core.contacts = contacts
        core.context = context
        core.messages = messages
        return {
            "wechat_cli": root,
            "wechat_cli.core": core,
            "wechat_cli.core.contacts": contacts,
            "wechat_cli.core.context": context,
            "wechat_cli.core.messages": messages,
        }

    @contextmanager
    def capture_runtime(self):
        with patch.dict(sys.modules, self.modules), patch.object(
            wechat_capture_helper,
            "_sessions",
            return_value=((
                self.username,
                "Synthetic Conversation",
                False,
            ),),
        ):
            yield

    @staticmethod
    def request(
        *,
        upper_bound=None,
        all_history_upper_bound=None,
        observe_only=False,
        message_limit=1000,
    ):
        return {
            "config_path": None,
            "after_cursor": {
                "timestamp": 99,
                "conversation_key": "",
                "message_key": "",
            },
            "upper_bound": upper_bound,
            "all_history_upper_bound": all_history_upper_bound,
            "observe_only": observe_only,
            "window_days": 30,
            "window_message_limit": message_limit,
        }

    @staticmethod
    def cursor_dict(cursor):
        return {
            "timestamp": cursor[0],
            "conversation_key": cursor[1],
            "message_key": cursor[2],
        }

    def _legacy_capture(self, request):
        def unbounded_cursor_discovery(
            located_tables,
            *,
            start_timestamp,
            end_timestamp=None,
        ):
            del end_timestamp
            return _all_cursor_rows(
                located_tables, start_timestamp=start_timestamp
            )

        with self.capture_runtime(), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            side_effect=unbounded_cursor_discovery,
        ):
            return capture_with_wechat_cli(request)

    def test_cursor_discovery_uses_optional_sql_upper_bound(self) -> None:
        statements = []
        real_connect = sqlite3.connect

        class TrackingConnection:
            def __init__(self, path):
                self.connection = real_connect(path)

            def execute(self, statement, parameters=()):
                statements.append((statement, parameters))
                return self.connection.execute(statement, parameters)

            def close(self):
                self.connection.close()

        located = {
            str(self.database): ((
                self.database_key,
                self.username,
                "Synthetic Conversation",
                False,
                self.table_name,
            ),)
        }
        with patch.object(
            wechat_capture_helper.sqlite3,
            "connect",
            side_effect=TrackingConnection,
        ):
            bounded = _all_cursor_rows(
                located, start_timestamp=99, end_timestamp=200
            )
        self.assertEqual([row[3] for row in bounded], [100, 200, 200, 200])
        self.assertIn("create_time <= ?", statements[0][0])
        self.assertEqual(statements[0][1], (99, 200))

        statements.clear()
        with patch.object(
            wechat_capture_helper.sqlite3,
            "connect",
            side_effect=TrackingConnection,
        ):
            unbounded = _all_cursor_rows(located, start_timestamp=99)
        self.assertEqual([row[3] for row in unbounded], [100, 200, 200, 200, 300])
        self.assertNotIn("create_time <= ?", statements[0][0])
        self.assertEqual(statements[0][1], (99,))

    def test_fixed_upper_is_sql_bounded_and_byte_equivalent(self) -> None:
        same_second = sorted(
            cursor for cursor in self.cursors.values() if cursor[0] == 200
        )
        upper = same_second[1]
        request = self.request(upper_bound=self.cursor_dict(upper))
        legacy = self._legacy_capture(request)
        with self.capture_runtime(), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            wraps=_all_cursor_rows,
        ) as discovery:
            optimized = capture_with_wechat_cli(request)
        self.assertEqual(discovery.call_args.kwargs["end_timestamp"], 200)
        self.assertEqual(
            json.dumps(optimized, ensure_ascii=False, separators=(",", ":")),
            json.dumps(legacy, ensure_ascii=False, separators=(",", ":")),
        )
        returned = [
            (
                item["cursor"]["timestamp"],
                item["cursor"]["conversation_key"],
                item["cursor"]["message_key"],
            )
            for item in optimized["messages"]
        ]
        self.assertEqual(
            [cursor for cursor in returned if cursor[0] == 200],
            same_second[:2],
        )

    def test_all_history_upper_is_sql_bounded_and_byte_equivalent(self) -> None:
        upper = sorted(
            cursor for cursor in self.cursors.values() if cursor[0] == 200
        )[1]
        request = self.request(
            all_history_upper_bound=self.cursor_dict(upper)
        )
        legacy = self._legacy_capture(request)
        with self.capture_runtime(), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            wraps=_all_cursor_rows,
        ) as discovery:
            optimized = capture_with_wechat_cli(request)
        self.assertEqual(discovery.call_args.kwargs["end_timestamp"], 200)
        self.assertEqual(
            json.dumps(optimized, ensure_ascii=False, separators=(",", ":")),
            json.dumps(legacy, ensure_ascii=False, separators=(",", ":")),
        )

    def test_observe_only_uses_known_upper_without_returning_messages(self) -> None:
        upper = sorted(self.cursors.values())[3]
        request = self.request(
            all_history_upper_bound=self.cursor_dict(upper),
            observe_only=True,
        )
        with self.capture_runtime(), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            side_effect=AssertionError("observe-only must not scan cursor rows"),
        ) as discovery:
            result = capture_with_wechat_cli(request)
        discovery.assert_not_called()
        self.assertEqual(result["observed_upper"], self.cursor_dict(upper))
        self.assertEqual(result["messages"], [])

    def test_observe_only_unknown_upper_uses_database_max_not_all_rows(self) -> None:
        request = self.request(observe_only=True)
        with self.capture_runtime(), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            side_effect=AssertionError("observe-only must not scan cursor rows"),
        ) as discovery, patch.object(
            wechat_capture_helper,
            "_upper_cursor_rows",
            wraps=wechat_capture_helper._upper_cursor_rows,
        ) as upper_query:
            result = capture_with_wechat_cli(request)
        discovery.assert_not_called()
        upper_query.assert_called_once()
        self.assertEqual(
            result["observed_upper"], self.cursor_dict(max(self.cursors.values()))
        )
        self.assertEqual(result["messages"], [])

    def test_production_shaped_probe_reads_only_table_maxima(self) -> None:
        located: dict[str, tuple[tuple[str, str, str, bool, str], ...]] = {}
        sessions: list[tuple[str, str, bool]] = []
        total_rows = 0
        for partition_index in range(4):
            database = self.root / f"production-{partition_index}.db"
            tables: list[tuple[str, str, str, bool, str]] = []
            with closing(sqlite3.connect(database)) as connection:
                for conversation_index in range(partition_index, 45, 4):
                    username = f"wxid_fixture_{conversation_index:02d}"
                    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
                    connection.execute(
                        f"CREATE TABLE [{table}] ("
                        "local_id INTEGER PRIMARY KEY, local_type INTEGER, "
                        "create_time INTEGER, real_sender_id TEXT, "
                        "raw_content TEXT, compression INTEGER)"
                    )
                    rows = [
                        (
                            row + 1,
                            3 if (total_rows + row) % 10 == 0 else 1,
                            1_700_000_000 + row,
                            "sender",
                            f"message-{conversation_index}-{row}",
                            0,
                        )
                        for row in range(22 + (conversation_index < 10))
                    ]
                    total_rows += len(rows)
                    connection.executemany(
                        f"INSERT INTO [{table}] VALUES (?, ?, ?, ?, ?, ?)", rows
                    )
                    sessions.append(
                        (
                            username,
                            f"Conversation {conversation_index}",
                            False,
                        )
                    )
                    tables.append(
                        (
                            f"msg/production-{partition_index}.db",
                            username,
                            f"Conversation {conversation_index}",
                            False,
                            table,
                        )
                    )
                connection.commit()
            located[str(database)] = tuple(tables)

        self.app.cache = {
            rows[0][0]: database
            for database, rows in located.items()
            if rows
        }
        self.app.msg_db_keys = tuple(self.app.cache)
        with patch.dict(sys.modules, self.modules), patch.object(
            wechat_capture_helper,
            "_sessions",
            return_value=tuple(sessions),
        ), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            side_effect=AssertionError("probe must not scan all rows"),
        ) as probe_full_scan:
            observed = capture_with_wechat_cli(
                self.request(observe_only=True)
            )
        probe_full_scan.assert_not_called()

        full_capture_samples: list[int] = []
        captured_samples: list[dict[str, object]] = []
        parsed_samples: list[WechatCapture] = []
        after_cursor = WechatCursor(99, "", "")
        parser = object.__new__(WechatCliCaptureProvider)
        parser.provider_version = "0.5.0"
        with patch.dict(sys.modules, self.modules), patch.object(
            wechat_capture_helper,
            "_sessions",
            return_value=tuple(sessions),
        ), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            wraps=_all_cursor_rows,
        ) as full_scan:
            for _ in range(7):
                started = time.monotonic_ns()
                captured = capture_with_wechat_cli(
                    self.request(
                        all_history_upper_bound=observed["observed_upper"],
                        message_limit=1000,
                    )
                )
                connector_payload = json.loads(
                    json.dumps(captured, ensure_ascii=False, separators=(",", ":"))
                )
                parsed = parser._parse_capture(connector_payload, after_cursor)
                full_capture_samples.append(time.monotonic_ns() - started)
                captured_samples.append(captured)
                parsed_samples.append(parsed)

        captured = captured_samples[-1]
        parsed_capture = parsed_samples[-1]
        plan, _status = _build_plan(
            parsed_capture,
            clock=lambda: "2026-08-25T00:00:00Z",
        )
        store = WechatDigestRunStore(self.root / "production-capture-artifacts")
        store.publish_capture_pending(plan, parsed_capture, capture_ms=1)
        store.publish_capture_artifacts(
            str(plan["run_id"]),
            parsed_capture,
            plan_binding_fingerprint=_plan_fingerprint(plan),
            capture_ms=1,
        )
        readback_samples: list[int] = []
        for _ in range(7):
            started = time.monotonic_ns()
            loaded, _receipt = store.load_capture_artifacts(str(plan["run_id"]))
            readback_samples.append(time.monotonic_ns() - started)
            self.assertEqual(loaded, parsed_capture)

        median_full_capture = sorted(full_capture_samples)[3]
        median_readback = sorted(readback_samples)[3]

        self.assertEqual(total_rows, 1000)
        self.assertEqual(full_scan.call_count, 7)
        self.assertEqual(len(captured_samples), 7)
        self.assertTrue(
            all(len(item["messages"]) == 1000 for item in captured_samples)
        )
        self.assertEqual(len(captured["messages"]), 1000)
        self.assertEqual(
            len({item["conversation_key"] for item in captured["messages"]}),
            45,
        )
        self.assertEqual(
            sum(len(item["attachments"]) for item in captured["messages"]),
            100,
        )
        self.assertLess(median_readback, median_full_capture)

    def test_unbounded_window_discovery_is_unchanged(self) -> None:
        request = self.request(message_limit=2)
        with self.capture_runtime(), patch.object(
            wechat_capture_helper,
            "_all_cursor_rows",
            wraps=_all_cursor_rows,
        ) as discovery:
            result = capture_with_wechat_cli(request)
        self.assertIsNone(discovery.call_args.kwargs["end_timestamp"])
        expected = _window_upper(
            list(self.cursors.values()), window_days=30, message_limit=2
        )
        self.assertEqual(result["observed_upper"], self.cursor_dict(expected))


class WechatCliCaptureProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.executable = self.root / "wechat-cli"
        self.executable.write_text(
            f"#!{sys.executable}\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def capture_payload(messages):
        return {
            "schema_version": "wechat-cli-capture/1.0",
            "observed_upper": {
                "timestamp": 1_700_000_001,
                "conversation_key": "conversation_a",
                "message_key": "message_a",
            },
            "messages": messages,
        }

    @staticmethod
    def captured_message(*, attachment_path: Path | None = None):
        return {
            "conversation_key": "conversation_a",
            "provider_conversation_id": "provider_a",
            "conversation_label": "Synthetic Conversation",
            "is_group": False,
            "message_key": "message_a",
            "cursor": {
                "timestamp": 1_700_000_001,
                "conversation_key": "conversation_a",
                "message_key": "message_a",
            },
            "sender_label": "Synthetic Sender",
            "message_type": "file" if attachment_path else "text",
            "timestamp": 1_700_000_001,
            "sent_at": "2023-11-14T22:13:21+00:00",
            "visible_content": "Synthetic content",
            "structured_payload": "Synthetic content",
            "attachments": [] if attachment_path is None else [
                {
                    "attachment_key": "attachment_a",
                    "status": "available",
                    "filename_hint": "synthetic.txt",
                    "media_type": "text/plain",
                    "path": str(attachment_path),
                }
            ],
        }

    def provider(self, payload, *, version="wechat-cli version 0.5.0"):
        def runner(command, **kwargs):
            del kwargs
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, version, "")
            return subprocess.CompletedProcess(
                command, 0, json.dumps(payload), ""
            )

        return WechatCliCaptureProvider(
            wechat_cli_binary=str(self.executable), runner=runner
        )

    def test_capture_request_has_time_and_message_boundaries(self) -> None:
        requests = []

        def runner(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, "wechat-cli version 0.5.0", ""
                )
            requests.append(json.loads(kwargs["input"]))
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.capture_payload([])), ""
            )

        provider = WechatCliCaptureProvider(
            wechat_cli_binary=str(self.executable), runner=runner
        )
        provider.capture(WechatCursor(0, "", ""))
        self.assertEqual(requests[0]["window_days"], 30)
        self.assertEqual(requests[0]["window_message_limit"], 1000)
        self.assertIsNone(requests[0]["all_history_upper_bound"])

    def test_window_upper_uses_the_stricter_boundary(self) -> None:
        day = 24 * 60 * 60
        cursors = [(index, "conversation", f"message-{index}") for index in range(5)]
        self.assertEqual(
            _window_upper(cursors, window_days=30, message_limit=3),
            cursors[2],
        )
        spread = [
            (1, "conversation", "message-1"),
            (1 + 31 * day, "conversation", "message-2"),
        ]
        self.assertEqual(
            _window_upper(spread, window_days=30, message_limit=1000),
            spread[0],
        )

    def test_structured_capture_hashes_exact_attachment(self) -> None:
        attachment_path = self.root / "synthetic.txt"
        attachment_path.write_text("synthetic bytes", encoding="utf-8")
        payload = self.capture_payload(
            [self.captured_message(attachment_path=attachment_path)]
        )
        capture = self.provider(payload).capture(WechatCursor(0, "", ""))
        captured = capture.messages[0].attachments[0]
        self.assertEqual(captured.path, attachment_path)
        self.assertEqual(captured.content_hash, _hash(attachment_path)[0])

    def test_unordered_capture_fails_closed(self) -> None:
        first = self.captured_message()
        second = dict(first)
        second["message_key"] = "message_0"
        second["cursor"] = {
            "timestamp": 1_700_000_000,
            "conversation_key": "conversation_a",
            "message_key": "message_0",
        }
        payload = self.capture_payload([first, second])
        with self.assertRaisesRegex(WechatDigestError, "结果不完整"):
            self.provider(payload).capture(WechatCursor(0, "", ""))

    def test_unverified_connector_version_fails_before_capture(self) -> None:
        with self.assertRaisesRegex(WechatDigestError, "版本未经验证"):
            self.provider(self.capture_payload([]), version="wechat-cli version 0.6.0")


if __name__ == "__main__":
    unittest.main()
