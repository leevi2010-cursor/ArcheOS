from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from .models import (
    AtomicInformationRevision,
    IngestionResult,
    atomic_information_revision_from_dict,
    atomic_information_revision_to_dict,
    validate_atomic_information_revision,
)


class JsonlAtomicInformationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def ingest_batch(
        self, revisions: Sequence[AtomicInformationRevision]
    ) -> IngestionResult:
        with self._exclusive_lock():
            return self._ingest_batch_locked(revisions)

    def _ingest_batch_locked(
        self, revisions: Sequence[AtomicInformationRevision]
    ) -> IngestionResult:
        candidates = tuple(revisions)
        existing_revisions = self._read_all()
        by_origin = {
            (item.origin_source_id, item.origin_candidate_id): item
            for item in existing_revisions
            if item.revision_number == 1
        }
        by_atomic_information_id = {
            item.atomic_information_id for item in existing_revisions
        }
        batch_origins: set[tuple[str, str]] = set()
        created: list[AtomicInformationRevision] = []
        existing = 0

        for index, candidate in enumerate(candidates, start=1):
            validate_atomic_information_revision(candidate, f"candidate[{index}]")
            if candidate.revision_number != 1:
                raise ValueError(
                    "batch ingestion accepts only initial Atomic Information revisions"
                )
            origin = (candidate.origin_source_id, candidate.origin_candidate_id)
            if origin in batch_origins:
                raise ValueError(f"duplicate origin candidate in batch: {origin[1]}")
            batch_origins.add(origin)

            stored = by_origin.get(origin)
            if stored is not None:
                if (
                    stored.atomic_information_id != candidate.atomic_information_id
                    or stored.origin_fingerprint != candidate.origin_fingerprint
                ):
                    raise ValueError(
                        f"origin collision for candidate: {candidate.origin_candidate_id}"
                    )
                existing += 1
                continue
            if candidate.atomic_information_id in by_atomic_information_id:
                raise ValueError(
                    "Atomic Information identity collision: "
                    f"{candidate.atomic_information_id}"
                )
            by_atomic_information_id.add(candidate.atomic_information_id)
            created.append(candidate)

        if created:
            self._write_all((*existing_revisions, *created))
        return IngestionResult(
            created=len(created),
            existing=existing,
            failed=0,
            atomic_information_ids=tuple(
                item.atomic_information_id for item in candidates
            ),
        )

    def get_current(self, atomic_information_id: str) -> AtomicInformationRevision:
        revisions = self.list_revisions(atomic_information_id)
        if not revisions:
            raise ValueError(f"Atomic Information not found: {atomic_information_id}")
        return revisions[-1]

    def list_revisions(
        self, atomic_information_id: str
    ) -> tuple[AtomicInformationRevision, ...]:
        return tuple(
            item
            for item in self._read_all()
            if item.atomic_information_id == atomic_information_id
        )

    def append_revision(
        self, revision: AtomicInformationRevision
    ) -> AtomicInformationRevision:
        with self._exclusive_lock():
            return self._append_revision_locked(revision)

    def _append_revision_locked(
        self, revision: AtomicInformationRevision
    ) -> AtomicInformationRevision:
        validate_atomic_information_revision(revision)
        existing = self._read_all()
        history = tuple(
            item
            for item in existing
            if item.atomic_information_id == revision.atomic_information_id
        )
        if not history:
            raise ValueError(
                f"Atomic Information not found: {revision.atomic_information_id}"
            )
        current = history[-1]
        if revision.revision_number != current.revision_number + 1:
            raise ValueError(
                "Atomic Information revision number must follow the current revision"
            )
        if (
            revision.origin_source_id != current.origin_source_id
            or revision.origin_candidate_id != current.origin_candidate_id
            or revision.origin_fingerprint != current.origin_fingerprint
        ):
            raise ValueError(
                "Atomic Information revision cannot change immutable origin provenance"
            )
        self._write_all((*existing, revision))
        return revision

    def list_atomic_information(self) -> tuple[AtomicInformationRevision, ...]:
        current: dict[str, AtomicInformationRevision] = {}
        order: list[str] = []
        for revision in self._read_all():
            if revision.atomic_information_id not in current:
                order.append(revision.atomic_information_id)
            current[revision.atomic_information_id] = revision
        return tuple(current[atomic_information_id] for atomic_information_id in order)

    def _read_all(self) -> tuple[AtomicInformationRevision, ...]:
        if not self.path.exists():
            return ()
        revisions: list[AtomicInformationRevision] = []
        seen_revision_ids: set[str] = set()
        seen_origins: set[tuple[str, str]] = set()
        last_revision: dict[str, int] = {}
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise ValueError(
                        f"corrupted Atomic Information store: blank line {line_number}"
                    )
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "corrupted Atomic Information store at line "
                        f"{line_number}: {exc.msg}"
                    ) from exc
                revision = atomic_information_revision_from_dict(
                    payload, f"line {line_number}"
                )
                if revision.revision_id in seen_revision_ids:
                    raise ValueError(
                        "corrupted Atomic Information store: duplicate revision "
                        f"{revision.revision_id}"
                    )
                origin = (
                    revision.origin_source_id,
                    revision.origin_candidate_id,
                )
                if revision.revision_number == 1 and origin in seen_origins:
                    raise ValueError(
                        "corrupted Atomic Information store: duplicate origin candidate "
                        f"{revision.origin_candidate_id}"
                    )
                expected = last_revision.get(revision.atomic_information_id, 0) + 1
                if revision.revision_number != expected:
                    raise ValueError(
                        "corrupted Atomic Information store: non-contiguous revision for "
                        f"{revision.atomic_information_id}"
                    )
                if expected > 1:
                    first = next(
                        item
                        for item in revisions
                        if item.atomic_information_id == revision.atomic_information_id
                    )
                    if (
                        revision.origin_source_id != first.origin_source_id
                        or revision.origin_candidate_id != first.origin_candidate_id
                        or revision.origin_fingerprint != first.origin_fingerprint
                    ):
                        raise ValueError(
                            "corrupted Atomic Information store: changed origin for "
                            f"{revision.atomic_information_id}"
                        )
                seen_revision_ids.add(revision.revision_id)
                seen_origins.add(origin)
                last_revision[revision.atomic_information_id] = revision.revision_number
                revisions.append(revision)
        return tuple(revisions)

    def _write_all(self, revisions: Sequence[AtomicInformationRevision]) -> None:
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
                            atomic_information_revision_to_dict(revision),
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

    @contextmanager
    def _exclusive_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / f".{self.path.name}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
