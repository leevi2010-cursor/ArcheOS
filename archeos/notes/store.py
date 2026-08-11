from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import IngestionResult, NoteRevision


class NoteStore(Protocol):
    def ingest_batch(self, revisions: Sequence[NoteRevision]) -> IngestionResult: ...

    def get_current(self, note_id: str) -> NoteRevision: ...

    def list_revisions(self, note_id: str) -> tuple[NoteRevision, ...]: ...

    def append_revision(self, revision: NoteRevision) -> NoteRevision: ...

    def list_notes(self) -> tuple[NoteRevision, ...]: ...
