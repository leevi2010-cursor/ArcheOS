"""Application service façade for Managed Source operations."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .contracts import ManagedSourceRepository
from .models import ManagedSource, RestoreResult, VerificationResult


class ManagedSourceService:
    """Keep CLI/application code independent from the local storage layout."""

    def __init__(self, repository: ManagedSourceRepository) -> None:
        self.repository = repository

    def admit(
        self,
        external_path: Path | str,
        *,
        source_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ManagedSource:
        return self.repository.admit(Path(external_path), source_id, metadata)

    def show(self, source_id: str) -> ManagedSource:
        return self.repository.get(source_id)

    def list(self) -> tuple[ManagedSource, ...]:
        return self.repository.list_sources()

    def verify(self, source_id: str) -> VerificationResult:
        return self.repository.verify(source_id)

    def restore(self, source_id: str, target_path: Path | str) -> RestoreResult:
        return self.repository.restore(source_id, Path(target_path))
