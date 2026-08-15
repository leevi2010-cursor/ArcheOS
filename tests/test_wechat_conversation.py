from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
from archeos.representation_information import (
    RepresentationAnalysisBatch,
    RepresentationAnalysisResult,
    RepresentationCandidateDraft,
    RepresentationInformationError,
    RepresentationInformationService,
    RepresentationResidueDraft,
    _analysis_batches,
    _representation_analysis_prompt,
    _units_from_representation,
)
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


def synthetic_multi_batch_export() -> dict[str, object]:
    payload = synthetic_export()
    payload["count"] = 50
    payload["messages"] = [
        (
            f"[2026-08-15 09:{index:02d}] "
            f"Sender_{index % 2}: Synthetic message {index + 1}."
        )
        for index in range(50)
    ]
    return payload


def build_synthetic_representation(
    root: Path,
    payload: dict[str, object],
    *,
    source_id: str = SOURCE_ID,
):
    external = root / "private-export.json"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    sources = LocalManagedSourceRepository(
        root / "01_inbox",
        id_factory=lambda: source_id,
        clock=lambda: TIMESTAMP,
    )
    source = sources.admit(external, metadata={"media_type": "application/json"}).source
    representations = LocalRepresentationRepository(
        root / "02_processing" / "representations", clock=lambda: TIMESTAMP
    )
    representation = (
        RepresentationService(sources, representations, clock=lambda: TIMESTAMP)
        .build(
            source.source_id,
            WechatConversationRepresentationAdapter(),
            {},
        )
        .representation
    )
    return sources, representations, representation


class NeverCalledProvider:
    name = "must-not-run"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, batch):
        self.calls += 1
        raise AssertionError("WeChat service gate must run before the provider")


class CrossBatchContextProvider:
    name = "synthetic-cross-batch"

    def __init__(self) -> None:
        self.batches: list[RepresentationAnalysisBatch] = []

    def analyze(self, batch: RepresentationAnalysisBatch):
        self.batches.append(batch)
        candidates = []
        residue = []
        supports = {
            unit.locator["sequence"]: unit for unit in batch.context_support_units
        }
        for anchor in batch.anchor_units:
            if anchor.locator["sequence"] == 20:
                support = supports[21]
                candidates.append(
                    RepresentationCandidateDraft(
                        statement="Synthetic cross-boundary statement.",
                        semantic_type="observation",
                        concerns=("Synthetic",),
                        evidence_unit_ids=(anchor.unit_id, support.unit_id),
                        context="Synthetic adjacent support is required.",
                        confidence=1.0,
                    )
                )
            else:
                residue.append(
                    RepresentationResidueDraft(
                        evidence_unit_ids=(anchor.unit_id,),
                        reason_not_absorbed="Synthetic anchor retained without a claim.",
                        future_value_or_uncertainty="Synthetic test coverage.",
                    )
                )
        return RepresentationAnalysisResult(tuple(candidates), tuple(residue))


class UnavailableContextProvider:
    name = "synthetic-unavailable-context"

    def analyze(self, batch: RepresentationAnalysisBatch):
        candidates = []
        residue = []
        has_unavailable_support = any(
            not unit.analysis_eligible for unit in batch.context_support_units
        )
        for anchor in batch.anchor_units:
            if has_unavailable_support:
                residue.append(
                    RepresentationResidueDraft(
                        evidence_unit_ids=(anchor.unit_id,),
                        reason_not_absorbed="Required context is not evidence-capable.",
                        future_value_or_uncertainty="Dependency remains unresolved.",
                    )
                )
            else:
                candidates.append(
                    RepresentationCandidateDraft(
                        statement="Synthetic independently supported statement.",
                        semantic_type="observation",
                        concerns=("Synthetic",),
                        evidence_unit_ids=(anchor.unit_id,),
                        context="No unavailable dependency.",
                        confidence=1.0,
                    )
                )
        return RepresentationAnalysisResult(tuple(candidates), tuple(residue))


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
            set(artifact), {"schema_version", "provider", "source", "conversation"}
        )
        rendered = json.dumps(artifact, ensure_ascii=False)
        for forbidden_identity in (
            "analysis_units",
            "analysis_batches",
            "conversation_unit_",
            '"batch_id"',
        ):
            self.assertNotIn(forbidden_identity, rendered)
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
        units = _units_from_representation(result.representation, self.representations)
        self.assertEqual(
            units[6].structured_value["processing_classification"],
            "attachment_reference",
        )
        self.assertFalse(units[6].analysis_eligible)
        self.assertEqual(units[6].exclusion_reason, "MESSAGE_METADATA_UNAVAILABLE")

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
        self.assertTrue(all(unit.unit_id.startswith("unit_") for unit in first_units))
        self.assertTrue(
            all(
                not unit.unit_id.startswith("conversation_unit_")
                for unit in first_units
            )
        )
        anchor = first_units[0]
        self.assertEqual(anchor.context_support_unit_ids, (first_units[1].unit_id,))
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
                "participant_object_bindings": 0,
                "analysis_eligible": 2,
                "context_support_references": 3,
                "excluded_or_unsupported": 5,
                "unresolved_reference_count": 1,
                "missing_external_message_id_count": 7,
                "missing_reply_metadata_count": 7,
                "missing_attachment_metadata_count": 7,
            },
        )
        self.assertFalse((self.root / "03_information").exists())
        self.assertFalse((self.root / "04_core").exists())

    def test_independent_rebuild_has_identical_artifact_and_canonical_units(
        self,
    ) -> None:
        first_sources, first_repository, first = build_synthetic_representation(
            self.root / "independent-a", synthetic_export()
        )
        second_sources, second_repository, second = build_synthetic_representation(
            self.root / "independent-b", synthetic_export()
        )
        first_artifact = first_repository.read_artifact(
            first.representation_id, first.artifacts[0].artifact_id
        )
        second_artifact = second_repository.read_artifact(
            second.representation_id, second.artifacts[0].artifact_id
        )
        first_units = _units_from_representation(first, first_repository)
        second_units = _units_from_representation(second, second_repository)

        self.assertTrue(first_sources.verify(first.source_id).verified)
        self.assertTrue(second_sources.verify(second.source_id).verified)
        self.assertEqual(first_artifact, second_artifact)
        self.assertEqual(
            [unit.unit_id for unit in first_units],
            [unit.unit_id for unit in second_units],
        )
        self.assertEqual(
            [unit.context_support_unit_ids for unit in first_units],
            [unit.context_support_unit_ids for unit in second_units],
        )

    def test_generic_multi_batch_context_is_explicit_and_anchor_accounted(self) -> None:
        sources, representations, representation = build_synthetic_representation(
            self.root / "multi-batch", synthetic_multi_batch_export()
        )
        units = _units_from_representation(representation, representations)
        provider = CrossBatchContextProvider()
        service = RepresentationInformationService(
            sources,
            representations,
            self.root / "must-not-publish",
            batch_size=20,
            clock=lambda: TIMESTAMP,
        )
        candidates, residue, batches = service._analyze(units, provider)

        self.assertEqual(len(units), 50)
        self.assertEqual(
            [len(batch.anchor_units) for batch in provider.batches], [20, 20, 10]
        )
        self.assertEqual([len(batch["unit_ids"]) for batch in batches], [20, 20, 10])
        first_batch = provider.batches[0]
        support_21 = next(
            unit
            for unit in first_batch.context_support_units
            if unit.locator["sequence"] == 21
        )
        self.assertEqual(support_21.unit_id, units[20].unit_id)
        self.assertEqual(support_21.locator, {"source_id": SOURCE_ID, "sequence": 21})
        provider_input = _representation_analysis_prompt(first_batch)
        self.assertIn('"anchor_units"', provider_input)
        self.assertIn('"context_support_units"', provider_input)
        self.assertIn(support_21.unit_id, provider_input)
        self.assertIn('"sequence": 21', provider_input)
        cross_boundary = candidates[0]["source_evidence"]
        self.assertEqual(
            [evidence["unit_id"] for evidence in cross_boundary],
            [units[19].unit_id, units[20].unit_id],
        )
        self.assertEqual(
            json.loads(cross_boundary[1]["locator"]),
            {"source_id": SOURCE_ID, "sequence": 21},
        )
        anchor_19_residue = next(
            item
            for item in residue
            if item["source_evidence"][0]["unit_id"] == units[18].unit_id
        )
        self.assertEqual(len(anchor_19_residue["source_evidence"]), 1)
        self.assertFalse((self.root / "must-not-publish").exists())

    def test_unavailable_context_dependency_remains_residue(self) -> None:
        units = _units_from_representation(
            self.build().representation, self.representations
        )
        batches = _analysis_batches(units, 1)
        anchor_two = next(
            batch for batch in batches if batch[0].locator["sequence"] == 2
        )
        self.assertTrue(
            any(not unit.analysis_eligible for unit in anchor_two.context_support_units)
        )
        service = RepresentationInformationService(
            self.sources,
            self.representations,
            self.root / "must-not-publish",
            batch_size=1,
            clock=lambda: TIMESTAMP,
        )
        candidates, residue, _batches = service._analyze(
            units, UnavailableContextProvider()
        )
        self.assertFalse(
            any(
                item["source_evidence"][0]["unit_id"] == units[1].unit_id
                for item in candidates
            )
        )
        unresolved = next(
            item
            for item in residue
            if item["source_evidence"][0]["unit_id"] == units[1].unit_id
        )
        self.assertEqual(len(unresolved["source_evidence"]), 1)
        self.assertIn("unresolved", unresolved["future_value_or_uncertainty"])
        self.assertFalse((self.root / "must-not-publish").exists())

    def test_information_extract_fails_before_provider_or_durable_write(self) -> None:
        representation = self.build().representation
        provider = NeverCalledProvider()
        output_root = self.root / "representation-information"
        service = RepresentationInformationService(
            self.sources, self.representations, output_root
        )
        with self.assertRaisesRegex(
            RepresentationInformationError,
            "Conversation 目前只允许生成 Representation；真实语义吸收尚未开放",
        ):
            service.extract(representation.representation_id, provider)
        self.assertEqual(provider.calls, 0)
        self.assertFalse(output_root.exists())
        self.assertFalse((self.root / "03_information").exists())
        self.assertFalse((self.root / "04_core").exists())

        analysis_file = self.root / "analysis.json"
        analysis_file.write_text(
            json.dumps({"candidates": [], "residue": []}), encoding="utf-8"
        )
        store = self.root / "03_information" / "atomic_information.jsonl"
        cli_output = io.StringIO()
        with (
            redirect_stdout(cli_output),
            patch("archeos.cli.ingest_processing_package") as ingest,
            patch("archeos.cli.JsonlAtomicInformationStore") as store_builder,
            patch("archeos.cli.FileRepresentationAnalysisProvider.analyze") as analyze,
            patch("archeos.cli.SQLiteWorldModelRepository") as world_repository,
        ):
            return_code = main(
                [
                    "information",
                    "--store",
                    str(store),
                    "extract",
                    representation.representation_id,
                    "--managed-root",
                    str(self.managed_root),
                    "--representation-root",
                    str(self.representation_root),
                    "--output-root",
                    str(output_root),
                    "--analysis-file",
                    str(analysis_file),
                ]
            )
        ingest.assert_not_called()
        store_builder.assert_not_called()
        analyze.assert_not_called()
        world_repository.assert_not_called()
        self.assertEqual(return_code, 1)
        self.assertIn("真实语义吸收尚未开放", cli_output.getvalue())
        self.assertFalse(output_root.exists())
        self.assertFalse(store.exists())
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
