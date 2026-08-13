"""A small, local, read-only MCP projection over canonical ArcheOS services."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .atomic_information import JsonlAtomicInformationStore
from .context import ContextBuilder, ContextRequest
from .digestion import JsonlChangeJournal, JsonlChangeProposalStore
from .source import LocalManagedSourceRepository, ManagedSourceService
from .workspace import WORKSPACE_DIRECTORIES
from .world_model import ObjectResolver, SQLiteWorldModelRepository

INSTRUCTIONS = (
    "ArcheOS provides local, read-only Context and Evidence. Before long-term business "
    "judgments, query bounded context when relevant. Treat pending, conflict, and truncation "
    "as disclosures, not facts. These tools cannot create or change Objects, Relationships, "
    "Decisions, Sources, or other Core data; use governed proposal/input workflows for writes."
)


class CanonicalReadService:
    """Composition root only; all data access goes through existing read services."""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        missing = [name for name in WORKSPACE_DIRECTORIES if not (self.workspace / name).is_dir()]
        if missing:
            raise ValueError("workspace is not initialized: missing " + ", ".join(missing))

    @property
    def database(self) -> Path:
        return self.workspace / "04_core" / "archeos.sqlite3"

    def object_resolve(self, object_id: str) -> dict[str, object]:
        with SQLiteWorldModelRepository(self.database, read_only=True) as repository:
            return asdict(ObjectResolver(repository).resolve(object_id))

    def context_build(
        self,
        object_id: str,
        max_relationships: int = 50,
        max_information: int = 50,
        max_changes: int = 50,
        max_pending: int = 20,
        max_evidence: int = 5,
    ) -> dict[str, object]:
        with SQLiteWorldModelRepository(self.database, read_only=True) as repository:
            bundle = ContextBuilder(
                repository,
                ObjectResolver(repository),
                JsonlAtomicInformationStore(self.workspace / "03_information" / "atomic_information.jsonl"),
                JsonlChangeJournal(self.workspace / "03_information" / "change_journal.jsonl"),
                JsonlChangeProposalStore(self.workspace / "03_information" / "change_proposals.jsonl"),
            ).build(
                ContextRequest(
                    "object", object_id,
                    max_relationships=max_relationships,
                    max_atomic_information=max_information,
                    max_changes=max_changes,
                    max_pending_judgments=max_pending,
                    max_evidence_per_information=max_evidence,
                )
            )
        return asdict(bundle)

    def source_show(self, source_id: str) -> dict[str, object]:
        return ManagedSourceService(LocalManagedSourceRepository(self.workspace / "01_inbox")).show(source_id).to_dict()

    def source_verify(self, source_id: str) -> dict[str, object]:
        return ManagedSourceService(LocalManagedSourceRepository(self.workspace / "01_inbox")).verify(source_id).to_dict()


def create_server(workspace: Path | str) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - packaging guarantees this dependency
        raise RuntimeError("MCP support is unavailable; reinstall ArcheOS core dependencies") from exc
    service = CanonicalReadService(workspace)
    server = FastMCP("ArcheOS", instructions=INSTRUCTIONS, json_response=True)

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(name="archeos_object_resolve", annotations=read_only)
    def archeos_object_resolve(object_id: str) -> dict[str, object]:
        """Read one canonical Object by its stable ID. This tool never writes Core data."""
        return service.object_resolve(object_id)

    @server.tool(name="archeos_context_build", annotations=read_only)
    def archeos_context_build(
        object_id: str,
        max_relationships: int = 50,
        max_information: int = 50,
        max_changes: int = 50,
        max_pending: int = 20,
        max_evidence: int = 5,
    ) -> dict[str, object]:
        """Build bounded canonical Context; metadata preserves truncation and pending judgments."""
        return service.context_build(object_id, max_relationships, max_information, max_changes, max_pending, max_evidence)

    @server.tool(name="archeos_source_show", annotations=read_only)
    def archeos_source_show(source_id: str) -> dict[str, object]:
        """Read one Managed Source through the canonical Source service without materializing it."""
        return service.source_show(source_id)

    @server.tool(name="archeos_source_verify", annotations=read_only)
    def archeos_source_verify(source_id: str) -> dict[str, object]:
        """Verify immutable Managed Source bytes through the canonical Source service."""
        return service.source_verify(source_id)

    return server


def serve(workspace: Path | str) -> None:
    create_server(workspace).run(transport="stdio")
