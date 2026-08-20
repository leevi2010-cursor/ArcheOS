from __future__ import annotations

import json
import os
from pathlib import Path

from .models import ChangeJournalRecord, ChangeProposal
from .serialization import (
    journal_from_dict,
    journal_to_dict,
    proposal_from_dict,
    proposal_to_dict,
    validate_journal,
    validate_proposal,
)


def _append_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as target:
        target.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        target.flush()
        os.fsync(target.fileno())


class JsonlChangeProposalStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def add_pending(self, proposal: ChangeProposal) -> ChangeProposal:
        validate_proposal(proposal)
        if proposal.status != "pending" or proposal.decided_at is not None:
            raise ValueError("new Change Proposal must be pending and undecided")
        history = self._read_history()
        latest = self._latest(history)
        for existing in latest.values():
            if (
                existing.atomic_information_revision_id
                == proposal.atomic_information_revision_id
                and existing.interpretation_fingerprint
                == proposal.interpretation_fingerprint
            ):
                return existing
        if proposal.proposal_id in latest:
            raise ValueError(
                f"Change Proposal identity collision: {proposal.proposal_id}"
            )
        _append_json(self.path, proposal_to_dict(proposal))
        return proposal

    def get(self, proposal_id: str) -> ChangeProposal:
        proposal = self._latest(self._read_history()).get(proposal_id)
        if proposal is None:
            raise ValueError(f"Change Proposal not found: {proposal_id}")
        return proposal

    def list_unresolved(self) -> tuple[ChangeProposal, ...]:
        return tuple(
            proposal
            for proposal in self._latest(self._read_history()).values()
            if proposal.status in {"pending", "deferred"}
        )

    def list_history(self) -> tuple[ChangeProposal, ...]:
        return self._read_history()

    def update(self, proposal: ChangeProposal) -> ChangeProposal:
        validate_proposal(proposal)
        history = self._read_history()
        current = self._latest(history).get(proposal.proposal_id)
        if current is None:
            raise ValueError(f"Change Proposal not found: {proposal.proposal_id}")
        if current == proposal:
            return current
        if current.status not in {"pending", "deferred"}:
            raise ValueError("decided Change Proposal cannot be changed")
        if proposal.status not in {"deferred", "approved", "rejected"}:
            raise ValueError("Change Proposal status transition is not supported")
        if proposal.status == "deferred" and proposal.decided_at is not None:
            raise ValueError("deferred Change Proposal must remain undecided")
        if proposal.status in {"approved", "rejected"} and proposal.decided_at is None:
            raise ValueError("Change Proposal decision must be timestamped")
        immutable_current = proposal_to_dict(current)
        immutable_new = proposal_to_dict(proposal)
        for field in immutable_current:
            if field not in {"status", "decided_at"} and (
                immutable_current[field] != immutable_new[field]
            ):
                raise ValueError("Change Proposal decision changed immutable content")
        _append_json(self.path, proposal_to_dict(proposal))
        return proposal

    def _read_history(self) -> tuple[ChangeProposal, ...]:
        if not self.path.exists():
            return ()
        proposals: list[ChangeProposal] = []
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise ValueError(
                        f"corrupted Change Proposal store: blank line {line_number}"
                    )
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "corrupted Change Proposal store at line "
                        f"{line_number}: {exc.msg}"
                    ) from exc
                proposals.append(proposal_from_dict(payload, f"line {line_number}"))
        for proposal_id in {item.proposal_id for item in proposals}:
            revisions = tuple(
                item for item in proposals if item.proposal_id == proposal_id
            )
            if revisions[0].status != "pending" or revisions[0].decided_at is not None:
                raise ValueError(
                    f"corrupted Change Proposal store: invalid decision history for {proposal_id}"
                )
            if any(
                item.status in {"approved", "rejected"} and item.decided_at is None
                for item in revisions
            ):
                raise ValueError(
                    "corrupted Change Proposal store: final decision has no timestamp "
                    f"for {proposal_id}"
                )
            for previous, current in zip(revisions, revisions[1:], strict=False):
                if previous.status not in {
                    "pending",
                    "deferred",
                } or current.status not in {
                    "deferred",
                    "approved",
                    "rejected",
                }:
                    raise ValueError(
                        "corrupted Change Proposal store: invalid decision history "
                        f"for {proposal_id}"
                    )
                previous_payload = proposal_to_dict(previous)
                current_payload = proposal_to_dict(current)
                if any(
                    previous_payload[field] != current_payload[field]
                    for field in previous_payload
                    if field not in {"status", "decided_at"}
                ):
                    raise ValueError(
                        "corrupted Change Proposal store: immutable content changed "
                        f"for {proposal_id}"
                    )
        return tuple(proposals)

    @staticmethod
    def _latest(
        proposals: tuple[ChangeProposal, ...],
    ) -> dict[str, ChangeProposal]:
        latest: dict[str, ChangeProposal] = {}
        for proposal in proposals:
            latest[proposal.proposal_id] = proposal
        return latest


class JsonlChangeJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def append(self, record: ChangeJournalRecord) -> ChangeJournalRecord:
        validate_journal(record)
        existing = self.get(record.change_id)
        if existing is not None:
            if existing != record:
                raise ValueError(
                    f"Change Journal identity collision: {record.change_id}"
                )
            return existing
        _append_json(self.path, journal_to_dict(record))
        return record

    def get(self, change_id: str) -> ChangeJournalRecord | None:
        return next(
            (item for item in self.list_changes() if item.change_id == change_id),
            None,
        )

    def list_changes(self) -> tuple[ChangeJournalRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[ChangeJournalRecord] = []
        seen: set[str] = set()
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise ValueError(
                        f"corrupted Change Journal: blank line {line_number}"
                    )
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"corrupted Change Journal at line {line_number}: {exc.msg}"
                    ) from exc
                record = journal_from_dict(payload, f"line {line_number}")
                if record.change_id in seen:
                    raise ValueError(
                        f"corrupted Change Journal: duplicate change {record.change_id}"
                    )
                seen.add(record.change_id)
                records.append(record)
        return tuple(records)
