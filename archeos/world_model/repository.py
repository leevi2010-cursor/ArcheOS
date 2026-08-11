from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .models import (
    LifecycleRecord,
    NameAssignment,
    ObjectRecord,
    RelationshipRecord,
    RoleAssignment,
)


class WorldModelRepository(Protocol):
    def initialize(self) -> None: ...

    def close(self) -> None: ...

    def create_object(
        self,
        name: str,
        *,
        roles: Iterable[str] = (),
    ) -> ObjectRecord: ...

    def get_object(self, object_id: str) -> ObjectRecord: ...

    def list_objects(self) -> tuple[ObjectRecord, ...]: ...

    def rename_object(self, object_id: str, name: str) -> NameAssignment: ...

    def list_names(
        self,
        object_id: str,
        *,
        active_only: bool = False,
    ) -> tuple[NameAssignment, ...]: ...

    def add_role(
        self,
        object_id: str,
        role: str,
        *,
        source_note_id: str | None = None,
        confidence: float | None = None,
    ) -> RoleAssignment: ...

    def end_role(self, object_id: str, role: str) -> RoleAssignment: ...

    def list_roles(
        self,
        object_id: str,
        *,
        active_only: bool = False,
    ) -> tuple[RoleAssignment, ...]: ...

    def set_lifecycle(
        self,
        object_id: str,
        *,
        state: str,
        start_at: str | None = None,
        actual_end_at: str | None = None,
        target_end_at: str | None = None,
        completion_condition: str | None = None,
    ) -> LifecycleRecord: ...

    def list_lifecycles(
        self,
        object_id: str,
        *,
        active_only: bool = False,
    ) -> tuple[LifecycleRecord, ...]: ...

    def create_relationship(
        self,
        from_object_id: str,
        relation: str,
        to_object_id: str,
        *,
        source_note_id: str | None = None,
        confidence: float | None = None,
    ) -> RelationshipRecord: ...

    def end_relationship(self, relationship_id: str) -> RelationshipRecord: ...

    def list_relationships(
        self,
        *,
        object_id: str | None = None,
        active_only: bool = True,
    ) -> tuple[RelationshipRecord, ...]: ...
