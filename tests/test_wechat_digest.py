from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from archeos.atomic_information import (
    JsonlAtomicInformationStore,
    ingest_processing_package,
)
from archeos.digestion import (
    CodexAtomicInformationInterpretationProvider,
    InterpretationResult,
    WorldModelOperation,
)
from archeos.digestion.providers import CodexInterpretationTimeout
from archeos.representation import LocalRepresentationRepository
from archeos.representation_information import (
    EXTERNAL_AGENT_PROTOCOL_V1,
    EXTERNAL_AGENT_PROTOCOL_V2,
    EXTERNAL_AGENT_PROTOCOL_V3,
    EXTERNAL_AGENT_PROTOCOL_V3_1,
    EXTERNAL_AGENT_PROTOCOL_V3_2,
    EXTERNAL_AGENT_PROTOCOL_V3_3,
    EXTERNAL_AGENT_PROTOCOL_V3_4,
    RepresentationAnalysisResult,
    RepresentationCandidateDraft,
    RepresentationInformationService,
    RepresentationResidueDraft,
    _analysis_batches_for_anchor_unit_ids,
    _external_agent_request,
    _units_from_representation,
)
from archeos.semantic_handoff import _package_fingerprint
from archeos.source import LocalManagedSourceRepository
from archeos.wechat_capture_helper import _window_upper
from archeos.wechat_digest import (
    ZERO_CURSOR,
    CapturedAttachment,
    CapturedMessage,
    DeterministicPrivacyGate,
    WechatCapture,
    WechatCliCaptureProvider,
    WechatCursor,
    WechatDigestError,
    WechatDigestRunStore,
    WechatDigestService,
    _build_plan,
    _governance_atomic_fingerprint,
    _plan_fingerprint,
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


class SyntheticSemanticHandoff:
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
        self.unknown_resolution = None
        self.timeout_212_resolution = None
        self.before_unknown_commit = None
        self.campaign_binding = None
        self.authority_bindings = []
        self.global_attempt_total = 176
        self.global_unknown = 0
        self.absolute_cap = 1000
        self.latest_representation_id = None

    def execute(
        self,
        representation_id: str,
        *,
        privacy_binding=None,
        authority_binding=None,
    ):
        self.latest_representation_id = representation_id
        if privacy_binding is not None:
            assert privacy_binding.route == "approved"
            assert authority_binding is not None
            self.authority_bindings.append(authority_binding)
        output_root = self.workspace / "02_processing" / "information"
        package = output_root / representation_id
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


class FailFirstBatchProgressOnceService(WechatDigestService):
    fail_progress = True

    def _update_item(self, run_id, status, item_id, **changes):
        receipt = changes.get("governance_receipt")
        if (
            self.fail_progress
            and isinstance(receipt, dict)
            and receipt.get("phase") == "applying"
            and receipt.get("next_index") == 1
        ):
            self.fail_progress = False
            raise OSError("synthetic progress persistence interruption")
        return super()._update_item(run_id, status, item_id, **changes)


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
        self.assertEqual(status["items"][item_id]["atomic_information_ids"], [])
        self.assertEqual(
            len(service.information_store.list_atomic_information()), 10
        )
        return service, capture, provider, run_id, item_id

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

    def test_issue_135_migration_freezes_15_and_batches_only_remaining_3(
        self,
    ) -> None:
        provider = BatchOnceProvider()
        capture = SyntheticCaptureProvider(
            [message(index) for index in range(1, 19)]
        )
        service = WechatDigestService(
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
            "#issuecomment-5353218136"
        )

        migration = service.activate_batch_governance(
            authority_ref=authority_ref
        )

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
            service.activate_batch_governance(authority_ref=authority_ref),
            migration,
        )
        self.assertEqual(provider.calls, 0)

        result = service.run()

        self.assertTrue(result.checkpoint_published)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.batch_sizes, [3])
        final_item = service.run_store.status(prepared.run_id)["items"][item_id]
        self.assertEqual(final_item["atomic_information_ids"], list(atomic_ids))
        self.assertEqual(final_item["governance_receipt"]["phase"], "completed")
        self.assertEqual(
            final_item["governance_migration"]["legacy_governance_receipt"][
                "phase"
            ],
            "started",
        )

    def test_mid_apply_recovery_reuses_persisted_batch_result(self) -> None:
        self.create_object()
        provider = BatchOnceProvider()
        capture = SyntheticCaptureProvider([message(1), message(2)])
        service = FailFirstBatchProgressOnceService(
            workspace=self.workspace,
            capture_provider=capture,
            semantic_handoff_factory=lambda: self.semantic,
            interpretation_provider=provider,
        )

        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        run_id = service.run_store.active_run_id()
        assert run_id is not None
        interrupted_receipt = next(
            item["governance_receipt"]
            for item in service.run_store.status(run_id)["items"].values()
            if isinstance(item, dict) and "governance_receipt" in item
        )
        self.assertEqual(interrupted_receipt["phase"], "interpreted")
        self.assertEqual(interrupted_receipt["next_index"], 0)
        self.assertEqual(provider.calls, 1)

        result = service.run()

        self.assertTrue(result.replayed)
        self.assertTrue(result.checkpoint_published)
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

        continuation = service.install_semantic_maintenance_continuation(
            authority_ref=authority_ref
        )

        self.assertEqual(continuation["activation_total"], 176)
        self.assertEqual(continuation["next_global_ordinal"], 177)
        self.assertEqual(continuation["authority_ref"], authority_ref)
        self.assertEqual(self.semantic.provider.calls, semantic_calls)
        self.assertEqual(provider.calls, governance_calls)
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

    def test_capture_fingerprint_change_fails_closed(self) -> None:
        self.create_object()
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        with self.assertRaises(WechatDigestError):
            self.service(capture).run(all_history=True)
        capture.messages[0] = replace(capture.messages[0], visible_content="changed")
        with self.assertRaisesRegex(WechatDigestError, "重放内容发生变化"):
            self.service(capture).run()

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

    def test_prepare_fingerprint_change_fails_before_provider(self) -> None:
        capture = SyntheticCaptureProvider([message(1)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        capture.messages[0] = replace(capture.messages[0], visible_content="changed")
        with self.assertRaisesRegex(WechatDigestError, "重放内容发生变化"):
            service.prepare_next_semantic()
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
        grant = service.install_semantic_authority(
            inventory_authority_file=Path("/private/authority-a.json"),
        )
        self.assertEqual(grant["baseline_total"], 80)
        self.assertEqual(grant["max_new"], 20)
        self.assertEqual(self.semantic.provider.calls, provider_calls)
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
