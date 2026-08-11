from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

from .models import (
    ALLOWED_ROLES,
    ApplyReceiptRecord,
    LifecycleRecord,
    NameAssignment,
    ObjectRecord,
    RelationshipRecord,
    RoleAssignment,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _non_empty(value, field)


def _validate_confidence(confidence: float | None) -> float | None:
    if confidence is None:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise TypeError("confidence must be a number between 0 and 1")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return float(confidence)


class SQLiteWorldModelRepository:
    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._transaction_depth = 0
        self.initialize()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        outermost = self._transaction_depth == 0
        if outermost:
            self._connection.execute("BEGIN")
        self._transaction_depth += 1
        try:
            yield
        except Exception:
            self._transaction_depth -= 1
            if outermost:
                self._connection.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                self._connection.commit()

    @contextmanager
    def _write_scope(self) -> Iterator[None]:
        if self._transaction_depth:
            yield
            return
        with self._connection:
            yield

    def initialize(self) -> None:
        allowed_roles = ", ".join(f"'{role}'" for role in sorted(ALLOWED_ROLES))
        self._connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS objects (
                object_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS object_names (
                name_assignment_id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL REFERENCES objects(object_id) ON DELETE RESTRICT,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS object_names_active_primary
                ON object_names(object_id)
                WHERE is_primary = 1 AND valid_to IS NULL;
            CREATE INDEX IF NOT EXISTS object_names_object_id
                ON object_names(object_id);

            CREATE TABLE IF NOT EXISTS role_assignments (
                role_assignment_id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL REFERENCES objects(object_id) ON DELETE RESTRICT,
                role TEXT NOT NULL CHECK (role IN ({allowed_roles})),
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                source_atomic_information_id TEXT,
                confidence REAL CHECK (
                    confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
                )
            );

            CREATE UNIQUE INDEX IF NOT EXISTS role_assignments_active_role
                ON role_assignments(object_id, role)
                WHERE valid_to IS NULL;
            CREATE INDEX IF NOT EXISTS role_assignments_object_id
                ON role_assignments(object_id);

            CREATE TABLE IF NOT EXISTS object_lifecycles (
                lifecycle_record_id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL REFERENCES objects(object_id) ON DELETE RESTRICT,
                start_at TEXT,
                actual_end_at TEXT,
                target_end_at TEXT,
                completion_condition TEXT,
                state TEXT NOT NULL CHECK (length(trim(state)) > 0),
                valid_from TEXT NOT NULL,
                valid_to TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS object_lifecycles_active
                ON object_lifecycles(object_id)
                WHERE valid_to IS NULL;
            CREATE INDEX IF NOT EXISTS object_lifecycles_object_id
                ON object_lifecycles(object_id);

            CREATE TABLE IF NOT EXISTS relationships (
                relationship_id TEXT PRIMARY KEY,
                from_object_id TEXT NOT NULL
                    REFERENCES objects(object_id) ON DELETE RESTRICT,
                relation TEXT NOT NULL CHECK (length(trim(relation)) > 0),
                to_object_id TEXT NOT NULL
                    REFERENCES objects(object_id) ON DELETE RESTRICT,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                source_atomic_information_id TEXT,
                confidence REAL CHECK (
                    confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
                )
            );

            CREATE UNIQUE INDEX IF NOT EXISTS relationships_active_edge
                ON relationships(from_object_id, relation, to_object_id)
                WHERE valid_to IS NULL;
            CREATE INDEX IF NOT EXISTS relationships_from_object_id
                ON relationships(from_object_id);
            CREATE INDEX IF NOT EXISTS relationships_to_object_id
                ON relationships(to_object_id);

            CREATE TABLE IF NOT EXISTS apply_receipts (
                apply_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL CHECK (length(trim(payload)) > 0),
                created_at TEXT NOT NULL
            );
            """
        )

    def create_object(
        self,
        name: str,
        *,
        roles: Iterable[str] = (),
    ) -> ObjectRecord:
        clean_name = _non_empty(name, "name")
        role_values = tuple(roles)
        if len(set(role_values)) != len(role_values):
            raise ValueError("roles must not contain duplicates")
        for role in role_values:
            self._validate_role(role)

        object_id = _identifier("obj")
        name_assignment_id = _identifier("name")
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                "INSERT INTO objects VALUES (?, ?, ?, ?)",
                (object_id, "active", now, now),
            )
            self._connection.execute(
                "INSERT INTO object_names VALUES (?, ?, ?, ?, NULL, 1)",
                (name_assignment_id, object_id, clean_name, now),
            )
            for role in role_values:
                self._connection.execute(
                    """
                    INSERT INTO role_assignments
                    VALUES (?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (_identifier("role"), object_id, role, now),
                )
        return self.get_object(object_id)

    def get_object(self, object_id: str) -> ObjectRecord:
        row = self._connection.execute(
            "SELECT * FROM objects WHERE object_id = ?", (object_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"object not found: {object_id}")
        return self._object_record(row)

    def list_objects(self) -> tuple[ObjectRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM objects ORDER BY rowid"
        ).fetchall()
        return tuple(self._object_record(row) for row in rows)

    def set_object_status(self, object_id: str, status: str) -> ObjectRecord:
        clean_status = _non_empty(status, "status")
        if clean_status not in {"active", "deleted"}:
            raise ValueError(f"unsupported object status: {clean_status}")
        current = self.get_object(object_id)
        if current.status == clean_status:
            return current
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                "UPDATE objects SET status = ?, updated_at = ? WHERE object_id = ?",
                (clean_status, now, object_id),
            )
        return self.get_object(object_id)

    def get_apply_receipt(self, apply_id: str) -> ApplyReceiptRecord | None:
        clean_apply_id = _non_empty(apply_id, "apply_id")
        row = self._connection.execute(
            "SELECT * FROM apply_receipts WHERE apply_id = ?", (clean_apply_id,)
        ).fetchone()
        if row is None:
            return None
        return ApplyReceiptRecord(row["apply_id"], row["payload"], row["created_at"])

    def put_apply_receipt(self, apply_id: str, payload: str) -> ApplyReceiptRecord:
        clean_apply_id = _non_empty(apply_id, "apply_id")
        clean_payload = _non_empty(payload, "payload")
        existing = self.get_apply_receipt(clean_apply_id)
        if existing is not None:
            if existing.payload != clean_payload:
                raise ValueError(f"apply receipt identity collision: {clean_apply_id}")
            return existing
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                "INSERT INTO apply_receipts VALUES (?, ?, ?)",
                (clean_apply_id, clean_payload, now),
            )
        return ApplyReceiptRecord(clean_apply_id, clean_payload, now)

    def rename_object(self, object_id: str, name: str) -> NameAssignment:
        clean_name = _non_empty(name, "name")
        self.get_object(object_id)
        assignment_id = _identifier("name")
        now = _utc_now()
        with self._write_scope():
            updated = self._connection.execute(
                """
                UPDATE object_names
                SET valid_to = ?
                WHERE object_id = ? AND is_primary = 1 AND valid_to IS NULL
                """,
                (now, object_id),
            )
            if updated.rowcount != 1:
                raise ValueError(
                    f"object must have exactly one active primary name: {object_id}"
                )
            self._connection.execute(
                "INSERT INTO object_names VALUES (?, ?, ?, ?, NULL, 1)",
                (assignment_id, object_id, clean_name, now),
            )
            self._touch_object(object_id, now)
        return NameAssignment(
            assignment_id,
            object_id,
            clean_name,
            now,
            None,
            True,
        )

    def list_names(
        self,
        object_id: str,
        *,
        active_only: bool = False,
    ) -> tuple[NameAssignment, ...]:
        self.get_object(object_id)
        query = "SELECT * FROM object_names WHERE object_id = ?"
        if active_only:
            query += " AND valid_to IS NULL"
        query += " ORDER BY rowid"
        rows = self._connection.execute(query, (object_id,)).fetchall()
        return tuple(self._name_assignment(row) for row in rows)

    def add_role(
        self,
        object_id: str,
        role: str,
        *,
        source_atomic_information_id: str | None = None,
        confidence: float | None = None,
    ) -> RoleAssignment:
        self.get_object(object_id)
        clean_role = self._validate_role(role)
        clean_source_atomic_information_id = _optional_text(
            source_atomic_information_id, "source_atomic_information_id"
        )
        clean_confidence = _validate_confidence(confidence)
        active = self._connection.execute(
            """
            SELECT 1 FROM role_assignments
            WHERE object_id = ? AND role = ? AND valid_to IS NULL
            """,
            (object_id, clean_role),
        ).fetchone()
        if active is not None:
            raise ValueError(f"role is already active: {clean_role}")

        assignment_id = _identifier("role")
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                """
                INSERT INTO role_assignments
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    assignment_id,
                    object_id,
                    clean_role,
                    now,
                    clean_source_atomic_information_id,
                    clean_confidence,
                ),
            )
            self._touch_object(object_id, now)
        return RoleAssignment(
            assignment_id,
            object_id,
            clean_role,
            now,
            None,
            clean_source_atomic_information_id,
            clean_confidence,
        )

    def end_role(self, object_id: str, role: str) -> RoleAssignment:
        self.get_object(object_id)
        clean_role = self._validate_role(role)
        row = self._connection.execute(
            """
            SELECT * FROM role_assignments
            WHERE object_id = ? AND role = ? AND valid_to IS NULL
            """,
            (object_id, clean_role),
        ).fetchone()
        if row is None:
            raise ValueError(f"active role not found: {clean_role}")
        assignment = self._role_assignment(row)
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                "UPDATE role_assignments SET valid_to = ? WHERE role_assignment_id = ?",
                (now, assignment.role_assignment_id),
            )
            self._touch_object(object_id, now)
        return replace(assignment, valid_to=now)

    def list_roles(
        self,
        object_id: str,
        *,
        active_only: bool = False,
    ) -> tuple[RoleAssignment, ...]:
        self.get_object(object_id)
        query = "SELECT * FROM role_assignments WHERE object_id = ?"
        if active_only:
            query += " AND valid_to IS NULL"
        query += " ORDER BY rowid"
        rows = self._connection.execute(query, (object_id,)).fetchall()
        return tuple(self._role_assignment(row) for row in rows)

    def set_lifecycle(
        self,
        object_id: str,
        *,
        state: str,
        start_at: str | None = None,
        actual_end_at: str | None = None,
        target_end_at: str | None = None,
        completion_condition: str | None = None,
    ) -> LifecycleRecord:
        self.get_object(object_id)
        clean_state = _non_empty(state, "state")
        clean_start_at = _optional_text(start_at, "start_at")
        clean_actual_end_at = _optional_text(actual_end_at, "actual_end_at")
        clean_target_end_at = _optional_text(target_end_at, "target_end_at")
        clean_completion_condition = _optional_text(
            completion_condition, "completion_condition"
        )
        lifecycle_id = _identifier("life")
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                """
                UPDATE object_lifecycles SET valid_to = ?
                WHERE object_id = ? AND valid_to IS NULL
                """,
                (now, object_id),
            )
            self._connection.execute(
                """
                INSERT INTO object_lifecycles
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    lifecycle_id,
                    object_id,
                    clean_start_at,
                    clean_actual_end_at,
                    clean_target_end_at,
                    clean_completion_condition,
                    clean_state,
                    now,
                ),
            )
            self._touch_object(object_id, now)
        return LifecycleRecord(
            lifecycle_id,
            object_id,
            clean_start_at,
            clean_actual_end_at,
            clean_target_end_at,
            clean_completion_condition,
            clean_state,
            now,
            None,
        )

    def list_lifecycles(
        self,
        object_id: str,
        *,
        active_only: bool = False,
    ) -> tuple[LifecycleRecord, ...]:
        self.get_object(object_id)
        query = "SELECT * FROM object_lifecycles WHERE object_id = ?"
        if active_only:
            query += " AND valid_to IS NULL"
        query += " ORDER BY rowid"
        rows = self._connection.execute(query, (object_id,)).fetchall()
        return tuple(self._lifecycle_record(row) for row in rows)

    def create_relationship(
        self,
        from_object_id: str,
        relation: str,
        to_object_id: str,
        *,
        source_atomic_information_id: str | None = None,
        confidence: float | None = None,
    ) -> RelationshipRecord:
        self.get_object(from_object_id)
        self.get_object(to_object_id)
        clean_relation = _non_empty(relation, "relation")
        clean_source_atomic_information_id = _optional_text(
            source_atomic_information_id, "source_atomic_information_id"
        )
        clean_confidence = _validate_confidence(confidence)
        relationship_id = _identifier("rel")
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                """
                INSERT INTO relationships
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    relationship_id,
                    from_object_id,
                    clean_relation,
                    to_object_id,
                    now,
                    clean_source_atomic_information_id,
                    clean_confidence,
                ),
            )
        return RelationshipRecord(
            relationship_id,
            from_object_id,
            clean_relation,
            to_object_id,
            now,
            None,
            clean_source_atomic_information_id,
            clean_confidence,
        )

    def end_relationship(self, relationship_id: str) -> RelationshipRecord:
        row = self._connection.execute(
            """
            SELECT * FROM relationships
            WHERE relationship_id = ? AND valid_to IS NULL
            """,
            (relationship_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"active relationship not found: {relationship_id}")
        relationship = self._relationship_record(row)
        now = _utc_now()
        with self._write_scope():
            self._connection.execute(
                "UPDATE relationships SET valid_to = ? WHERE relationship_id = ?",
                (now, relationship_id),
            )
        return replace(relationship, valid_to=now)

    def list_relationships(
        self,
        *,
        object_id: str | None = None,
        active_only: bool = True,
    ) -> tuple[RelationshipRecord, ...]:
        parameters: tuple[str, ...] = ()
        query = "SELECT * FROM relationships WHERE 1 = 1"
        if object_id is not None:
            self.get_object(object_id)
            query += " AND (from_object_id = ? OR to_object_id = ?)"
            parameters = (object_id, object_id)
        if active_only:
            query += " AND valid_to IS NULL"
        query += " ORDER BY rowid"
        rows = self._connection.execute(query, parameters).fetchall()
        return tuple(self._relationship_record(row) for row in rows)

    def _touch_object(self, object_id: str, updated_at: str) -> None:
        self._connection.execute(
            "UPDATE objects SET updated_at = ? WHERE object_id = ?",
            (updated_at, object_id),
        )

    @staticmethod
    def _validate_role(role: str) -> str:
        clean_role = _non_empty(role, "role")
        if clean_role not in ALLOWED_ROLES:
            raise ValueError(f"unsupported role: {clean_role}")
        return clean_role

    @staticmethod
    def _object_record(row: sqlite3.Row) -> ObjectRecord:
        return ObjectRecord(
            row["object_id"],
            row["status"],
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _name_assignment(row: sqlite3.Row) -> NameAssignment:
        return NameAssignment(
            row["name_assignment_id"],
            row["object_id"],
            row["name"],
            row["valid_from"],
            row["valid_to"],
            bool(row["is_primary"]),
        )

    @staticmethod
    def _role_assignment(row: sqlite3.Row) -> RoleAssignment:
        return RoleAssignment(
            row["role_assignment_id"],
            row["object_id"],
            row["role"],
            row["valid_from"],
            row["valid_to"],
            row["source_atomic_information_id"],
            row["confidence"],
        )

    @staticmethod
    def _lifecycle_record(row: sqlite3.Row) -> LifecycleRecord:
        return LifecycleRecord(
            row["lifecycle_record_id"],
            row["object_id"],
            row["start_at"],
            row["actual_end_at"],
            row["target_end_at"],
            row["completion_condition"],
            row["state"],
            row["valid_from"],
            row["valid_to"],
        )

    @staticmethod
    def _relationship_record(row: sqlite3.Row) -> RelationshipRecord:
        return RelationshipRecord(
            row["relationship_id"],
            row["from_object_id"],
            row["relation"],
            row["to_object_id"],
            row["valid_from"],
            row["valid_to"],
            row["source_atomic_information_id"],
            row["confidence"],
        )
