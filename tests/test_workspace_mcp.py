from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import archeos.workspace as workspace_module
from archeos.atomic_information import (
    AtomicInformationRevision,
    EvidenceRecord,
    JsonlAtomicInformationStore,
)
from archeos.cli import _resolve_durable_paths, build_parser, main
from archeos.digestion import JsonlChangeProposalStore
from archeos.digestion.models import (
    ChangeProposal,
    HumanReviewContent,
    WorldModelOperation,
)
from archeos.mcp_server import CanonicalReadService, create_server
from archeos.representation.models import AdapterArtifact, AdapterBuildResult
from archeos.workspace import (
    MANAGED_CONFIG_COMMENT,
    MANAGED_TABLE,
    codex_integration_status,
    initialize_workspace,
    install_codex_integration,
    load_workspace_config,
    remove_codex_integration,
)
from archeos.world_model import SQLiteWorldModelRepository


@contextmanager
def _chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class _SyntheticRepresentationAdapter:
    name = "synthetic-workspace"
    version = "1.0"
    kind = "synthetic_workspace"
    supported_media_types = ("text/markdown",)

    def build(self, source, materialized_path, staging_dir, configuration):
        artifact_path = staging_dir / "artifacts" / "synthetic.json"
        artifact_path.write_text('{"synthetic":true}\n', encoding="utf-8")
        return AdapterBuildResult(
            self.kind,
            (AdapterArtifact("structure", "artifacts/synthetic.json", "application/json"),),
            1.0,
        )


class WorkspaceAndMcpTest(unittest.TestCase):
    def test_production_cli_defaults_use_configured_workspace_across_code_cwds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "stable-workspace"
            config = root / "archeos.toml"
            initialize_workspace(workspace, config_path=config)
            code_a = root / "code-a"
            code_b = root / "code-b"
            code_a.mkdir()
            code_b.mkdir()
            external = root / "fixture.md"
            external.write_text("# Synthetic workspace fixture\n", encoding="utf-8")

            with _chdir(code_a):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([
                        "source", "--config", str(config), "admit", str(external), "--media-type", "text/markdown",
                    ]), 0)
                source_id = json.loads(output.getvalue())["source"]["source_id"]
                output = StringIO()
                with (
                    mock.patch("archeos.cli.production_adapter", return_value=_SyntheticRepresentationAdapter()),
                    redirect_stdout(output),
                ):
                    self.assertEqual(main([
                        "representation", "--config", str(config), "build", source_id,
                        "--adapter", "synthetic-workspace",
                    ]), 0)
                representation_id = json.loads(output.getvalue())["representation"]["representation_id"]

            (code_b / "01_inbox").mkdir()
            (code_b / "03_information").mkdir()
            with _chdir(code_b):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["source", "--config", str(config), "list"]), 0)
                self.assertEqual([item["source_id"] for item in json.loads(output.getvalue())], [source_id])
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([
                        "representation", "--config", str(config), "show", representation_id,
                    ]), 0)
                self.assertEqual(json.loads(output.getvalue())["representation_id"], representation_id)

            with SQLiteWorldModelRepository(workspace / "04_core" / "archeos.sqlite3") as repository:
                record = repository.create_object("Synthetic workspace object")
            with _chdir(code_b):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([
                        "context", "--config", str(config), "build", "--scope", "object", record.object_id,
                    ]), 0)
                self.assertEqual(json.loads(output.getvalue())["root"]["object_id"], record.object_id)

            shutil.rmtree(code_a)
            self.assertTrue((workspace / "01_inbox" / "sources" / source_id / "manifest.json").is_file())
            self.assertTrue((workspace / "02_processing" / "representations").is_dir())
            self.assertTrue((workspace / "04_core" / "archeos.sqlite3").is_file())

    def test_omitted_paths_fail_closed_and_explicit_paths_bypass_workspace_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code = root / "code"
            code.mkdir()
            with _chdir(code):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["source", "list"]), 1)
                self.assertIn("error:", output.getvalue())
                self.assertFalse((code / "01_inbox").exists())

                explicit = root / "explicit-sources"
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["source", "--managed-root", str(explicit), "list"]), 0)
                self.assertEqual(json.loads(output.getvalue()), [])
                self.assertFalse((code / "01_inbox").exists())

    def test_shared_resolver_covers_package_audit_and_governance_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            config = root / "archeos.toml"
            initialize_workspace(workspace, config_path=config)
            parser = build_parser()
            args = parser.parse_args([
                "information", "--config", str(config), "extract", "repr_" + "a" * 64,
                "--external-agent-route", "codex-cli",
            ])
            _resolve_durable_paths(args)
            self.assertEqual(args.managed_root, workspace.resolve() / "01_inbox")
            self.assertEqual(args.representation_root, workspace.resolve() / "02_processing" / "representations")
            self.assertEqual(args.output_root, workspace.resolve() / "02_processing" / "information")
            self.assertEqual(args.store, workspace.resolve() / "03_information" / "atomic_information.jsonl")
            self.assertEqual(args.audit_root, workspace.resolve() / "02_processing" / "semantic_handoff_runs")

            args = parser.parse_args(["digest", "--config", str(config), "pending"])
            _resolve_durable_paths(args)
            self.assertEqual(args.proposal_store, workspace.resolve() / "03_information" / "change_proposals.jsonl")
            self.assertEqual(args.journal, workspace.resolve() / "03_information" / "change_journal.jsonl")
            self.assertEqual(args.database, workspace.resolve() / "04_core" / "archeos.sqlite3")

    def test_doctor_reports_workspace_as_durable_authority_and_git_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            config = root / "archeos.toml"
            initialize_workspace(workspace, config_path=config)
            (workspace / ".git").mkdir()
            report = workspace_module.doctor(config)
            self.assertEqual(report["workspace_config"], "valid")
            self.assertEqual(report["durable_path_authority"], "configured_workspace")
            self.assertEqual(report["workspace_worktree_coupling"], "detected")

    def test_init_is_safe_idempotent_and_config_show_and_doctor_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            config = root / "config.toml"
            first, changed = initialize_workspace(workspace, config_path=config)
            second, repeated = initialize_workspace(workspace, config_path=config)
            self.assertTrue(changed)
            self.assertFalse(repeated)
            self.assertEqual(first, second)
            self.assertEqual(load_workspace_config(config).workspace, workspace.resolve())
            self.assertIn("01_inbox/**", (workspace / ".gitignore").read_text(encoding="utf-8"))

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["config", "--config", str(config), "show"]), 0)
            self.assertEqual(json.loads(output.getvalue())["workspace"], str(workspace.resolve()))
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["doctor", "--config", str(config)]), 0)

    def test_doctor_reports_audio_and_document_optional_runtime_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _ = initialize_workspace(root, config_path=root / "archeos.toml")
            with mock.patch.object(workspace_module, "_optional_audio_status", return_value="unavailable"):
                with mock.patch.object(workspace_module.importlib.util, "find_spec", return_value=None):
                    report = workspace_module.doctor(root / "archeos.toml")
            self.assertEqual(report["optional_audio_runtime"], "unavailable")
            self.assertEqual(report["optional_document_runtime"], "unavailable")

    def test_init_refuses_to_retarget_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            initialize_workspace(root / "one", config_path=config)
            with self.assertRaisesRegex(ValueError, "another workspace"):
                initialize_workspace(root / "two", config_path=config)

    def test_codex_integration_preserves_existing_config_and_removes_only_managed_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = initialize_workspace(root / "workspace", config_path=root / "archeos.toml")
            codex_config = root / "codex.toml"
            existing = "[mcp_servers.existing]\ncommand = \"existing-server\"\n"
            codex_config.write_text(existing, encoding="utf-8")
            install_codex_integration(config, codex_config)
            first = codex_config.read_text(encoding="utf-8")
            install_codex_integration(config, codex_config)
            self.assertEqual(codex_config.read_text(encoding="utf-8"), first)
            self.assertIn(existing, first)
            self.assertIn(MANAGED_CONFIG_COMMENT, first)
            self.assertIn(MANAGED_TABLE, first)
            self.assertEqual(codex_integration_status(config, codex_config)["state"], "configured")
            self.assertEqual(remove_codex_integration(codex_config), "removed")
            self.assertEqual(codex_config.read_text(encoding="utf-8"), existing)
            self.assertEqual(remove_codex_integration(codex_config), "not_installed")

    def test_codex_integration_refuses_to_replace_unmanaged_archeos_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = initialize_workspace(root / "workspace", config_path=root / "archeos.toml")
            codex_config = root / "codex.toml"
            original = "[mcp_servers.archeos]\ncommand = \"someone-else\"\n"
            codex_config.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                install_codex_integration(config, codex_config)
            self.assertEqual(codex_config.read_text(encoding="utf-8"), original)

    def test_codex_integration_fails_closed_when_config_changes_before_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, _ = initialize_workspace(root / "workspace", config_path=root / "archeos.toml")
            codex_config = root / "codex.toml"
            original = "[mcp_servers.existing]\ncommand = \"existing-server\"\n"
            changed = original + "[profiles.default]\nmodel = \"user-change\"\n"
            codex_config.write_text(original, encoding="utf-8")
            original_write = workspace_module._write_private_config

            def write_after_user_change(
                path: Path,
                content: str,
                *,
                expected_snapshot: object,
            ) -> None:
                codex_config.write_text(changed, encoding="utf-8")
                original_write(path, content, expected_snapshot=expected_snapshot)  # type: ignore[arg-type]

            with mock.patch.object(workspace_module, "_write_private_config", side_effect=write_after_user_change):
                with self.assertRaisesRegex(ValueError, "changed during integration"):
                    install_codex_integration(config, codex_config)
            self.assertEqual(codex_config.read_text(encoding="utf-8"), changed)
            self.assertFalse((root / ".codex.toml.archeos.lock").exists())

    def test_read_only_server_exposes_only_canonical_read_tools_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_workspace(root, config_path=root / "archeos.toml")
            database = root / "04_core" / "archeos.sqlite3"
            with SQLiteWorldModelRepository(database) as repository:
                record = repository.create_object("Synthetic MCP Object", roles=("project",))
            before = database.read_bytes()
            service = CanonicalReadService(root)
            context = service.context_build(record.object_id, max_relationships=1, max_information=1, max_changes=1, max_pending=1, max_evidence=1)
            self.assertEqual(context["root"]["object_id"], record.object_id)
            self.assertEqual(before, database.read_bytes())
            with self.assertRaisesRegex(ValueError, "object not found"):
                service.object_resolve("obj_missing")
            server = create_server(root)
            tools = asyncio.run(server.list_tools())
            self.assertEqual(
                {tool.name for tool in tools},
                {"archeos_object_resolve", "archeos_context_build", "archeos_source_show", "archeos_source_verify"},
            )
            self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
            self.assertTrue(all(not tool.annotations.destructiveHint for tool in tools))

    def test_mcp_context_tool_is_callable_for_a_synthetic_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_workspace(root, config_path=root / "archeos.toml")
            with SQLiteWorldModelRepository(root / "04_core" / "archeos.sqlite3") as repository:
                record = repository.create_object("Synthetic MCP Context")
            server = create_server(root)
            _, result = asyncio.run(server.call_tool("archeos_context_build", {"object_id": record.object_id, "max_information": 1}))
            self.assertEqual(result["root"]["object_id"], record.object_id)

    def test_mcp_context_preserves_pending_and_truncation_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_workspace(root, config_path=root / "archeos.toml")
            with SQLiteWorldModelRepository(root / "04_core" / "archeos.sqlite3") as repository:
                record = repository.create_object("Synthetic bounded MCP Context")

            revisions = []
            for candidate in ("first", "second"):
                source_id = "src_" + "a" * 32
                atomic_id = "atomic_info_" + hashlib.sha256(f"{source_id}\0{candidate}".encode()).hexdigest()[:32]
                revisions.append(AtomicInformationRevision(
                    atomic_information_id=atomic_id,
                    revision_number=1,
                    revision_id=f"{atomic_id}-r0001",
                    origin_source_id=source_id,
                    origin_candidate_id=candidate,
                    origin_fingerprint=hashlib.sha256(("origin-" + candidate).encode()).hexdigest(),
                    statement=f"Synthetic {candidate} statement.",
                    semantic_type="observation",
                    raw_concerns=(candidate,),
                    related_object_ids=(record.object_id,),
                    source_evidence=(EvidenceRecord(source_id, "synthetic", 1, "Speaker_1", "00:00:01.000", "00:00:02.000", candidate),),
                    context="Synthetic MCP context.",
                    confidence=0.8,
                    created_at="2026-08-13T00:00:00+00:00",
                    revision_reason="initial_ingestion",
                ))
            JsonlAtomicInformationStore(root / "03_information" / "atomic_information.jsonl").ingest_batch(revisions)
            proposals = JsonlChangeProposalStore(root / "03_information" / "change_proposals.jsonl")
            review = HumanReviewContent("Synthetic finding", "medium", "Review", "Synthetic evidence", "Synthetic consequence")
            for index, revision in enumerate(revisions, start=1):
                proposals.add_pending(ChangeProposal(
                    f"proposal-{index}", revision.atomic_information_id, revision.revision_id,
                    (WorldModelOperation("no_structural_change", target_object_id=record.object_id),),
                    (record.object_id,), "Synthetic pending", (revision.origin_source_id,), "before",
                    f"fingerprint-{index}", review, "pending", f"2026-08-13T00:00:0{index}+00:00", None, None, None,
                ))

            _, context = asyncio.run(create_server(root).call_tool(
                "archeos_context_build",
                {"object_id": record.object_id, "max_information": 1, "max_pending": 1},
            ))
            self.assertTrue(context["metadata"]["atomic_information"]["truncated"])
            self.assertTrue(context["metadata"]["pending_judgments"]["truncated"])
            self.assertEqual(len(context["pending_judgments"]), 1)

    def test_stdio_mcp_lists_only_read_tools_and_fails_closed_for_unknown_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_workspace(root, config_path=root / "archeos.toml")
            with SQLiteWorldModelRepository(root / "04_core" / "archeos.sqlite3") as repository:
                record = repository.create_object("Synthetic STDIO Object")

            async def smoke() -> tuple[set[str], object, object]:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "archeos", "mcp", "serve", "--workspace", str(root)],
                    cwd=Path(__file__).resolve().parents[1],
                )
                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        context = await session.call_tool("archeos_context_build", {"object_id": record.object_id})
                        unknown = await session.call_tool("archeos_object_resolve", {"object_id": "obj_missing"})
                        return {tool.name for tool in tools.tools}, context, unknown

            names, context, unknown = asyncio.run(smoke())
            self.assertEqual(names, {"archeos_object_resolve", "archeos_context_build", "archeos_source_show", "archeos_source_verify"})
            self.assertFalse(context.isError)
            self.assertEqual(context.structuredContent["root"]["object_id"], record.object_id)
            self.assertTrue(unknown.isError)


if __name__ == "__main__":
    unittest.main()
