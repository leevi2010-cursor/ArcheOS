from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from archeos.atomic_information import (
    JsonlAtomicInformationStore,
    ingest_processing_package,
)
from archeos.digestion import InterpretationResult, WorldModelOperation
from archeos.representation import LocalRepresentationRepository
from archeos.representation_information import (
    RepresentationAnalysisResult,
    RepresentationCandidateDraft,
    RepresentationInformationService,
)
from archeos.source import LocalManagedSourceRepository
from archeos.wechat_capture_helper import _window_upper
from archeos.wechat_digest import (
    CapturedAttachment,
    CapturedMessage,
    DeterministicPrivacyGate,
    WechatCapture,
    WechatCliCaptureProvider,
    WechatCursor,
    WechatDigestError,
    WechatDigestRunStore,
    WechatDigestService,
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
        self.calls: list[tuple[WechatCursor, WechatCursor | None, bool]] = []
        self.outputs: list[WechatCapture] = []

    def capture(
        self,
        after_cursor: WechatCursor,
        *,
        upper_bound: WechatCursor | None = None,
        observe_only: bool = False,
    ) -> WechatCapture:
        self.calls.append((after_cursor, upper_bound, observe_only))
        ordered = tuple(sorted(self.messages, key=lambda item: item.cursor))
        remaining = tuple(item for item in ordered if item.cursor > after_cursor)
        if upper_bound is not None:
            observed_upper = upper_bound
        elif remaining and self.window_seconds is not None:
            cutoff = remaining[0].timestamp + self.window_seconds
            observed_upper = tuple(
                item for item in remaining if item.timestamp < cutoff
            )[-1].cursor
        else:
            observed_upper = ordered[-1].cursor if ordered else after_cursor
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
    name = "synthetic-wechat-digest"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, batch):
        self.calls += 1
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
                for unit in batch.anchor_units
            ),
            residue=(),
        )


class SyntheticSemanticHandoff:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.provider = SyntheticAnalysisProvider()
        self.failures_remaining = 0

    def execute(self, representation_id: str):
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
        return SimpleNamespace(ingestion=ingest_processing_package(package, store))


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
        non_empty = [item for item in capture.outputs if item.messages]
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

    def test_prepare_stops_at_multi_batch_item(self) -> None:
        capture = SyntheticCaptureProvider([message(number) for number in range(1, 42)])
        self.semantic.failures_remaining = 1
        service = self.service(capture)
        with self.assertRaises(WechatDigestError):
            service.run(all_history=True)
        with self.assertRaisesRegex(WechatDigestError, "多个 batch"):
            service.prepare_next_semantic(batch_size=40)
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
