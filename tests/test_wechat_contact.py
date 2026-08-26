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
    OverlapFilteringWechatCaptureProvider,
    WechatContactSelectionStore,
    build_contact_acceptance_pack,
    committed_legacy_message_keys,
)
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
    def test_selection_receipt_is_private_and_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WechatContactSelectionStore(Path(directory))
            path = store.bind(_binding())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(json.loads(path.read_text())["person_object_id"])
            self.assertEqual(store.bind(_binding()), path)
            with self.assertRaisesRegex(WechatDigestError, "不一致"):
                store.bind(_binding(name="已改名"))
            with self.assertRaisesRegex(WechatDigestError, "不一致"):
                store.bind(_binding("2", name="联系人甲"))

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
            Provider(), lambda: frozenset({messages[0].message_key})
        ).capture(WechatCursor(0, "", ""))
        self.assertEqual(filtered.upper_bound, messages[-1].cursor)
        self.assertEqual(filtered.messages, (messages[1],))

    def test_legacy_overlap_uses_only_terminal_plan_metadata(self) -> None:
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


class ContactAcceptancePackTests(unittest.TestCase):
    def _revision(self, index: int, *, concern: str, statement: str):
        source_id = "src_" + f"{index:032x}"
        candidate_id = "candidate_" + f"{index:032x}"
        atomic_id = "atomic_info_" + hashlib.sha256(
            f"{source_id}\0{candidate_id}".encode()
        ).hexdigest()[:32]
        return AtomicInformationRevision(
            atomic_information_id=atomic_id,
            revision_number=1,
            revision_id=f"{atomic_id}-r0001",
            origin_source_id=source_id,
            origin_candidate_id=candidate_id,
            origin_fingerprint=f"{index:064x}",
            statement=statement,
            semantic_type="action",
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

    def test_pack_consolidates_same_business_subject_and_is_private(self) -> None:
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
            runs_root = workspace / "runs"
            (runs_root / ("run_" + "1" * 32)).mkdir(parents=True)

            class RunStore:
                def __init__(self):
                    self.runs_root = runs_root

                def plan(self, _run_id):
                    return {"attachments": []}

                def status(self, _run_id):
                    return {
                        "items": {
                            "conversation:test": {
                                "atomic_information_ids": [
                                    item.atomic_information_id for item in revisions
                                ]
                            }
                        }
                    }

            json_path, markdown_path = build_contact_acceptance_pack(
                workspace=workspace,
                run_store=RunStore(),  # type: ignore[arg-type]
                binding=_binding(),
                output_root=workspace / "acceptance",
            )
            pack = json.loads(json_path.read_text())
            self.assertEqual(len(pack["events"]), 2)
            project_a = next(
                item for item in pack["events"] if item["business_subject"] == "项目甲"
            )
            self.assertEqual(len(project_a["evidence"]), 2)
            self.assertEqual(os.stat(json_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(markdown_path).st_mode & 0o777, 0o600)


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
