from __future__ import annotations

from typing import Protocol

from ..atomic_information import AtomicInformationRevision
from .models import (
    ChangeJournalRecord,
    ChangeProposal,
    DigestionWorldState,
    InterpretationResult,
)


class AtomicInformationInterpretationProvider(Protocol):
    name: str

    def interpret(
        self,
        atomic_information: AtomicInformationRevision,
        current_world_state: DigestionWorldState,
    ) -> InterpretationResult: ...


class ChangeProposalStore(Protocol):
    def add_pending(self, proposal: ChangeProposal) -> ChangeProposal: ...

    def get(self, proposal_id: str) -> ChangeProposal: ...

    def list_pending(self) -> tuple[ChangeProposal, ...]: ...

    def update(self, proposal: ChangeProposal) -> ChangeProposal: ...


class ChangeJournal(Protocol):
    def append(self, record: ChangeJournalRecord) -> ChangeJournalRecord: ...

    def get(self, change_id: str) -> ChangeJournalRecord | None: ...

    def list_changes(self) -> tuple[ChangeJournalRecord, ...]: ...


class HumanJudgmentPort(Protocol):
    def render(self, proposal: ChangeProposal) -> str: ...

    def normalize_decision(self, decision: str) -> str: ...
