from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .models import (
    IngestionResult,
    NoteRevision,
    note_revision_from_dict,
    note_revision_to_dict,
    validate_note_revision,
)


class JsonlNoteStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def ingest_batch(self, revisions: Sequence[NoteRevision]) -> IngestionResult:
        candidates = tuple(revisions)
        existing_revisions = self._read_all()
        by_origin = {
            (item.origin_source_id, item.origin_candidate_id): item
            for item in existing_revisions
            if item.revision_number == 1
        }
        by_note_id = {item.note_id for item in existing_revisions}
        batch_origins: set[tuple[str, str]] = set()
        created: list[NoteRevision] = []
        existing = 0

        for index, candidate in enumerate(candidates, start=1):
            validate_note_revision(candidate, f"candidate[{index}]")
            if candidate.revision_number != 1:
                raise ValueError("batch ingestion accepts only initial Note revisions")
            origin = (candidate.origin_source_id, candidate.origin_candidate_id)
            if origin in batch_origins:
                raise ValueError(f"duplicate origin candidate in batch: {origin[1]}")
            batch_origins.add(origin)

            stored = by_origin.get(origin)
            if stored is not None:
                if (
                    stored.note_id != candidate.note_id
                    or stored.origin_fingerprint != candidate.origin_fingerprint
                ):
                    raise ValueError(
                        f"origin collision for candidate: {candidate.origin_candidate_id}"
                    )
                existing += 1
                continue
            if candidate.note_id in by_note_id:
                raise ValueError(f"Note identity collision: {candidate.note_id}")
            by_note_id.add(candidate.note_id)
            created.append(candidate)

        if created:
            self._write_all((*existing_revisions, *created))
        return IngestionResult(
            created=len(created),
            existing=existing,
            failed=0,
            note_ids=tuple(item.note_id for item in candidates),
        )

    def get_current(self, note_id: str) -> NoteRevision:
        revisions = self.list_revisions(note_id)
        if not revisions:
            raise ValueError(f"Note not found: {note_id}")
        return revisions[-1]

    def list_revisions(self, note_id: str) -> tuple[NoteRevision, ...]:
        return tuple(item for item in self._read_all() if item.note_id == note_id)

    def append_revision(self, revision: NoteRevision) -> NoteRevision:
        validate_note_revision(revision)
        existing = self._read_all()
        history = tuple(item for item in existing if item.note_id == revision.note_id)
        if not history:
            raise ValueError(f"Note not found: {revision.note_id}")
        current = history[-1]
        if revision.revision_number != current.revision_number + 1:
            raise ValueError("Note revision number must follow the current revision")
        if (
            revision.origin_source_id != current.origin_source_id
            or revision.origin_candidate_id != current.origin_candidate_id
            or revision.origin_fingerprint != current.origin_fingerprint
        ):
            raise ValueError("Note revision cannot change immutable origin provenance")
        self._write_all((*existing, revision))
        return revision

    def list_notes(self) -> tuple[NoteRevision, ...]:
        current: dict[str, NoteRevision] = {}
        order: list[str] = []
        for revision in self._read_all():
            if revision.note_id not in current:
                order.append(revision.note_id)
            current[revision.note_id] = revision
        return tuple(current[note_id] for note_id in order)

    def _read_all(self) -> tuple[NoteRevision, ...]:
        if not self.path.exists():
            return ()
        revisions: list[NoteRevision] = []
        seen_revision_ids: set[str] = set()
        seen_origins: set[tuple[str, str]] = set()
        last_revision: dict[str, int] = {}
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise ValueError(f"corrupted Note store: blank line {line_number}")
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"corrupted Note store at line {line_number}: {exc.msg}"
                    ) from exc
                revision = note_revision_from_dict(payload, f"line {line_number}")
                if revision.revision_id in seen_revision_ids:
                    raise ValueError(
                        f"corrupted Note store: duplicate revision {revision.revision_id}"
                    )
                origin = (
                    revision.origin_source_id,
                    revision.origin_candidate_id,
                )
                if revision.revision_number == 1 and origin in seen_origins:
                    raise ValueError(
                        "corrupted Note store: duplicate origin candidate "
                        f"{revision.origin_candidate_id}"
                    )
                expected = last_revision.get(revision.note_id, 0) + 1
                if revision.revision_number != expected:
                    raise ValueError(
                        f"corrupted Note store: non-contiguous revision for {revision.note_id}"
                    )
                if expected > 1:
                    first = next(
                        item for item in revisions if item.note_id == revision.note_id
                    )
                    if (
                        revision.origin_source_id != first.origin_source_id
                        or revision.origin_candidate_id != first.origin_candidate_id
                        or revision.origin_fingerprint != first.origin_fingerprint
                    ):
                        raise ValueError(
                            f"corrupted Note store: changed origin for {revision.note_id}"
                        )
                seen_revision_ids.add(revision.revision_id)
                seen_origins.add(origin)
                last_revision[revision.note_id] = revision.revision_number
                revisions.append(revision)
        return tuple(revisions)

    def _write_all(self, revisions: Sequence[NoteRevision]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as target:
                temporary_path = Path(target.name)
                for revision in revisions:
                    target.write(
                        json.dumps(
                            note_revision_to_dict(revision),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
