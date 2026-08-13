from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archeos.atomic_information import JsonlAtomicInformationStore, ingest_processing_package
from archeos.representation import (
    AdapterArtifact,
    AdapterBuildResult,
    LocalRepresentationRepository,
    RepresentationService,
)
from archeos.representation.adapters import MarkdownRepresentationAdapter
from archeos.representation_information import (
    RepresentationAnalysisResult,
    RepresentationCandidateDraft,
    RepresentationInformationError,
    RepresentationInformationService,
    RepresentationResidueDraft,
)
from archeos.source import LocalManagedSourceRepository


TIMESTAMP = "2026-08-13T00:00:00.000Z"


class JsonAdapter:
    name = "synthetic"
    version = "1.0"
    supported_media_types = ("application/synthetic",)

    def __init__(self, kind: str, payload: object) -> None:
        self.kind = kind
        self.payload = payload

    def build(self, source, materialized_path, staging_dir, configuration):
        path = staging_dir / "artifacts" / "synthetic.json"
        path.write_text(json.dumps(self.payload), encoding="utf-8")
        return AdapterBuildResult(
            self.kind,
            (AdapterArtifact("structure", "artifacts/synthetic.json", "application/json"),),
            1.0,
        )


class CoveringProvider:
    name = "covering-fake"

    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    def analyze(self, units):
        self.batches.append(tuple(unit.unit_id for unit in units))
        return RepresentationAnalysisResult(
            candidates=tuple(
                RepresentationCandidateDraft(
                    statement=f"Synthetic {unit.kind} is retained.",
                    semantic_type="observation",
                    concerns=("Synthetic",),
                    evidence_unit_ids=(unit.unit_id,),
                    context=unit.context,
                    confidence=1.0,
                )
                for unit in units
            ),
            residue=(),
        )


class InvalidReferenceProvider:
    name = "invalid-reference"

    def analyze(self, units):
        return RepresentationAnalysisResult(
            candidates=(),
            residue=(
                RepresentationResidueDraft(
                    evidence_unit_ids=("unit_" + "0" * 64,),
                    reason_not_absorbed="Synthetic invalid reference.",
                    future_value_or_uncertainty="None.",
                ),
            ),
        )


class FailingProvider:
    name = "failing"

    def analyze(self, units):
        raise RuntimeError("synthetic provider failure")


class MutatingProvider(CoveringProvider):
    name = "mutating"

    def __init__(self, managed_bytes: Path) -> None:
        super().__init__()
        self.managed_bytes = managed_bytes

    def analyze(self, units):
        self.managed_bytes.write_text("changed", encoding="utf-8")
        return super().analyze(units)


class RepresentationInformationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.managed_root = self.root / "managed"
        self.representation_root = self.root / "representations"
        self.output_root = self.root / "information"
        self.number = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, kind: str, payload: object):
        self.number += 1
        external = self.root / f"source-{self.number}.bin"
        external.write_text("synthetic", encoding="utf-8")
        source_id = f"src_{self.number:032x}"
        sources = LocalManagedSourceRepository(
            self.managed_root,
            id_factory=lambda: source_id,
            clock=lambda: TIMESTAMP,
        )
        source = sources.admit(
            external, metadata={"media_type": "application/synthetic"}
        ).source
        representations = LocalRepresentationRepository(
            self.representation_root, clock=lambda: TIMESTAMP
        )
        representation = RepresentationService(
            sources, representations, clock=lambda: TIMESTAMP
        ).build(source.source_id, JsonAdapter(kind, payload)).representation
        return sources, representations, representation

    def extract(self, kind: str, payload: object, provider, *, batch_size: int = 100):
        sources, representations, representation = self.build(kind, payload)
        service = RepresentationInformationService(
            sources,
            representations,
            self.output_root,
            batch_size=batch_size,
            clock=lambda: TIMESTAMP,
        )
        package = service.extract(representation.representation_id, provider)
        return representation, package

    def test_format_mappings_have_replayable_units_and_image_is_excluded(self) -> None:
        fixtures = (
            ("markdown_blocks", {"blocks": [{"kind": "paragraph", "raw": "Synthetic markdown.", "source_locator": {"line_start": 1, "line_end": 1}}]}, 1),
            ("pdf_text", {"pages": [{"source_locator": {"page": 1}, "text_blocks": [{"text": "Synthetic PDF.", "source_locator": {"page": 1, "ordinal": 1}}], "tables": []}]}, 1),
            ("xlsx_structure", {"sheets": [{"cells": [{"value": "Synthetic XLSX.", "source_locator": {"sheet": "Sheet", "cell": "A1"}}], "embedded_media": []}]}, 1),
            ("pptx_structure", {"slides": [{"source_locator": {"slide_index": 1}, "speaker_notes": None, "shapes": [{"source_locator": {"slide_index": 1, "shape_id": 1}, "text": "Synthetic PPTX."}]}]}, 1),
            ("image_structural_preflight", {"format": "png", "pixel_width": 1, "pixel_height": 1}, 0),
        )
        for kind, payload, candidates in fixtures:
            with self.subTest(kind=kind):
                representation, package = self.extract(kind, payload, CoveringProvider())
                manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["representation"]["representation_id"], representation.representation_id)
                self.assertEqual(manifest["counts"]["atomic_information_candidates"], candidates)
                self.assertEqual(manifest["counts"]["unaccounted_eligible_units"], 0)
                self.assertTrue(all(item["unit_id"].startswith("unit_") for item in manifest["units"]))
                if kind == "image_structural_preflight":
                    self.assertEqual(manifest["counts"]["eligible_units"], 0)
                    self.assertEqual(
                        manifest["units"][0]["exclusion_reason"],
                        "IMAGE_STRUCTURAL_PREFLIGHT_HAS_NO_BUSINESS_SEMANTICS",
                    )

    def test_batches_cover_every_eligible_unit_and_ingestion_is_idempotent(self) -> None:
        payload = {"blocks": [{"kind": "paragraph", "raw": f"Synthetic {index}.", "source_locator": {"line_start": index, "line_end": index}} for index in range(1, 6)]}
        provider = CoveringProvider()
        _, package = self.extract("markdown_blocks", payload, provider, batch_size=2)
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([len(batch) for batch in provider.batches], [2, 2, 1])
        self.assertEqual([len(batch["unit_ids"]) for batch in manifest["batches"]], [2, 2, 1])
        store = JsonlAtomicInformationStore(self.root / "atomic.jsonl")
        first = ingest_processing_package(package, store)
        second = ingest_processing_package(package, store)
        self.assertEqual(first.created, 5)
        self.assertEqual(second.existing, 5)
        information = store.list_atomic_information()[0]
        evidence = information.source_evidence[0]
        self.assertIsNotNone(evidence.representation_id)
        self.assertTrue(evidence.unit_id.startswith("unit_"))
        self.assertEqual(evidence.excerpt, "Synthetic 1.")

    def test_invalid_reference_and_runtime_failure_publish_nothing(self) -> None:
        payload = {"blocks": [{"kind": "paragraph", "raw": "Synthetic.", "source_locator": {"line_start": 1, "line_end": 1}}]}
        for provider in (InvalidReferenceProvider(), FailingProvider()):
            with self.subTest(provider=provider.name):
                sources, representations, representation = self.build("markdown_blocks", payload)
                service = RepresentationInformationService(
                    sources, representations, self.output_root, clock=lambda: TIMESTAMP
                )
                with self.assertRaises(RepresentationInformationError):
                    service.extract(representation.representation_id, provider)
                self.assertFalse((self.output_root / representation.representation_id).exists())

    def test_strict_package_rejects_path_leak_before_store_write(self) -> None:
        payload = {"blocks": [{"kind": "paragraph", "raw": "Synthetic.", "source_locator": {"line_start": 1, "line_end": 1}}]}
        _, package = self.extract("markdown_blocks", payload, CoveringProvider())
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["path"] = "/private/synthetic"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        store_path = self.root / "untouched.jsonl"
        with self.assertRaises(RepresentationInformationError):
            ingest_processing_package(package, JsonlAtomicInformationStore(store_path))
        self.assertFalse(store_path.exists())

    def test_managed_source_change_during_analysis_does_not_publish(self) -> None:
        payload = {"blocks": [{"kind": "paragraph", "raw": "Synthetic.", "source_locator": {"line_start": 1, "line_end": 1}}]}
        sources, representations, representation = self.build("markdown_blocks", payload)
        managed_bytes = self.managed_root / "sources" / representation.source_id / "original.bin"
        service = RepresentationInformationService(
            sources, representations, self.output_root, clock=lambda: TIMESTAMP
        )
        with self.assertRaises(RepresentationInformationError):
            service.extract(representation.representation_id, MutatingProvider(managed_bytes))
        self.assertFalse((self.output_root / representation.representation_id).exists())

    def test_synthetic_markdown_representation_smoke(self) -> None:
        external = self.root / "smoke.md"
        external.write_text("# Synthetic\n\nSynthetic business content.\n", encoding="utf-8")
        source_id = "src_" + "a" * 32
        sources = LocalManagedSourceRepository(
            self.managed_root, id_factory=lambda: source_id, clock=lambda: TIMESTAMP
        )
        source = sources.admit(
            external, metadata={"media_type": "text/markdown"}
        ).source
        representations = LocalRepresentationRepository(
            self.representation_root, clock=lambda: TIMESTAMP
        )
        representation = RepresentationService(
            sources, representations, clock=lambda: TIMESTAMP
        ).build(source.source_id, MarkdownRepresentationAdapter()).representation
        package = RepresentationInformationService(
            sources, representations, self.output_root, clock=lambda: TIMESTAMP
        ).extract(representation.representation_id, CoveringProvider())
        result = ingest_processing_package(
            package, JsonlAtomicInformationStore(self.root / "smoke-information.jsonl")
        )
        self.assertGreater(result.created, 0)


if __name__ == "__main__":
    unittest.main()
