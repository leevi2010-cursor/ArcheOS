from __future__ import annotations

from .models import ObjectReadModel
from .repository import WorldModelRepository


class ObjectResolver:
    def __init__(self, repository: WorldModelRepository) -> None:
        self.repository = repository

    def resolve(self, object_id: str) -> ObjectReadModel:
        record = self.repository.get_object(object_id)
        primary_names = tuple(
            assignment
            for assignment in self.repository.list_names(object_id, active_only=True)
            if assignment.is_primary
        )
        if len(primary_names) != 1:
            raise ValueError(
                f"object must have exactly one active primary name: {object_id}"
            )
        roles = tuple(
            sorted(
                assignment.role
                for assignment in self.repository.list_roles(
                    object_id, active_only=True
                )
            )
        )
        lifecycles = self.repository.list_lifecycles(object_id, active_only=True)
        if len(lifecycles) > 1:
            raise ValueError(f"object has multiple active lifecycles: {object_id}")
        return ObjectReadModel(
            object_id=record.object_id,
            current_name=primary_names[0].name,
            roles=roles,
            status=record.status,
            lifecycle=lifecycles[0] if lifecycles else None,
        )
