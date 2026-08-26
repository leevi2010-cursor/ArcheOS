from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    JsonlAtomicInformationStore,
)
from archeos.wechat_contact import (
    LegacyMessageOverlap,
    OverlapFilteringWechatCaptureProvider,
    WechatContactSelectionStore,
    build_contact_acceptance_pack,
    committed_legacy_message_keys,
    legacy_message_overlap,
)
from archeos.wechat_contact_synthesis import ContactSynthesisStore
from archeos.wechat_digest import (
    CapturedMessage,
    WechatCapture,
    WechatContactBinding,
    WechatCursor,
    WechatDigestError,
)


def _binding(number: str = "1", name: str = "联系人甲") -> WechatContactBinding:
    return WechatContactBinding(
        "wechat_conversation_" + number * 32,
        "wxid_" + number * 8,
        name,
        False,
    )


def _message(number: int) -> CapturedMessage:
    binding = _binding()
    key = "wechat_message_" + f"{number:032x}"
    cursor = WechatCursor(1_700_000_000 + number, binding.conversation_key, key)
    return CapturedMessage(
        binding.conversation_key,
        binding.provider_conversation_id,
        binding.display_name,
        False,
        key,
        cursor,
        "发送人",
        "text",
        cursor.timestamp,
        "2026-08-26T10:00:00+08:00",
        f"消息 {number}",
        f"消息 {number}",
        (),
    )


class ContactSelectionTests(unittest.TestCase):
    def test_stable_identity_allows_rename_and_same_name_separate_bindings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WechatContactSelectionStore(Path(directory))
            path = store.bind(_binding())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(json.loads(path.read_text())["person_object_id"])
            self.assertEqual(store.bind(_binding()), path)
            self.assertEqual(store.bind(_binding(name="已改名")), path)
            renamed = json.loads(path.read_text())["selection"]
            self.assertEqual(renamed["current_display_name"], "已改名")
            self.assertEqual(renamed["display_name_history"], ["联系人甲", "已改名"])
            second = store.bind(_binding("2", name="已改名"))
            self.assertNotEqual(second, path)
            self.assertTrue(second.exists())

    def test_stable_key_or_provider_identity_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WechatContactSelectionStore(Path(directory))
            store.bind(_binding())
            with self.assertRaisesRegex(WechatDigestError, "不一致"):
                store.bind(
                    WechatContactBinding(
                        _binding().conversation_key,
                        "wxid_different",
                        "联系人甲",
                        False,
                    )
                )
            with self.assertRaisesRegex(WechatDigestError, "不一致"):
                store.bind(
                    WechatContactBinding(
                        _binding("2").conversation_key,
                        _binding().provider_conversation_id,
                        "另一联系人",
                        False,
                    )
                )

    def test_overlap_filter_preserves_range_and_removes_only_seen_message(self) -> None:
        messages = (_message(1), _message(2))

        class Provider:
            provider_version = "synthetic/1"
            last_capture_metrics = {
                "materialized_cursor_rows": 2,
                "cursor_discovery_ms": 1,
            }

            def capture(self, after_cursor, **_kwargs):
                return WechatCapture(
                    self.provider_version,
                    after_cursor,
                    messages[-1].cursor,
                    messages,
                )

        filtered = OverlapFilteringWechatCaptureProvider(
            Provider(),
            lambda: LegacyMessageOverlap(
                frozenset({messages[0].message_key}), frozenset()
            ),
        ).capture(WechatCursor(0, "", ""))
        self.assertEqual(filtered.upper_bound, messages[-1].cursor)
        self.assertEqual(filtered.messages, (messages[1],))

    def test_legacy_overlap_filters_terminal_and_blocks_nonterminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runs = workspace / "02_processing" / "wechat_digest" / "runs"
            for index, state in enumerate(("processed", "planned"), start=1):
                run = runs / ("run_" + str(index) * 32)
                run.mkdir(parents=True)
                conversation_key = _binding().conversation_key
                (run / "plan.json").write_text(
                    json.dumps(
                        {
                            "conversations": [
                                {
                                    "conversation_key": conversation_key,
                                    "message_keys": [f"message-{index}"],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (run / "status.json").write_text(
                    json.dumps(
                        {
                            "items": {
                                f"conversation:{conversation_key}": {"state": state}
                            }
                        }
                    ),
                    encoding="utf-8",
                )
            self.assertEqual(
                committed_legacy_message_keys(workspace),
                frozenset({"message-1"}),
            )
            authority = legacy_message_overlap(workspace)
            self.assertEqual(
                authority.nonterminal_message_keys, frozenset({"message-2"})
            )
            messages = (_message(1), _message(2))

            class Provider:
                provider_version = "synthetic/1"

                def capture(self, after_cursor, **_kwargs):
                    return WechatCapture(
                        self.provider_version,
                        after_cursor,
                        messages[-1].cursor,
                        messages,
                    )

            keyed = LegacyMessageOverlap(
                frozenset({messages[0].message_key}),
                frozenset({messages[1].message_key}),
            )
            with self.assertRaisesRegex(WechatDigestError, "未完成.*重叠"):
                OverlapFilteringWechatCaptureProvider(
                    Provider(), lambda: keyed
                ).capture(WechatCursor(0, "", ""))

    def test_nonterminal_effect_phases_all_fail_before_contact_writes(self) -> None:
        for phase in ("source_saved", "information_committed", "world_applied"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                run = (
                    workspace
                    / "02_processing"
                    / "wechat_digest"
                    / "runs"
                    / ("run_" + "1" * 32)
                )
                run.mkdir(parents=True)
                key = _binding().conversation_key
                (run / "plan.json").write_text(
                    json.dumps(
                        {
                            "conversations": [
                                {
                                    "conversation_key": key,
                                    "message_keys": ["overlap-message"],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                (run / "status.json").write_text(
                    json.dumps(
                        {
                            "items": {
                                f"conversation:{key}": {
                                    "state": "represented",
                                    "durable_effect_phase": phase,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                authority = legacy_message_overlap(workspace)
                self.assertEqual(
                    authority.nonterminal_message_keys,
                    frozenset({"overlap-message"}),
                )


class ContactAcceptancePackTests(unittest.TestCase):
    def _revision(
        self,
        index: int,
        *,
        concern: str,
        statement: str,
        semantic_type: str = "action",
    ):
        source_id = "src_" + f"{index:032x}"
        candidate_id = "candidate_" + f"{index:032x}"
        atomic_id = (
            "atomic_info_"
            + hashlib.sha256(f"{source_id}\0{candidate_id}".encode()).hexdigest()[:32]
        )
        return AtomicInformationRevision(
            atomic_information_id=atomic_id,
            revision_number=1,
            revision_id=f"{atomic_id}-r0001",
            origin_source_id=source_id,
            origin_candidate_id=candidate_id,
            origin_fingerprint=f"{index:064x}",
            statement=statement,
            semantic_type=semantic_type,
            raw_concerns=(concern,),
            related_object_ids=(),
            source_evidence=(
                EvidenceRecord(
                    source_id=source_id,
                    artifact="conversation.json",
                    segment=index,
                    speaker="发送人",
                    start="2026-08-26T10:00:00+08:00",
                    end=None,
                    excerpt=statement,
                ),
            ),
            context="同一联系人会话",
            confidence=0.9,
            created_at="2026-08-26T10:00:00+08:00",
            revision_reason="initial_ingestion",
        )

    class SynthesisProvider:
        name = "synthetic-contact-synthesis"
        provider_version = "synthetic/1"
        model = "synthetic"
        reasoning_effort = "medium"

        def __init__(self) -> None:
            self.provider_calls = 0

        def synthesize(self, request, _schema):
            self.provider_calls += 1
            evidence = {
                item["atomic_information_id"]: item
                for item in request["new_atomic_information"]
            }
            previous = request["previous_synthesis"]
            for event in previous["events"]:
                for atomic_id in event["evidence_atomic_information_ids"]:
                    evidence.setdefault(
                        atomic_id,
                        {
                            "atomic_information_id": atomic_id,
                            "statement": event["what_happened"],
                        },
                    )
            quote_ids = [
                atomic_id
                for atomic_id, item in evidence.items()
                if "报价" in item["statement"] or "询价" in item["statement"]
            ]
            visit_ids = [
                atomic_id
                for atomic_id, item in evidence.items()
                if "拜访" in item["statement"]
            ]
            events = []
            if quote_ids:
                events.append(
                    {
                        "event_id": "event_quote",
                        "business_subject": "项目甲报价",
                        "time_start": "2026-08-26T10:00:00+08:00",
                        "time_end": "2026-08-26T10:00:00+08:00",
                        "participants": ["发送人"],
                        "location_or_channel": "微信",
                        "what_happened": "完成询价、答复与报价确认",
                        "status": "已确认",
                        "status_changes": ["从询价变为已确认"],
                        "evidence_atomic_information_ids": quote_ids,
                        "conflicts": [],
                        "unknowns": [],
                    }
                )
            if visit_ids:
                events.append(
                    {
                        "event_id": "event_visit",
                        "business_subject": "项目乙拜访",
                        "time_start": "2026-08-26T10:00:00+08:00",
                        "time_end": "2026-08-26T10:00:00+08:00",
                        "participants": ["发送人"],
                        "location_or_channel": "微信",
                        "what_happened": "安排项目乙拜访",
                        "status": "待执行",
                        "status_changes": ["已安排"],
                        "evidence_atomic_information_ids": visit_ids,
                        "conflicts": [],
                        "unknowns": [],
                    }
                )
            ids = list(request["source_atomic_information_ids"])
            return {
                "schema_version": "wechat-contact-event-synthesis/1.0",
                "request_fingerprint": request["request_fingerprint"],
                "source_atomic_information_ids": ids,
                "accounted_atomic_information_ids": ids,
                "object_candidates": [],
                "events": events,
                "current_state": {
                    "completed": ["报价已确认"] if quote_ids else [],
                    "in_progress": ["拜访待执行"] if visit_ids else [],
                    "todos": [],
                    "commitments": [],
                    "blockers": [],
                },
                "conflicts": [],
                "unknowns": [],
            }

    def test_pack_projects_durable_synthesis_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlAtomicInformationStore(
                workspace / "03_information" / "atomic_information.jsonl"
            )
            revisions = (
                self._revision(1, concern="项目甲", statement="已询价"),
                self._revision(2, concern="项目甲", statement="已确认报价"),
                self._revision(3, concern="项目乙", statement="已安排拜访"),
            )
            store.ingest_batch(revisions)
            semantic_audit = (
                workspace
                / "02_processing"
                / "semantic_handoff_runs"
                / ("run_" + "1" * 32)
                / "processing-run-audit.json"
            )
            semantic_audit.parent.mkdir(parents=True)
            semantic_audit.write_text(
                json.dumps(
                    {
                        "schema_version": "processing-run-audit/1.0",
                        "provider_route": "codex-cli",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(semantic_audit, 0o600)
            runs_root = workspace / "runs"
            (runs_root / ("run_" + "1" * 32)).mkdir(parents=True)

            class RunStore:
                def __init__(self):
                    self.runs_root = runs_root
                    self.root = workspace / "contact-run-store"

                def plan(self, _run_id):
                    return {
                        "after_cursor": {
                            "timestamp": 0,
                            "conversation_key": "",
                            "message_key": "",
                        },
                        "attachments": [],
                        "conversations": [{"conversation_key": "test"}],
                    }

                def status(self, _run_id):
                    return {
                        "state": "completed",
                        "items": {
                            "conversation:test": {
                                "atomic_information_ids": [
                                    item.atomic_information_id for item in revisions
                                ],
                                "governance_metrics": {"turn_count": 1},
                            }
                        },
                    }

            provider = self.SynthesisProvider()
            json_path, markdown_path = build_contact_acceptance_pack(
                workspace=workspace,
                run_store=RunStore(),  # type: ignore[arg-type]
                binding=_binding(),
                output_root=workspace / "acceptance",
                synthesis_provider_factory=lambda: provider,
            )
            pack = json.loads(json_path.read_text())
            self.assertEqual(len(pack["events"]), 2)
            project_a = next(
                item
                for item in pack["events"]
                if item["business_subject"] == "项目甲报价"
            )
            self.assertEqual(len(project_a["evidence_atomic_information_ids"]), 2)
            self.assertEqual(provider.provider_calls, 1)
            self.assertEqual(
                pack["provider_metrics"]["semantic_provider_calls"], 1
            )
            self.assertEqual(
                pack["provider_metrics"]["governance_provider_calls"], 1
            )
            self.assertEqual(
                pack["provider_metrics"]["contact_synthesis_provider_calls"],
                1,
            )
            self.assertEqual(pack["provider_metrics"]["total_provider_calls"], 3)
            self.assertEqual(os.stat(json_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(markdown_path).st_mode & 0o777, 0o600)

    def test_ordered_continuation_merges_cross_segment_event_and_separates_same_day(
        self,
    ) -> None:
        revisions = (
            self._revision(
                1,
                concern="项目甲",
                statement="询问项目甲报价",
                semantic_type="question",
            ),
            self._revision(2, concern="项目甲", statement="答复项目甲报价"),
            self._revision(3, concern="项目乙", statement="安排项目乙拜访"),
            self._revision(4, concern="项目甲", statement="确认项目甲报价"),
        )
        with tempfile.TemporaryDirectory() as directory:
            provider = self.SynthesisProvider()
            store = ContactSynthesisStore(Path(directory), segment_size=2)
            first = store.synthesize(revisions, binding=_binding(), provider=provider)
            self.assertEqual(first.provider_calls, 2)
            self.assertEqual(len(first.result["events"]), 2)
            quote = next(
                item
                for item in first.result["events"]
                if item["event_id"] == "event_quote"
            )
            self.assertEqual(len(quote["evidence_atomic_information_ids"]), 3)
            replay_provider = self.SynthesisProvider()
            replay = store.synthesize(
                revisions, binding=_binding(name="已改名"), provider=replay_provider
            )
            self.assertEqual(replay.provider_calls, 0)
            self.assertEqual(replay.result, first.result)

    def test_result_first_interruption_resumes_with_zero_provider_calls(self) -> None:
        revisions = (
            self._revision(1, concern="项目甲", statement="询问项目甲报价"),
            self._revision(2, concern="项目甲", statement="确认项目甲报价"),
        )
        with tempfile.TemporaryDirectory() as directory:
            interrupted_provider = self.SynthesisProvider()

            def interrupt() -> None:
                raise RuntimeError("synthetic interruption")

            interrupted = ContactSynthesisStore(
                Path(directory), segment_size=2, after_result_write=interrupt
            )
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                interrupted.synthesize(
                    revisions, binding=_binding(), provider=interrupted_provider
                )
            self.assertEqual(interrupted_provider.provider_calls, 1)
            recovery_provider = self.SynthesisProvider()
            recovered = ContactSynthesisStore(
                Path(directory), segment_size=2
            ).synthesize(
                revisions, binding=_binding(name="新名称"), provider=recovery_provider
            )
            self.assertEqual(recovered.provider_calls, 0)
            self.assertEqual(recovered.resumed_segments, 1)
            self.assertEqual(len(recovered.result["events"]), 1)

    def test_reserved_attempt_can_start_once_and_reports_unified_budget(self) -> None:
        revisions = (self._revision(1, concern="项目甲", statement="已询价"),)
        with tempfile.TemporaryDirectory() as directory:
            calls = 0

            def interrupt() -> None:
                nonlocal calls
                calls += 1
                raise RuntimeError("reserved interruption")

            with self.assertRaisesRegex(RuntimeError, "reserved interruption"):
                ContactSynthesisStore(
                    Path(directory), after_reservation_write=interrupt
                ).synthesize(
                    revisions,
                    binding=_binding(),
                    provider=self.SynthesisProvider(),
                    authority_ref="issue-202-test",
                    absolute_cap=5,
                    semantic_provider_calls=1,
                    governance_provider_calls=1,
                )
            self.assertEqual(calls, 1)
            provider = self.SynthesisProvider()
            outcome = ContactSynthesisStore(Path(directory)).synthesize(
                revisions,
                binding=_binding(name="新名称"),
                provider=provider,
                authority_ref="issue-202-test",
                absolute_cap=5,
                semantic_provider_calls=1,
                governance_provider_calls=1,
            )
            self.assertEqual(provider.provider_calls, 1)
            self.assertEqual(outcome.provider_metrics["total_provider_calls"], 3)
            self.assertEqual(outcome.provider_metrics["remaining_provider_calls"], 2)

    def test_started_without_result_fails_closed_with_zero_retry(self) -> None:
        revisions = (self._revision(1, concern="项目甲", statement="已询价"),)
        with tempfile.TemporaryDirectory() as directory:
            provider = self.SynthesisProvider()

            def interrupt() -> None:
                raise RuntimeError("started interruption")

            with self.assertRaisesRegex(RuntimeError, "started interruption"):
                ContactSynthesisStore(
                    Path(directory), after_started_write=interrupt
                ).synthesize(
                    revisions,
                    binding=_binding(),
                    provider=provider,
                )
            self.assertEqual(provider.provider_calls, 0)
            retry = self.SynthesisProvider()
            with self.assertRaisesRegex(WechatDigestError, "结果未知"):
                ContactSynthesisStore(Path(directory)).synthesize(
                    revisions, binding=_binding(), provider=retry
                )
            self.assertEqual(retry.provider_calls, 0)
            self.assertFalse((Path(directory) / "cursor.json").exists())

    def test_post_start_pre_result_failure_is_unknown_and_not_retried(self) -> None:
        revisions = (self._revision(1, concern="项目甲", statement="已询价"),)

        class FailingProvider(self.SynthesisProvider):
            def synthesize(self, request, schema):
                self.provider_calls += 1
                raise RuntimeError("provider returned no durable result")

        with tempfile.TemporaryDirectory() as directory:
            provider = FailingProvider()
            with self.assertRaisesRegex(RuntimeError, "no durable result"):
                ContactSynthesisStore(Path(directory)).synthesize(
                    revisions, binding=_binding(), provider=provider
                )
            self.assertEqual(provider.provider_calls, 1)
            retry = self.SynthesisProvider()
            with self.assertRaisesRegex(WechatDigestError, "结果未知"):
                ContactSynthesisStore(Path(directory)).synthesize(
                    revisions, binding=_binding(), provider=retry
                )
            self.assertEqual(retry.provider_calls, 0)

    def test_receipt_before_cursor_resumes_with_zero_provider_calls(self) -> None:
        revisions = (self._revision(1, concern="项目甲", statement="已询价"),)
        with tempfile.TemporaryDirectory() as directory:
            def interrupt() -> None:
                raise RuntimeError("receipt interruption")

            provider = self.SynthesisProvider()
            with self.assertRaisesRegex(RuntimeError, "receipt interruption"):
                ContactSynthesisStore(
                    Path(directory), after_receipt_write=interrupt
                ).synthesize(
                    revisions, binding=_binding(), provider=provider
                )
            self.assertEqual(provider.provider_calls, 1)
            retry = self.SynthesisProvider()
            outcome = ContactSynthesisStore(Path(directory)).synthesize(
                revisions, binding=_binding(), provider=retry
            )
            self.assertEqual(retry.provider_calls, 0)
            self.assertEqual(outcome.resumed_segments, 1)
            self.assertTrue((Path(directory) / "cursor.json").exists())

    def test_absolute_cap_exhaustion_preserves_result_and_cursor(self) -> None:
        revisions = (self._revision(1, concern="项目甲", statement="已询价"),)
        with tempfile.TemporaryDirectory() as directory:
            provider = self.SynthesisProvider()
            with self.assertRaisesRegex(WechatDigestError, "达到授权上限"):
                ContactSynthesisStore(Path(directory)).synthesize(
                    revisions,
                    binding=_binding(),
                    provider=provider,
                    authority_ref="issue-202-cap-test",
                    absolute_cap=2,
                    semantic_provider_calls=1,
                    governance_provider_calls=1,
                )
            self.assertEqual(provider.provider_calls, 0)
            self.assertFalse((Path(directory) / "cursor.json").exists())
            self.assertFalse(
                (
                    Path(directory)
                    / "segments"
                    / "segment_0001"
                    / "result.json"
                ).exists()
            )


class ContactResolutionTests(unittest.TestCase):
    def test_exact_name_or_technical_key_must_resolve_uniquely(self) -> None:
        from archeos.wechat_digest import WechatCliCaptureProvider

        provider = object.__new__(WechatCliCaptureProvider)
        provider.discover_contacts = lambda: (  # type: ignore[method-assign]
            _binding("1", "同名"),
            _binding("2", "同名"),
            _binding("3", "唯一"),
        )
        self.assertEqual(provider.resolve_contact("唯一"), _binding("3", "唯一"))
        self.assertEqual(
            provider.resolve_contact(_binding("1", "同名").conversation_key),
            _binding("1", "同名"),
        )
        with self.assertRaisesRegex(WechatDigestError, "多个匹配"):
            provider.resolve_contact("同名")
        with self.assertRaisesRegex(WechatDigestError, "没有找到"):
            provider.resolve_contact("不存在")


if __name__ == "__main__":
    unittest.main()
