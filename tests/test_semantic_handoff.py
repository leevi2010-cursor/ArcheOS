from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archeos.atomic_information import JsonlAtomicInformationStore
from archeos.representation import (
    AdapterArtifact,
    AdapterBuildResult,
    LocalRepresentationRepository,
    RepresentationService,
)
from archeos.representation_information import (
    CodexCliRepresentationAnalysisProvider,
    RepresentationAnalysisBatch,
    RepresentationAnalysisUnit,
    RepresentationInformationService,
)
from archeos.semantic_handoff import ExternalAgentSemanticHandoffService
from archeos.source import LocalManagedSourceRepository


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


class FakeProcess:
    def __init__(self, command, *, mode: str, calls: list[list[str]]):
        self.command = list(command)
        self.mode = mode
        self.calls = calls
        self.pid = 99999999
        self.returncode: int | None = None
        calls.append(self.command)

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
            result_path.write_text(
                json.dumps(
                    {
                        "protocol_version": request["protocol_version"],
                        "input_fingerprint": request["input_fingerprint"],
                        "candidates": [
                            {
                                "statement": "Synthetic statement.",
                                "semantic_type": "observation",
                                "concerns": ["Synthetic"],
                                "evidence_unit_ids": [
                                    request["anchor_units"][0]["unit_id"]
                                ],
                                "context": "Synthetic context.",
                                "confidence": 0.9,
                            }
                        ],
                        "residue": [],
                    }
                ),
                encoding="utf-8",
            )
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
        self.root = Path(self.temp.name)

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
            {"type": "string", "const": "external-agent-semantic-handoff/1.0"},
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            provider.execution_records[0].strict_validation_status, "passed"
        )

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

    def build_service(self, *, blocks: int = 1):
        external = self.root / "synthetic.txt"
        external.write_text("synthetic", encoding="utf-8")
        sources = LocalManagedSourceRepository(
            self.root / "managed",
            id_factory=lambda: "src_" + "1" * 32,
            clock=lambda: "2026-08-15T00:00:00.000Z",
        )
        source = sources.admit(
            external, metadata={"media_type": "application/synthetic"}
        ).source
        representations = LocalRepresentationRepository(self.root / "representations")
        representation = (
            RepresentationService(sources, representations)
            .build(source.source_id, JsonAdapter(blocks))
            .representation
        )
        service = RepresentationInformationService(
            sources,
            representations,
            self.root / "information",
            clock=lambda: "2026-08-15T00:00:00.000Z",
        )
        return representation, service

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
        first = handoff.execute(representation.representation_id, provider)
        self.assertEqual(first.ingestion.created, 1)
        self.assertFalse(first.replayed_existing_package)
        audit = json.loads(first.audit_paths[0].read_text())
        self.assertEqual(audit["execution_status"], "succeeded")
        self.assertTrue(audit["package_published"])
        self.assertTrue(audit["information_ingested"])
        self.assertEqual(audit["durable_ingestion_status"], "completed")
        self.assertEqual(audit["unaccounted_units"], 0)
        self.assertEqual(audit["audit_readback_status"], "verified")
        self.assertNotIn("Synthetic business input.", first.audit_paths[0].read_text())
        replay_provider = CodexCliRepresentationAnalysisProvider(
            provider_version="0.147.0", runner=FakeRunner()
        )
        second = handoff.execute(representation.representation_id, replay_provider)
        self.assertTrue(second.replayed_existing_package)
        self.assertEqual(second.ingestion.existing, 1)
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
            handoff.execute(representation.representation_id, provider)
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())
        audits = list((self.root / "audits").glob("*/processing-run-audit.json"))
        self.assertEqual(len(audits), 1)
        self.assertEqual(
            json.loads(audits[0].read_text())["execution_status"], "failed"
        )

    def test_replay_rechecks_managed_source_before_store_write(self) -> None:
        representation, service = self.build_service()
        store_path = self.root / "atomic.jsonl"
        handoff = ExternalAgentSemanticHandoffService(
            service, JsonlAtomicInformationStore(store_path), self.root / "audits"
        )
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=FakeRunner()
            ),
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
            handoff.execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
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
            handoff.execute(representation.representation_id, provider)
        audits = sorted((self.root / "audits").glob("*/processing-run-audit.json"))
        self.assertEqual(len(audits), 2)
        payloads = [json.loads(path.read_text()) for path in audits]
        successful = next(
            item for item in payloads if item["execution_status"] == "succeeded"
        )
        failed = next(item for item in payloads if item["execution_status"] == "failed")
        self.assertIsNone(successful["failure_category"])
        self.assertEqual(failed["failure_category"], "runtime_nonzero_exit")
        self.assertEqual(successful["handoff_status"], "failed")
        self.assertFalse(
            (self.root / "information" / representation.representation_id).exists()
        )
        self.assertFalse((self.root / "atomic.jsonl").exists())

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
            provider_version="0.147.0", runner=FakeRunner()
        )
        handoff = ExternalAgentSemanticHandoffService(
            service, InspectingStore(self.root / "atomic.jsonl"), audit_root
        )
        handoff.execute(representation.representation_id, provider)

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
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=SequenceRunner("valid", "valid")
            ),
        )
        next(audit_root.glob("*/processing-run-audit.json")).unlink()
        replay_store = self.root / "replay-atomic.jsonl"
        with self.assertRaisesRegex(Exception, "未能安全重放"):
            ExternalAgentSemanticHandoffService(
                service, JsonlAtomicInformationStore(replay_store), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
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
        handoff.execute(
            representation.representation_id,
            CodexCliRepresentationAnalysisProvider(
                provider_version="0.147.0", runner=SequenceRunner("valid", "valid")
            ),
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
                    provider_version="0.147.0", runner=FakeRunner()
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

        with self.assertRaisesRegex(Exception, "已写入或正在读回"):
            ExternalAgentSemanticHandoffService(
                service, ReadbackFailingStore(store_path), audit_root
            ).execute(
                representation.representation_id,
                CodexCliRepresentationAnalysisProvider(
                    provider_version="0.147.0", runner=FakeRunner()
                ),
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
                provider_version="0.147.0", runner=FakeRunner()
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
        self.assertNotIn("fallback", source.lower())


if __name__ == "__main__":
    unittest.main()
