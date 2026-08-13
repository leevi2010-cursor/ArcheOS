"""Storage- and format-independent Representation contracts."""

from __future__ import annotations

from pathlib import Path
from typing import ContextManager, Mapping, Protocol

from .models import (
    AdapterBuildResult,
    NormalizedRepresentation,
    RepresentationVerificationResult,
)
from ..source.models import ManagedSource


class RepresentationAdapter(Protocol):
    name: str
    version: str
    kind: str
    supported_media_types: tuple[str, ...]

    def build(
        self,
        source: ManagedSource,
        materialized_path: Path,
        staging_dir: Path,
        configuration: Mapping[str, object],
    ) -> AdapterBuildResult: ...


class RepresentationRepository(Protocol):
    def staging(self, source_id: str, representation_id: str) -> ContextManager[Path]: ...

    def write_manifest(
        self, staging_dir: Path, representation: NormalizedRepresentation
    ) -> None: ...

    def publish(
        self, staging_dir: Path, representation: NormalizedRepresentation
    ) -> NormalizedRepresentation: ...

    def get(self, representation_id: str) -> NormalizedRepresentation: ...

    def list_for_source(self, source_id: str) -> tuple[NormalizedRepresentation, ...]: ...

    def verify(self, representation_id: str) -> RepresentationVerificationResult: ...
