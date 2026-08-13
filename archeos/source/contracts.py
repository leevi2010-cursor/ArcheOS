"""Storage-independent Managed Source repository contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

from .models import ManagedSource, RestoreResult, VerificationResult


class ManagedSourceRepository(Protocol):
    def admit(
        self,
        external_path: Path,
        source_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ManagedSource: ...

    def get(self, source_id: str) -> ManagedSource: ...

    def list_sources(self) -> tuple[ManagedSource, ...]: ...

    def verify(self, source_id: str) -> VerificationResult: ...

    def restore(self, source_id: str, target_path: Path) -> RestoreResult: ...
