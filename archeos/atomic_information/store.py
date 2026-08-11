from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import IngestionResult, AtomicInformationRevision


class AtomicInformationStore(Protocol):
    def ingest_batch(
        self, revisions: Sequence[AtomicInformationRevision]
    ) -> IngestionResult: ...

    def get_current(self, atomic_information_id: str) -> AtomicInformationRevision: ...

    def list_revisions(
        self, atomic_information_id: str
    ) -> tuple[AtomicInformationRevision, ...]: ...

    def append_revision(
        self, revision: AtomicInformationRevision
    ) -> AtomicInformationRevision: ...

    def list_atomic_information(self) -> tuple[AtomicInformationRevision, ...]: ...
