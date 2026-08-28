from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    IngestionResult,
    JsonlAtomicInformationStore,
)
from archeos.cli import main
from archeos.workspace import WorkspaceConfig
from archeos.codex_app_server import CodexAnalysisProvider
from archeos.pipeline import ProcessingError
from archeos.pyannote_speakers import PyannoteSpeakerProvider
from archeos.representation_information import (
    FileRepresentationAnalysisProvider,
)
from archeos.wechat_contact_synthesis import ContactSynthesisStore
from archeos.wechat_digest import (
    WechatContactBinding,
    WechatDigestResult,
    WechatSemanticPreparation,
)
from archeos.workspace import WorkspaceConfig
from archeos.world_model import SQLiteWorldModelRepository


class CliTest(unittest.TestCase):
    CONTACT_AUTHORITY_REF = (
        "https://github.com/leevi2010-cursor/ArcheOS/issues/999"
        "#issuecomment-999"
    )

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_first_contact_run_without_authority_stops_before_capture_or_run_artifact(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            require_workspace.return_value = WorkspaceConfig(
                workspace, workspace / "config"
            )
            binding = WechatContactBinding(
                "wechat_conversation_" + "9" * 32,
                "wxid_synthetic_9",
                "Synthetic Contact 9",
                False,
            )
            capture_provider.return_value.resolve_contact.return_value = binding
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "wechat",
                        "digest",
                        "--contact",
                        binding.display_name,
                        "--from-now",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertIn("真实批准链接", output.getvalue())
            capture_provider.return_value.scoped.assert_not_called()
            capture_provider.return_value.capture.assert_not_called()
            digest_service.assert_not_called()
            contact_root = (
                workspace
                / "02_processing"
                / "wechat_digest"
                / "contacts"
                / binding.conversation_key
            )
            self.assertFalse(contact_root.exists())

            with redirect_stdout(output):
                invalid = main(
                    [
                        "wechat",
                        "digest",
                        "--contact",
                        binding.display_name,
                        "--authority-ref",
                        "https://github.com/leevi2010-cursor/ArcheOS/issues/202",
                        "--from-now",
                    ]
                )
            self.assertEqual(invalid, 1)
            capture_provider.return_value.scoped.assert_not_called()
            digest_service.assert_not_called()
            self.assertFalse(contact_root.exists())

    @patch("archeos.cli.build_contact_acceptance_pack")
    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_contact_resume_reuses_exact_authority_and_rejects_drift(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
        build_pack: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            require_workspace.return_value = WorkspaceConfig(
                workspace, workspace / "config"
            )
            binding = WechatContactBinding(
                "wechat_conversation_" + "8" * 32,
                "wxid_synthetic_8",
                "Synthetic Contact 8",
                False,
            )
            capture_provider.return_value.resolve_contact.return_value = binding
            capture_provider.return_value.scoped.return_value = (
                capture_provider.return_value
            )
            contact_root = (
                workspace
                / "02_processing"
                / "wechat_digest"
                / "contacts"
                / binding.conversation_key
            )
            provider = Mock()
            provider.name = "synthetic"
            provider.provider_version = "1"
            provider.model = "synthetic"
            provider.reasoning_effort = "medium"
            provider.provider_calls = 0
            ContactSynthesisStore(contact_root / "synthesis").synthesize(
                (),
                binding=binding,
                provider=provider,
                authority_ref=self.CONTACT_AUTHORITY_REF,
                absolute_cap=7,
            )
            digest_service.return_value.run.return_value = WechatDigestResult(
                run_id="run_" + "8" * 32,
                new_messages=0,
                new_attachments=0,
                durable_information=0,
                local_only=0,
                unsupported=0,
                pending_human=0,
                context_objects=0,
                checkpoint_published=True,
                replayed=True,
            )
            build_pack.return_value = (
                workspace / "result.json",
                workspace / "result.md",
            )
            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "wechat",
                        "digest",
                        "--contact",
                        binding.display_name,
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(
                build_pack.call_args.kwargs["authority_ref"],
                self.CONTACT_AUTHORITY_REF,
            )
            self.assertEqual(build_pack.call_args.kwargs["absolute_cap"], 7)

            digest_service.reset_mock()
            with redirect_stdout(StringIO()):
                drift = main(
                    [
                        "wechat",
                        "digest",
                        "--contact",
                        binding.display_name,
                        "--authority-ref",
                        (
                            "https://github.com/leevi2010-cursor/ArcheOS/"
                            "issues/998#issuecomment-998"
                        ),
                    ]
                )
            self.assertEqual(drift, 1)
            digest_service.assert_not_called()

    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_new_wechat_digest_requires_contact_before_connector(
        self, require_workspace: Mock, capture_provider: Mock
    ) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main(["wechat", "digest", "--since", "2026-08-01"])
        self.assertEqual(result, 2)
        self.assertIn("先用 --list-contacts", output.getvalue())
        require_workspace.assert_not_called()
        capture_provider.assert_not_called()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_isolated_contact_acceptance_uses_only_private_workspace(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary"
            isolated = root / "isolated"
            primary.mkdir()
            require_workspace.return_value = WorkspaceConfig(
                primary, root / "config"
            )
            binding = WechatContactBinding(
                "wechat_conversation_" + "3" * 32,
                "wxid_synthetic_3",
                "Synthetic Contact 3",
                False,
            )
            capture_provider.return_value.resolve_contact.return_value = binding
            capture_provider.return_value.scoped.return_value = (
                capture_provider.return_value
            )
            digest_service.return_value.run.return_value = WechatDigestResult(
                run_id="run_" + "3" * 32,
                new_messages=1,
                new_attachments=0,
                durable_information=1,
                local_only=0,
                unsupported=0,
                pending_human=0,
                context_objects=0,
                checkpoint_published=True,
                replayed=False,
            )
            before = tuple(primary.rglob("*"))
            with patch(
                "archeos.cli.build_contact_acceptance_pack",
                return_value=(isolated / "result.json", isolated / "result.md"),
            ), redirect_stdout(StringIO()):
                result = main(
                    [
                        "wechat",
                        "digest",
                        "--contact",
                        binding.display_name,
                        "--authority-ref",
                        self.CONTACT_AUTHORITY_REF,
                        "--since",
                        "2026-08-01",
                        "--isolated-acceptance-dir",
                        str(isolated),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(tuple(primary.rglob("*")), before)
            service_kwargs = digest_service.call_args.kwargs
            self.assertEqual(service_kwargs["workspace"], isolated.resolve())
            self.assertEqual(service_kwargs["semantic_parallelism"], 1)
            self.assertTrue(
                callable(service_kwargs["seal_contact_governance_timeout"])
            )
            with patch("archeos.wechat_digest.detect_clean_git_head", return_value="a" * 40):
                handoff = service_kwargs["semantic_handoff_factory"]()
            self.assertTrue(callable(handoff._contact_pre_attempt_proof))
            self.assertIn(
                binding.conversation_key,
                str(service_kwargs["run_store"].root),
            )

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_prints_business_summary(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.run.return_value = WechatDigestResult(
            run_id="run_" + "a" * 32,
            new_messages=3,
            new_attachments=1,
            durable_information=4,
            local_only=1,
            unsupported=2,
            pending_human=1,
            context_objects=2,
            checkpoint_published=True,
            replayed=False,
            capture_ms=3,
            snapshot_publish_ms=4,
            snapshot_readback_ms=5,
            slice_build_ms=6,
            semantic_wall_ms=12,
            commit_wall_ms=7,
            governance_app_server_starts=2,
            governance_threads=3,
            governance_turns=4,
            governance_startup_wall_ms=101,
            governance_turn_wall_ms_sum=202,
            governance_turn_wall_ms_max=77,
            governance_timeouts=1,
            governance_failures=2,
            governance_wall_ms=8,
            checkpoint_wall_ms=2,
            total_wall_ms=47,
        )
        capture_provider.return_value.resolve_contact.return_value = (
            WechatContactBinding(
                "wechat_conversation_" + "1" * 32,
                "wxid_synthetic",
                "Synthetic Contact",
                False,
            )
        )
        capture_provider.return_value.scoped.return_value = (
            capture_provider.return_value
        )
        output = StringIO()
        with patch(
            "archeos.cli.WechatContactSelectionStore.bind"
        ), patch(
            "archeos.cli.build_contact_acceptance_pack",
            return_value=(Path("/private/result.json"), Path("/private/result.md")),
        ), redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--contact",
                    "Synthetic Contact",
                    "--authority-ref",
                    self.CONTACT_AUTHORITY_REF,
                    "--from-now",
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("新增消息：3", output.getvalue())
        self.assertIn("待你判断：1", output.getvalue())
        self.assertIn("已保留但未形成长期信息：0", output.getvalue())
        self.assertIn("已形成长期信息但治理未完整确认：0", output.getvalue())
        self.assertIn("checkpoint：已推进", output.getvalue())
        self.assertIn(
            "历史累计治理记录（app-server / thread / turn）：2 / 3 / 4",
            output.getvalue(),
        )
        self.assertIn(
            "历史累计治理记录（耗时，ms）："
            "startup=101, turn_sum=202, turn_max=77",
            output.getvalue(),
        )
        self.assertIn("本次治理耗时（ms）：8", output.getvalue())
        self.assertIn(
            "历史累计治理记录（timeout / failure）：1 / 2",
            output.getvalue(),
        )
        self.assertNotIn("total=", output.getvalue())
        performance_line = next(
            line for line in output.getvalue().splitlines()
            if line.startswith("性能指标：")
        )
        performance = json.loads(performance_line.removeprefix("性能指标："))
        self.assertEqual(performance["dominant_stage"], "semantic")
        self.assertEqual(performance["commit_wall_ms"], 7)
        self.assertEqual(performance["governance_wall_ms"], 8)
        self.assertEqual(performance["checkpoint_wall_ms"], 2)
        self.assertEqual(performance["total_wall_ms"], 47)
        self.assertEqual(performance["capture_attempts"], 0)
        self.assertEqual(performance["capture_successes"], 0)
        self.assertEqual(performance["capture_reasons"], [])
        self.assertEqual(performance["materialized_cursor_rows"], 0)
        digest_service.return_value.run.assert_called_once_with(
            since=None,
            from_now=True,
            all_history=False,
            max_terminal_items=None,
        )
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_reports_safe_segment_boundary(
        self,
        require_workspace: Mock,
        _capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.run.return_value = WechatDigestResult(
            run_id="run_" + "b" * 32,
            new_messages=4,
            new_attachments=0,
            durable_information=2,
            local_only=0,
            unsupported=0,
            pending_human=1,
            context_objects=0,
            checkpoint_published=False,
            replayed=True,
            segment_safe_stopped=True,
            segment_items_completed=1,
            segment_remaining_items=3,
            segment_stop_reason="item_limit",
            segment_receipt_fingerprint="sha256:" + "c" * 64,
        )
        _capture_provider.return_value.resolve_contact.return_value = (
            WechatContactBinding(
                "wechat_conversation_" + "2" * 32,
                "wxid_synthetic_2",
                "Synthetic Contact 2",
                False,
            )
        )
        _capture_provider.return_value.scoped.return_value = (
            _capture_provider.return_value
        )
        output = StringIO()
        with patch(
            "archeos.cli.WechatContactSelectionStore.bind"
        ), patch(
            "archeos.cli.build_contact_acceptance_pack",
            return_value=(Path("/private/result.json"), Path("/private/result.md")),
        ), redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--contact",
                    "Synthetic Contact 2",
                    "--authority-ref",
                    self.CONTACT_AUTHORITY_REF,
                    "--max-items-per-run",
                    "1",
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("本段已安全完成：1 项；当前窗口剩余 3 项。", output.getvalue())
        digest_service.return_value.run.assert_called_once_with(
            since=None,
            from_now=False,
            all_history=False,
            max_terminal_items=1,
        )

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_resolves_semantic_unknown_with_zero_call_summary(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.resolve_semantic_unknown.return_value = {
            "continuation": {"next_global_ordinal": 167},
            "resolution_receipt_fingerprint": "sha256:" + "f" * 64,
        }
        authority = Path("/private/unknown-authority.json")
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-semantic-unknown",
                    "--semantic-unknown-authority-file",
                    str(authority),
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 166)
        self.assertEqual(payload["global_unknown"], 0)
        self.assertEqual(payload["next_global_ordinal"], 167)
        self.assertEqual(payload["preserved_but_unabsorbed"], 1)
        digest_service.return_value.resolve_semantic_unknown.assert_called_once_with(
            authority_manifest_file=authority
        )
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_resolves_timeout_212_with_zero_call_summary(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.resolve_semantic_timeout_212.return_value = {
            "continuation": {"next_global_ordinal": 213},
            "resolution_receipt_fingerprint": "sha256:" + "f" * 64,
        }
        authority = Path("/private/timeout-212-authority.json")
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-semantic-timeout-212",
                    "--semantic-timeout-212-authority-file",
                    str(authority),
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 212)
        self.assertEqual(payload["global_unknown"], 0)
        self.assertEqual(payload["next_global_ordinal"], 213)
        self.assertEqual(payload["remaining"], 788)
        digest_service.return_value.resolve_semantic_timeout_212.assert_called_once_with(
            authority_manifest_file=authority
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_builds_generic_attempt_candidate_zero_calls(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.build_semantic_attempt_resolution_manifest.return_value = {
            "activation_total": 371,
            "activation_unknown_count": 1,
            "continuation": {
                "next_global_ordinal": 372,
                "absolute_cap": 1000,
            },
            "payload_fingerprint": "sha256:" + "e" * 64,
        }
        candidate = Path("/private/attempt-candidate.json")
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/184"
            "#issuecomment-5407349371"
        )
        observed_at = "2026-08-25T12:00:00Z"
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--prepare-semantic-attempt-resolution",
                    "--semantic-attempt-candidate-file",
                    str(candidate),
                    "--authority-ref",
                    authority_ref,
                    "--semantic-attempt-observed-at",
                    observed_at,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 371)
        self.assertEqual(payload["global_unknown"], 1)
        self.assertEqual(payload["next_global_ordinal"], 372)
        digest_service.return_value.build_semantic_attempt_resolution_manifest.assert_called_once_with(
            candidate_file=candidate,
            authority_ref=authority_ref,
            observed_at=observed_at,
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_resolves_generic_attempt_with_zero_call_summary(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.resolve_semantic_attempt.return_value = {
            "global_ordinal": 371,
            "continuation": {
                "next_global_ordinal": 372,
                "absolute_cap": 1000,
            },
            "resolution_receipt_fingerprint": "sha256:" + "f" * 64,
        }
        authority = Path("/private/attempt-authority.json")
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-semantic-attempt",
                    "--semantic-attempt-authority-file",
                    str(authority),
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 371)
        self.assertEqual(payload["global_unknown"], 0)
        self.assertEqual(payload["next_global_ordinal"], 372)
        self.assertEqual(payload["remaining"], 629)
        digest_service.return_value.resolve_semantic_attempt.assert_called_once_with(
            authority_manifest_file=authority
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_seals_governance_timeout_with_zero_call_summary(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.seal_governance_timeout.return_value = {
            "semantic_provider_calls": 0,
            "governance_provider_calls": 0,
            "global_attempt_total": 176,
            "global_unknown": 0,
            "next_global_ordinal": 177,
            "absolute_cap": 1000,
            "governance_preserved_but_incomplete": 1,
            "provider_retry_permitted": False,
        }
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                ["wechat", "digest", "--seal-governance-timeout"]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["next_global_ordinal"], 177)
        self.assertEqual(payload["governance_preserved_but_incomplete"], 1)
        self.assertFalse(payload["provider_retry_permitted"])
        digest_service.return_value.seal_governance_timeout.assert_called_once_with()
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatContactSelectionStore")
    @patch("archeos.cli.ContactSynthesisStore")
    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_contact_isolated_seal_routes_to_service_without_capture_or_provider(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
        synthesis_store: Mock,
        selection_store: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            require_workspace.return_value = WorkspaceConfig(
                Path(temp_dir) / "workspace", Path(temp_dir) / "config"
            )
            binding = SimpleNamespace(conversation_key="contact-1")
            capture_provider.return_value.resolve_contact.return_value = binding
            capture_provider.return_value.scoped.return_value = Mock()
            synthesis_store.return_value.read_provider_authority.return_value = {
                "authority_ref": "https://github.com/example/repo/issues/1",
                "absolute_cap": 50,
            }
            digest_service.return_value.seal_governance_timeout.return_value = {
                "semantic_provider_calls": 0,
                "governance_provider_calls": 0,
                "governance_preserved_but_incomplete": 1,
                "provider_retry_permitted": False,
            }
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "wechat",
                        "digest",
                        "--contact",
                        "contact-1",
                        "--isolated-acceptance-dir",
                        str(Path(temp_dir) / "isolated"),
                        "--seal-governance-timeout",
                    ]
                )
            self.assertEqual(result, 0)
            digest_service.return_value.seal_governance_timeout.assert_called_once_with()
            digest_service.return_value.run.assert_not_called()
            capture_provider.return_value.discover_contacts.assert_not_called()
            capture_provider.return_value.scoped.assert_called_once_with(binding)

    @patch("archeos.cli.WechatContactSelectionStore")
    @patch("archeos.cli.ContactSynthesisStore")
    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_contact_isolated_startup_transport_seal_routes_without_capture_or_provider(
        self, require_workspace: Mock, capture_provider: Mock, digest_service: Mock,
        synthesis_store: Mock, selection_store: Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            require_workspace.return_value = WorkspaceConfig(Path(temp_dir) / "workspace", Path(temp_dir) / "config")
            binding = SimpleNamespace(conversation_key="contact-1")
            capture_provider.return_value.resolve_contact.return_value = binding
            capture_provider.return_value.scoped.return_value = Mock()
            synthesis_store.return_value.read_provider_authority.return_value = {
                "authority_ref": "https://github.com/example/repo/issues/1", "absolute_cap": 50,
            }
            digest_service.return_value.seal_contact_governance_startup_transport_failure.return_value = {
                "semantic_provider_calls": 0, "governance_provider_calls": 0,
            }
            with redirect_stdout(StringIO()):
                result = main(["wechat", "digest", "--contact", "contact-1",
                    "--isolated-acceptance-dir", str(Path(temp_dir) / "isolated"),
                    "--seal-contact-governance-startup-transport-failure"])
            self.assertEqual(result, 0)
            digest_service.return_value.seal_contact_governance_startup_transport_failure.assert_called_once_with()
            digest_service.return_value.run.assert_not_called()
            capture_provider.return_value.discover_contacts.assert_not_called()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_startup_transport_seal_requires_contact_and_isolated_before_construction(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        for arguments in (
            ["wechat", "digest", "--seal-contact-governance-startup-transport-failure"],
            [
                "wechat", "digest", "--contact", "contact-1",
                "--seal-contact-governance-startup-transport-failure",
            ],
        ):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(arguments), 2)
        require_workspace.assert_not_called()
        capture_provider.assert_not_called()
        digest_service.assert_not_called()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_resolves_governance_startup_with_business_summary(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.resolve_governance_startup_failure.return_value = {
            "receipt_fingerprint": "sha256:" + "f" * 64,
        }
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/150"
            "#issuecomment-1234567890"
        )
        authority = Path("/private/issue-150-authority.json")
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-governance-startup-failure",
                    "--governance-startup-authority-file",
                    str(authority),
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["durable_information_preserved"], 4)
        self.assertEqual(payload["previous_governance_model_turns"], 0)
        self.assertEqual(payload["safe_restart_attempts_available"], 1)
        self.assertEqual(payload["objects_created"], 0)
        self.assertFalse(payload["checkpoint_published"])
        self.assertIn("已保留4条长期信息", payload["message"])
        digest_service.return_value.resolve_governance_startup_failure.assert_called_once_with(
            authority_ref=authority_ref,
            authority_manifest_file=authority,
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_resolves_failed_closed_continuation_zero_calls(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.resolve_failed_closed_continuation.return_value = {
            "receipt_fingerprint": "sha256:" + "e" * 64,
        }
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
            "#issuecomment-1234567890"
        )
        authority = Path("/private/issue-154-authority.json")
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-failed-closed-continuation",
                    "--failed-closed-authority-file",
                    str(authority),
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 298)
        self.assertEqual(payload["global_unknown"], 0)
        self.assertEqual(payload["next_global_ordinal"], 299)
        self.assertFalse(payload["checkpoint_published"])
        digest_service.return_value.resolve_failed_closed_continuation.assert_called_once_with(
            authority_ref=authority_ref,
            authority_manifest_file=authority,
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_resolves_multi_governance_startup_zero_calls(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        method = (
            digest_service.return_value.resolve_multi_governance_startup_failure
        )
        def resolve(**kwargs):
            for stage in ("capture_skipped", "verify", "write", "readback"):
                kwargs["progress"](stage)
            return {"receipt_fingerprint": "sha256:" + "d" * 64}

        method.side_effect = resolve
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
            "#issuecomment-1234567890"
        )
        authority = Path("/private/issue-168-authority.json")
        output = StringIO()
        progress = StringIO()
        with redirect_stdout(output), redirect_stderr(progress):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-multi-governance-startup-failure",
                    "--multi-governance-startup-authority-file",
                    str(authority),
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["durable_information_preserved"], 3)
        self.assertEqual(payload["safe_restart_attempts_available"], 1)
        self.assertFalse(payload["checkpoint_published"])
        method.assert_called_once()
        called = method.call_args.kwargs
        self.assertEqual(called["authority_ref"], authority_ref)
        self.assertEqual(called["authority_manifest_file"], authority)
        self.assertTrue(callable(called["progress"]))
        self.assertIn("无需重新读取微信历史", progress.getvalue())
        self.assertIn("正在核对持久化证据", progress.getvalue())
        self.assertIn("正在写入恢复许可", progress.getvalue())
        self.assertIn("正在读回并确认恢复状态", progress.getvalue())
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.require_workspace")
    def test_multi_governance_startup_cli_requires_private_manifest(
        self, require_workspace: Mock
    ) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-multi-governance-startup-failure",
                    "--authority-ref",
                    (
                        "https://github.com/leevi2010-cursor/ArcheOS/issues/168"
                        "#issuecomment-1234567890"
                    ),
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("必须指定私有 authority file", output.getvalue())
        require_workspace.assert_not_called()

    @patch("archeos.cli.require_workspace")
    def test_failed_closed_recovery_cli_requires_exact_private_manifest(
        self, require_workspace: Mock
    ) -> None:
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/154"
            "#issuecomment-1234567890"
        )
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--resolve-failed-closed-continuation",
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("必须指定私有 authority file", output.getvalue())
        require_workspace.assert_not_called()

        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--failed-closed-authority-file",
                    "/private/unbound.json",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("只能与恢复入口一起使用", output.getvalue())
        require_workspace.assert_not_called()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_installs_maintenance_continuation_zero_calls(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/127"
            "#issuecomment-1234567890"
        )
        digest_service.return_value.install_semantic_maintenance_continuation.return_value = {
            "activation_total": 176,
            "activation_unknown_count": 0,
            "next_global_ordinal": 177,
            "absolute_cap": 1000,
            "previous_reviewed_git_head": "8" * 40,
            "reviewed_git_head": "9" * 40,
            "continuation_fingerprint": "sha256:" + "a" * 64,
        }
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--install-semantic-maintenance-continuation",
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 176)
        self.assertEqual(payload["next_global_ordinal"], 177)
        digest_service.return_value.install_semantic_maintenance_continuation.assert_called_once_with(
            authority_ref=authority_ref
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_installs_generic_reviewed_head_continuation_zero_calls(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/176"
            "#issuecomment-5402000000"
        )
        method = (
            digest_service.return_value
            .install_semantic_reviewed_head_continuation
        )
        method.return_value = {
            "activation_total": 361,
            "activation_unknown_count": 0,
            "next_global_ordinal": 362,
            "absolute_cap": 1000,
            "previous_reviewed_git_head": "8" * 40,
            "reviewed_git_head": "9" * 40,
            "continuation_fingerprint": "sha256:" + "a" * 64,
        }
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--install-semantic-reviewed-head-continuation",
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 361)
        self.assertEqual(payload["next_global_ordinal"], 362)
        method.assert_called_once_with(authority_ref=authority_ref)
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_installs_gate_c_continuation_zero_calls(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/146"
            "#issuecomment-1234567890"
        )
        digest_service.return_value.install_semantic_gate_c_continuation.return_value = {
            "activation_total": 220,
            "activation_unknown_count": 0,
            "next_global_ordinal": 221,
            "absolute_cap": 1000,
            "previous_reviewed_git_head": "8" * 40,
            "reviewed_git_head": "9" * 40,
            "continuation_fingerprint": "sha256:" + "a" * 64,
        }
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--install-semantic-gate-c-continuation",
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 220)
        self.assertEqual(payload["next_global_ordinal"], 221)
        digest_service.return_value.install_semantic_gate_c_continuation.assert_called_once_with(
            authority_ref=authority_ref
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_installs_segmented_gate_c_continuation_zero_calls(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        authority_ref = (
            "https://github.com/leevi2010-cursor/ArcheOS/issues/148"
            "#issuecomment-1234567890"
        )
        method = (
            digest_service.return_value
            .install_semantic_segmented_gate_c_continuation
        )
        method.return_value = {
            "activation_total": 297,
            "activation_unknown_count": 0,
            "next_global_ordinal": 298,
            "absolute_cap": 1000,
            "previous_reviewed_git_head": "8" * 40,
            "reviewed_git_head": "9" * 40,
            "continuation_fingerprint": "sha256:" + "a" * 64,
        }
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--install-semantic-segmented-gate-c-continuation",
                    "--authority-ref",
                    authority_ref,
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["semantic_provider_calls"], 0)
        self.assertEqual(payload["governance_provider_calls"], 0)
        self.assertEqual(payload["global_attempt_total"], 297)
        self.assertEqual(payload["next_global_ordinal"], 298)
        method.assert_called_once_with(authority_ref=authority_ref)
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_prepares_without_running_digest(self, require_workspace: Mock, capture_provider: Mock, digest_service: Mock) -> None:
        require_workspace.return_value = WorkspaceConfig(Path("/workspace"), Path("/config"))
        digest_service.return_value.prepare_next_semantic.return_value = WechatSemanticPreparation("run_" + "a" * 32, "repr_" + "b" * 32, ("unit_" + "c" * 64,))
        output = StringIO()
        with redirect_stdout(output):
            result = main(["wechat", "digest", "--prepare-next-semantic"])
        self.assertEqual(result, 0)
        self.assertIn('"semantic_provider_calls": 0', output.getvalue())
        self.assertIn('"governance_provider_calls": "unavailable"', output.getvalue())
        digest_service.return_value.prepare_next_semantic.assert_called_once_with(batch_size=40)
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_explicitly_upgrades_active_v2_history_scope(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        run_id = "run_" + "a" * 32
        digest_service.return_value.upgrade_active_v2_all_history.return_value = run_id
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                ["wechat", "digest", "--upgrade-active-v2-all-history"]
            )
        self.assertEqual(result, 0)
        self.assertIn('"semantic_provider_calls": 0', output.getvalue())
        digest_service.return_value.upgrade_active_v2_all_history.assert_called_once_with()
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_installs_one_global_semantic_authority(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.install_semantic_authority.return_value = {
            "baseline_total": 80,
            "max_new": 20,
            "absolute_cap": 100,
            "global_authority_fingerprint": "sha256:" + "a" * 64,
        }
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--install-semantic-authority",
                    "--inventory-authority-file",
                    "/private/semantic-inventory-authority.json",
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn('"semantic_provider_calls": 0', output.getvalue())
        digest_service.return_value.install_semantic_authority.assert_called_once_with(
            inventory_authority_file=Path(
                "/private/semantic-inventory-authority.json"
            ),
        )
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatDigestService")
    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_digest_installs_fixed_cap1000_semantic_extension(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
        digest_service: Mock,
    ) -> None:
        require_workspace.return_value = WorkspaceConfig(
            Path("/workspace"), Path("/config")
        )
        digest_service.return_value.install_semantic_authority_extension.return_value = {
            "activation_total": 81,
            "previous_absolute_cap": 100,
            "new_absolute_cap": 1000,
            "first_authorized_ordinal": 82,
            "last_authorized_ordinal": 1000,
            "extension_fingerprint": "sha256:" + "e" * 64,
        }
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                ["wechat", "digest", "--install-semantic-authority-extension"]
            )
        self.assertEqual(result, 0)
        self.assertIn('"semantic_provider_calls": 0', output.getvalue())
        self.assertIn('"new_absolute_cap": 1000', output.getvalue())
        digest_service.return_value.install_semantic_authority_extension.assert_called_once_with()
        digest_service.return_value.run.assert_not_called()
        capture_provider.assert_called_once()

    @patch("archeos.cli.WechatCliCaptureProvider")
    @patch("archeos.cli.require_workspace")
    def test_wechat_semantic_authority_file_requires_install_before_workspace(
        self,
        require_workspace: Mock,
        capture_provider: Mock,
    ) -> None:
        with redirect_stdout(StringIO()):
            result = main(
                [
                    "wechat",
                    "digest",
                    "--inventory-authority-file",
                    "/private/semantic-inventory-authority.json",
                ]
            )
        self.assertEqual(result, 2)
        require_workspace.assert_not_called()
        capture_provider.assert_not_called()

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_managed_audio")
    def test_constructs_file_backed_providers(
        self,
        process_managed_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_managed_audio.return_value = Path("/tmp/package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        with (
            patch("archeos.cli.require_workspace", return_value=WorkspaceConfig(Path("/workspace"), Path("/config"))),
            redirect_stdout(StringIO()),
        ):
            result = main(
                [
                    "process", "sample.wav", "--transcript", "transcript.json",
                    "--speaker-map", "speakers.json", "--analysis-file", "analysis.json",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(process_managed_audio.call_count, 1)
        self.assertEqual(process_managed_audio.call_args.args[0], "sample.wav")
        transcriber, speaker_provider, analysis_provider = process_managed_audio.call_args.args[
            3:
        ]
        self.assertEqual(transcriber.transcript_file, Path("transcript.json"))
        self.assertEqual(speaker_provider.speaker_map, Path("speakers.json"))
        self.assertEqual(analysis_provider.analysis_file, Path("analysis.json"))
        self.assertEqual(ingest_processing_package.call_count, 1)
        store = ingest_processing_package.call_args.args[1]
        self.assertEqual(
            store.path,
            Path("/workspace/03_information/atomic_information.jsonl"),
        )

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_managed_audio")
    def test_uses_automatic_diarization_and_codex_sdk_by_default(
        self,
        process_managed_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_managed_audio.return_value = Path("/tmp/package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        with (
            patch("archeos.cli.require_workspace", return_value=WorkspaceConfig(Path("/workspace"), Path("/config"))),
            redirect_stdout(StringIO()),
        ):
            result = main(["process", "sample.wav", "--transcript", "transcript.json"])
        self.assertEqual(result, 0)
        speaker_provider = process_managed_audio.call_args.args[4]
        analysis_provider = process_managed_audio.call_args.args[5]
        self.assertIsInstance(speaker_provider, PyannoteSpeakerProvider)
        self.assertIsInstance(analysis_provider, CodexAnalysisProvider)

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.RepresentationInformationService.extract")
    def test_representation_extract_requires_an_explicit_file_provider(
        self,
        extract: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        extract.return_value = Path("/tmp/representation-package")
        ingest_processing_package.return_value = IngestionResult(0, 0, 0, ())
        arguments = [
            "information",
            "--store",
            "store.jsonl",
            "extract",
            "repr_" + "a" * 64,
            "--managed-root",
            "managed",
            "--representation-root",
            "representations",
            "--output-root",
            "information",
        ]
        with (
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(arguments)
        self.assertEqual(error.exception.code, 2)
        extract.assert_not_called()

        with redirect_stdout(StringIO()):
            self.assertEqual(main([*arguments, "--analysis-file", "fixture.json"]), 0)
        fixture_provider = extract.call_args.args[-1]
        self.assertIsInstance(fixture_provider, FileRepresentationAnalysisProvider)
        self.assertEqual(fixture_provider.path, Path("fixture.json"))

    def test_object_commands_return_human_readable_world_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "world-model.sqlite3"

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "object",
                        "--database",
                        str(database),
                        "create",
                        "--name",
                        "Synthetic Operations",
                        "--role",
                        "business_line",
                    ]
                )
            created = json.loads(output.getvalue())
            object_id = created["object_id"]
            self.assertEqual(result, 0)
            self.assertTrue(object_id.startswith("obj_"))
            self.assertEqual(created["current_name"], "Synthetic Operations")
            self.assertEqual(created["roles"], ["business_line"])

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "object",
                            "--database",
                            str(database),
                            "rename",
                            object_id,
                            "--name",
                            "Renamed Operations",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "object",
                            "--database",
                            str(database),
                            "add-role",
                            object_id,
                            "brand",
                        ]
                    ),
                    0,
                )

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    ["object", "--database", str(database), "show", object_id]
                )
            shown = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(shown["object_id"], object_id)
            self.assertEqual(shown["current_name"], "Renamed Operations")
            self.assertEqual(shown["roles"], ["brand", "business_line"])

    def test_digest_information_uses_explicit_file_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "world.sqlite3"
            information_path = root / "information.jsonl"
            proposal_path = root / "proposals.jsonl"
            journal_path = root / "journal.jsonl"
            interpretation_path = root / "interpretation.json"
            with SQLiteWorldModelRepository(database) as repository:
                record = repository.create_object("CLI Target")

            source_id = "synthetic-cli-source"
            candidate_id = "synthetic-cli-candidate"
            atomic_information_id = (
                "atomic_info_"
                + hashlib.sha256(f"{source_id}\0{candidate_id}".encode()).hexdigest()[
                    :32
                ]
            )
            JsonlAtomicInformationStore(information_path).ingest_batch(
                (
                    AtomicInformationRevision(
                        atomic_information_id=atomic_information_id,
                        revision_number=1,
                        revision_id=f"{atomic_information_id}-r0001",
                        origin_source_id=source_id,
                        origin_candidate_id=candidate_id,
                        origin_fingerprint=hashlib.sha256(b"synthetic-cli").hexdigest(),
                        statement="CLI Target is an active project.",
                        semantic_type="requirement",
                        raw_concerns=("CLI Target",),
                        related_object_ids=(),
                        source_evidence=(
                            EvidenceRecord(
                                source_id=source_id,
                                artifact="synthetic.md",
                                segment=1,
                                speaker="Speaker_1",
                                start="00:00:01.000",
                                end="00:00:02.000",
                                excerpt="CLI Target is an active project.",
                            ),
                        ),
                        context="Synthetic CLI smoke context.",
                        confidence=0.9,
                        created_at="2026-08-11T00:00:00+00:00",
                        revision_reason="initial_ingestion",
                    ),
                )
            )
            fields = {
                "target_object_id": record.object_id,
                "secondary_object_id": None,
                "name": None,
                "role": "project",
                "relation": None,
                "relationship_id": None,
                "lifecycle_state": None,
                "start_at": None,
                "actual_end_at": None,
                "target_end_at": None,
                "completion_condition": None,
            }
            interpretation_path.write_text(
                json.dumps(
                    {
                        "operations": [{"kind": "add_role", **fields}],
                        "rationale": "Synthetic deterministic CLI interpretation.",
                        "evidence_sufficient": True,
                        "conflict": False,
                        "ambiguous": False,
                        "claim": None,
                    }
                ),
                encoding="utf-8",
            )

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "digest",
                        "--database",
                        str(database),
                        "--information-store",
                        str(information_path),
                        "--proposal-store",
                        str(proposal_path),
                        "--journal",
                        str(journal_path),
                        "information",
                        atomic_information_id,
                        "--interpretation-file",
                        str(interpretation_path),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "automatic")
            with SQLiteWorldModelRepository(database) as repository:
                self.assertEqual(
                    repository.list_roles(record.object_id, active_only=True)[0].role,
                    "project",
                )
            self.assertFalse(proposal_path.exists())
            self.assertTrue(journal_path.exists())

    def test_context_build_cli_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "world.sqlite3"
            information = root / "information.jsonl"
            proposals = root / "proposals.jsonl"
            journal = root / "journal.jsonl"
            with SQLiteWorldModelRepository(database) as repository:
                record = repository.create_object("CLI Context Target")
            output = StringIO()
            with redirect_stdout(output):
                result = main([
                    "context", "--database", str(database),
                    "--information-store", str(information),
                    "--proposal-store", str(proposals), "--journal", str(journal),
                    "build", "--scope", "object", record.object_id,
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["root"]["object_id"], record.object_id)
            self.assertEqual(payload["metadata"]["complete"], True)

    def test_context_build_cli_invalid_root_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "world.sqlite3"
            output = StringIO()
            with redirect_stdout(output):
                result = main([
                    "context", "--database", str(database), "build",
                    "--scope", "object", "missing",
                ])
            self.assertEqual(result, 1)
            self.assertIn("error:", output.getvalue())
            self.assertFalse(database.exists())

    def test_source_cli_admit_show_verify_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root / "synthetic.txt"
            external.write_bytes(b"synthetic source")
            managed_root = root / "managed"
            source_id = "src_" + "a" * 32

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "source",
                        "admit",
                        str(external),
                        "--source-id",
                        source_id,
                        "--managed-root",
                        str(managed_root),
                    ]
                )
            admitted = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(admitted["source"]["source_id"], source_id)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "show",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["source_id"], source_id)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "verify",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertTrue(json.loads(output.getvalue())["verified"])

            target = root / "restored.txt"
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "restore",
                            source_id,
                            str(target),
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertTrue(json.loads(output.getvalue())["verified"])
            self.assertEqual(target.read_bytes(), external.read_bytes())

    def test_source_handoff_cli_write_and_show(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            root = Path(temp)
            external = root / "synthetic.txt"
            external.write_bytes(b"synthetic handoff")
            managed_root = root / "managed"
            source_id = "src_" + "c" * 32

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "admit",
                            str(external),
                            "--source-id",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "handoff",
                            "write",
                            source_id,
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["status"], "written")

            output = StringIO()
            marker = external.with_name(f"{external.name}.archeos.md")
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "source",
                            "handoff",
                            "show",
                            str(marker),
                            "--managed-root",
                            str(managed_root),
                        ]
                    ),
                    0,
                )
            shown = json.loads(output.getvalue())
            self.assertEqual(shown["marker"]["source_id"], source_id)
            self.assertTrue(shown["source_verified"])

    def test_process_managed_source_synthetic_end_to_end_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "synthetic.wav"
            with wave.open(str(original), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16_000)
                audio.writeframes(b"\x00\x00" * 1_600)
            source_id = "src_" + "b" * 32
            managed_root = root / "managed"
            output_root = root / "processing"
            information_store = root / "information.jsonl"
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps(
                    {
                        "text": "Synthetic decision. Synthetic ambiguity.",
                        "segments": [
                            {"text": "Synthetic decision.", "start": 0, "end": 1},
                            {"text": "Synthetic ambiguity.", "start": 1, "end": 2},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            speakers = root / "speakers.json"
            speakers.write_text(
                json.dumps({"segments": [{"segment": 1, "speaker": "Speaker_1"}]}),
                encoding="utf-8",
            )
            analysis = root / "analysis.json"
            analysis.write_text(
                json.dumps(
                    {
                        "meeting_summary": {
                            "topic": "Synthetic smoke",
                            "participants": ["Speaker_1"],
                            "discussion_goal": "Validate Managed Source provenance.",
                            "main_discussion": ["Synthetic processing."],
                            "key_viewpoints": ["Managed Source is authoritative."],
                            "agreements": ["Use source_id."],
                            "disagreements": [],
                            "unresolved_questions": ["Synthetic ambiguity."],
                            "next_actions": ["Review the output."],
                        },
                        "atomic_information_candidates": [
                            {
                                "statement": "Synthetic decision.",
                                "semantic_type": "decision",
                                "concerns": ["Synthetic smoke"],
                                "evidence_segments": [1],
                                "context": "Synthetic test only.",
                                "confidence": 0.9,
                            }
                        ],
                        "residue": [
                            {
                                "evidence_segments": [2],
                                "reason_not_absorbed": "Synthetic ambiguity.",
                                "future_value_or_uncertainty": "Needs synthetic review.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            "source", "admit", str(original), "--source-id", source_id,
                            "--managed-root", str(managed_root),
                        ]
                    ),
                    0,
                )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "process", source_id,
                            "--managed-root", str(managed_root),
                            "--output-root", str(output_root),
                            "--information-store", str(information_store),
                            "--transcript", str(transcript),
                            "--speaker-map", str(speakers),
                            "--analysis-file", str(analysis),
                        ]
                    ),
                    0,
                )
            package = output_root / source_id
            manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "1.2")
            self.assertEqual(manifest["source"]["id"], source_id)
            self.assertNotIn("path", manifest["source"])
            self.assertNotIn(str(original), (package / "transcript.md").read_text())
            candidate = json.loads(
                (package / "atomic_information_candidates.jsonl").read_text().splitlines()[0]
            )
            self.assertEqual(candidate["source_evidence"][0]["source_id"], source_id)
            self.assertEqual(
                JsonlAtomicInformationStore(information_store)
                .list_atomic_information()[0]
                .source_evidence[0]
                .source_id,
                source_id,
            )
            original.unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(["source", "verify", source_id, "--managed-root", str(managed_root)]),
                    0,
                )

    def test_process_rejects_external_path_as_unknown_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "process", str(Path(temp) / "external.wav"),
                        "--managed-root", str(Path(temp) / "managed"),
                        "--output-root", str(Path(temp) / "processing"),
                        "--information-store", str(Path(temp) / "information.jsonl"),
                    ]
                )
            self.assertEqual(result, 1)
            self.assertIn("source_id", output.getvalue())

    @patch("archeos.cli.ingest_processing_package")
    @patch("archeos.cli.process_managed_audio")
    def test_processing_publish_failure_never_triggers_ingestion(
        self,
        process_managed_audio: Mock,
        ingest_processing_package: Mock,
    ) -> None:
        process_managed_audio.side_effect = ProcessingError("cannot publish safely")
        with redirect_stdout(StringIO()):
            result = main(["process", "src_" + "a" * 32])

        self.assertEqual(result, 1)
        ingest_processing_package.assert_not_called()


if __name__ == "__main__":
    unittest.main()
