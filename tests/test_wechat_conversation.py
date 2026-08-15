from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from archeos.cli import main
from archeos.representation import (
    LocalRepresentationRepository,
    RepresentationError,
    RepresentationService,
    WechatConversationRepresentationAdapter,
    validate_wechat_conversation_artifact,
    wechat_conversation_metrics,
)
from archeos.representation.registry import production_adapter
from archeos.representation_information import _units_from_representation
from archeos.source import LocalManagedSourceRepository


SOURCE_ID = "src_" + "a" * 32
TIMESTAMP = "2026-08-15T00:00:00.000Z"


def synthetic_export() -> dict[str, object]:
    return {
        "chat": "Synthetic Chat",
        "username": "wxid_synthetic_chat",
        "is_group": False,
        "count": 7,
        "offset": 0,
        "limit": 50,
        "start_time": None,
        "end_time": None,
        "type": None,
        "messages": [
            "[2026-08-15 09:00] Sender_A: 这个项目需要复核。",
            "[2026-08-15 09:01] Sender_A: Confirmed.\n  ↳ Sender_B: Prior text.",
            "[2026-08-15 09:02] Sender_B: [图片] local_id=42",
            "[2026-08-15 09:03] Sender_A: [系统] Synthetic system event.",
            "[2026-08-15 09:04] Sender_B: [语音]",
            "[2026-08-15 09:05] Sender_A: [视频]",
            "[2026-08-15 09:06] Sender_B: [文件] synthetic.pdf",
        ],
        "failures": None,
    }


class WechatConversationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.managed_root = self.root / "01_inbox"
        self.representation_root = self.root / "02_processing" / "representations"
        self.external = self.root / "private-export.json"
        self.external.write_text(
            json.dumps(synthetic_export(), ensure_ascii=False), encoding="utf-8"
        )
        self.sources = LocalManagedSourceRepository(
            self.managed_root,
            id_factory=lambda: SOURCE_ID,
            clock=lambda: TIMESTAMP,
        )
        self.source = self.sources.admit(
            self.external, metadata={"media_type": "application/json"}
        ).source
        self.representations = LocalRepresentationRepository(
            self.representation_root, clock=lambda: TIMESTAMP
        )
        self.service = RepresentationService(
            self.sources, self.representations, clock=lambda: TIMESTAMP
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self):
        return self.service.build(
            self.source.source_id,
            WechatConversationRepresentationAdapter(),
            {},
        )

    def artifact_payload(self, result) -> dict[str, object]:
        artifact = result.representation.artifacts[0]
        return json.loads(
            self.representations.read_artifact(
                result.representation.representation_id, artifact.artifact_id
            )
        )

    def test_strict_conversation_preserves_order_and_unavailable_metadata(self) -> None:
        result = self.build()
        artifact = validate_wechat_conversation_artifact(self.artifact_payload(result))
        conversation = artifact["conversation"]
        assert isinstance(conversation, dict)
        messages = conversation["messages"]
        participants = conversation["participants"]
        assert isinstance(messages, list) and isinstance(participants, list)

        self.assertEqual(result.representation.kind, "wechat_conversation")
        self.assertEqual(
            [message["sequence"] for message in messages], [1, 2, 3, 4, 5, 6, 7]
        )
        self.assertEqual(
            [message["source_locator"] for message in messages],
            [
                {"source_id": SOURCE_ID, "sequence": sequence}
                for sequence in range(1, 8)
            ],
        )
        self.assertTrue(
            all(message["external_message_id"] == "unavailable" for message in messages)
        )
        self.assertTrue(
            all(message["reply_to"] == "unavailable" for message in messages)
        )
        self.assertTrue(all(message["attachment_refs"] == [] for message in messages))
        self.assertTrue(
            all(
                participant["object_identity"] == "unavailable"
                for participant in participants
            )
        )
        self.assertEqual(conversation["time_range"]["timezone"], "unavailable")
        self.assertEqual(
            messages[0]["unresolved_references"],
            [{"surface": "这个项目", "resolved_object_id": "unavailable"}],
        )
        self.assertEqual(messages[6]["message_type"], "file_placeholder")

    def test_analysis_units_are_stable_bounded_and_compatible_with_issue_31(
        self,
    ) -> None:
        first = self.build()
        first_bytes = self.representations.read_artifact(
            first.representation.representation_id,
            first.representation.artifacts[0].artifact_id,
        )
        first_units = _units_from_representation(
            first.representation, self.representations
        )
        second = self.build()
        second_units = _units_from_representation(
            second.representation, self.representations
        )

        self.assertEqual(first.status, "built")
        self.assertEqual(second.status, "existing")
        self.assertEqual(first.representation, second.representation)
        self.assertEqual(
            first_bytes,
            self.representations.read_artifact(
                second.representation.representation_id,
                second.representation.artifacts[0].artifact_id,
            ),
        )
        self.assertEqual(
            [unit.unit_id for unit in first_units],
            [unit.unit_id for unit in second_units],
        )
        self.assertEqual(len(first_units), 7)
        self.assertEqual(sum(unit.analysis_eligible for unit in first_units), 2)
        anchor = first_units[0]
        context = json.loads(anchor.context)
        self.assertEqual(context["anchor_role"], "evidence_capable")
        self.assertEqual(
            [item["sequence"] for item in context["context_messages"]], [2]
        )
        self.assertTrue(
            all(not item["evidence_capable"] for item in context["context_messages"])
        )
        self.assertEqual(anchor.locator["sequence"], 1)
        self.assertTrue(first_units[1].analysis_eligible)
        self.assertEqual(first_units[1].locator["sequence"], 2)
        self.assertTrue(
            all(
                reference["resolved_object_id"] == "unavailable"
                for reference in anchor.structured_value["unresolved_references"]
            )
        )

    def test_metrics_account_for_every_message_without_semantic_execution(self) -> None:
        metrics = wechat_conversation_metrics(self.artifact_payload(self.build()))
        self.assertEqual(
            metrics,
            {
                "message_total": 7,
                "replayable_messages": 7,
                "stable_locator_failures": 0,
                "participant_binding_attempts_should_be_zero": 0,
                "unaccounted_analysis_units": 0,
                "analysis_eligible": 2,
                "context_only": 3,
                "excluded_or_unsupported": 5,
                "unresolved_reference_count": 1,
                "missing_external_message_id_count": 7,
                "missing_reply_metadata_count": 7,
                "missing_attachment_metadata_count": 7,
            },
        )
        self.assertFalse((self.root / "03_information").exists())
        self.assertFalse((self.root / "04_core").exists())

    def test_cli_reports_only_safe_aggregate_state(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            return_code = main(
                [
                    "conversation",
                    "wechat",
                    "represent",
                    SOURCE_ID,
                    "--managed-root",
                    str(self.managed_root),
                    "--representation-root",
                    str(self.representation_root),
                ]
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(return_code, 0)
        self.assertFalse(payload["semantic_provider_called"])
        self.assertFalse(payload["atomic_information_written"])
        self.assertFalse(payload["world_model_written"])
        rendered = output.getvalue()
        for private_value in (
            "Synthetic Chat",
            "wxid_synthetic_chat",
            "Sender_A",
            "这个项目需要复核",
            str(self.external),
            str(self.managed_root),
        ):
            self.assertNotIn(private_value, rendered)

    def test_strict_input_rejects_drift_without_publishing(self) -> None:
        invalid_exports = []
        wrong_count = copy.deepcopy(synthetic_export())
        wrong_count["count"] = 4
        invalid_exports.append(wrong_count)
        bad_line = copy.deepcopy(synthetic_export())
        bad_line["messages"][0] = "malformed"
        invalid_exports.append(bad_line)
        unknown_field = copy.deepcopy(synthetic_export())
        unknown_field["unexpected"] = "not accepted"
        invalid_exports.append(unknown_field)
        failures = copy.deepcopy(synthetic_export())
        failures["failures"] = ["synthetic"]
        invalid_exports.append(failures)

        for index, payload in enumerate(invalid_exports, start=1):
            with self.subTest(index=index):
                root = self.root / f"invalid-{index}"
                external = root / "input.json"
                external.parent.mkdir(parents=True)
                external.write_text(json.dumps(payload), encoding="utf-8")
                source_id = f"src_{index:032x}"
                sources = LocalManagedSourceRepository(
                    root / "01_inbox", id_factory=lambda: source_id
                )
                source = sources.admit(
                    external, metadata={"media_type": "application/json"}
                ).source
                representation_root = root / "02_processing" / "representations"
                representations = LocalRepresentationRepository(representation_root)
                with self.assertRaises(RepresentationError):
                    RepresentationService(sources, representations).build(
                        source.source_id,
                        WechatConversationRepresentationAdapter(),
                        {},
                    )
                self.assertEqual(representations.list_for_source(source.source_id), ())
                self.assertEqual(list((representation_root / ".staging").iterdir()), [])

    def test_tampered_source_and_runtime_configuration_fail_closed(self) -> None:
        with self.assertRaises(RepresentationError):
            self.service.build(
                self.source.source_id,
                WechatConversationRepresentationAdapter(),
                {"previous_messages": 2},
            )
        managed_bytes = self.managed_root / self.source.managed_locator
        managed_bytes.write_bytes(b"tampered")
        with self.assertRaises(RepresentationError):
            self.build()
        self.assertEqual(
            self.representations.list_for_source(self.source.source_id), ()
        )

    def test_wechat_adapter_is_the_only_new_registered_entry(self) -> None:
        self.assertIsInstance(
            production_adapter("wechat-conversation"),
            WechatConversationRepresentationAdapter,
        )


if __name__ == "__main__":
    unittest.main()
