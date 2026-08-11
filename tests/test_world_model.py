from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import archeos.world_model.models as world_model_models
from archeos.world_model import ObjectResolver, SQLiteWorldModelRepository


class WorldModelRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "world-model.sqlite3"
        self.repository = SQLiteWorldModelRepository(self.database)
        self.resolver = ObjectResolver(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary_directory.cleanup()

    def test_stable_identity_after_rename(self) -> None:
        source = self.repository.create_object(
            "Synthetic Operations", roles=("business_line",)
        )
        target = self.repository.create_object("Synthetic Catalog")
        relationship = self.repository.create_relationship(
            source.object_id,
            "test_fixture_relation",
            target.object_id,
        )

        self.repository.rename_object(source.object_id, "Renamed Operations")

        self.assertEqual(
            self.resolver.resolve(source.object_id).object_id, source.object_id
        )
        self.assertEqual(
            self.resolver.resolve(source.object_id).current_name,
            "Renamed Operations",
        )
        self.assertEqual(
            [
                assignment.name
                for assignment in self.repository.list_names(source.object_id)
            ],
            ["Synthetic Operations", "Renamed Operations"],
        )
        stored_relationship = self.repository.list_relationships(
            object_id=source.object_id
        )[0]
        self.assertEqual(
            stored_relationship.relationship_id, relationship.relationship_id
        )
        self.assertEqual(stored_relationship.from_object_id, source.object_id)
        self.assertEqual(stored_relationship.to_object_id, target.object_id)

    def test_role_reinterpretation_preserves_identity_and_history(self) -> None:
        record = self.repository.create_object(
            "Synthetic Initiative", roles=("project",)
        )

        ended = self.repository.end_role(record.object_id, "project")
        self.repository.add_role(record.object_id, "business_line")

        resolved = self.resolver.resolve(record.object_id)
        self.assertEqual(resolved.object_id, record.object_id)
        self.assertEqual(resolved.roles, ("business_line",))
        history = self.repository.list_roles(record.object_id)
        self.assertEqual(
            [assignment.role for assignment in history], ["project", "business_line"]
        )
        self.assertIsNotNone(ended.valid_to)
        self.assertEqual(len(self.repository.list_objects()), 1)

    def test_one_object_supports_multiple_active_roles(self) -> None:
        record = self.repository.create_object(
            "Synthetic Organization", roles=("company", "brand")
        )

        resolved = self.resolver.resolve(record.object_id)

        self.assertEqual(resolved.roles, ("brand", "company"))
        self.assertEqual(len(self.repository.list_objects()), 1)

    def test_ongoing_lifecycle_round_trips_without_end_values(self) -> None:
        record = self.repository.create_object(
            "Ongoing Operations", roles=("business_line",)
        )

        lifecycle = self.repository.set_lifecycle(
            record.object_id,
            state="active",
            start_at="2026-01-01T00:00:00+00:00",
        )

        resolved = self.resolver.resolve(record.object_id)
        self.assertEqual(resolved.lifecycle, lifecycle)
        self.assertIsNone(resolved.lifecycle.target_end_at)
        self.assertIsNone(resolved.lifecycle.actual_end_at)
        self.assertIsNone(resolved.lifecycle.completion_condition)

    def test_bounded_lifecycle_round_trips_separately_from_role(self) -> None:
        record = self.repository.create_object("Bounded Delivery", roles=("project",))

        lifecycle = self.repository.set_lifecycle(
            record.object_id,
            state="active",
            start_at="2026-01-01T00:00:00+00:00",
            target_end_at="2026-06-30T00:00:00+00:00",
            completion_condition="Synthetic delivery accepted",
        )

        resolved = self.resolver.resolve(record.object_id)
        self.assertEqual(resolved.roles, ("project",))
        self.assertEqual(resolved.lifecycle, lifecycle)
        self.assertEqual(
            resolved.lifecycle.completion_condition,
            "Synthetic delivery accepted",
        )

    def test_same_role_does_not_imply_lifecycle_values(self) -> None:
        bounded = self.repository.create_object("Bounded", roles=("project",))
        open_ended = self.repository.create_object("Open Ended", roles=("project",))
        self.repository.set_lifecycle(
            bounded.object_id,
            state="active",
            target_end_at="2026-12-31T00:00:00+00:00",
        )
        self.repository.set_lifecycle(open_ended.object_id, state="active")

        self.assertIsNotNone(
            self.resolver.resolve(bounded.object_id).lifecycle.target_end_at
        )
        self.assertIsNone(
            self.resolver.resolve(open_ended.object_id).lifecycle.target_end_at
        )

    def test_lifecycle_updates_preserve_history(self) -> None:
        record = self.repository.create_object("Lifecycle History")
        first = self.repository.set_lifecycle(record.object_id, state="active")

        second = self.repository.set_lifecycle(
            record.object_id,
            state="completed",
            actual_end_at="2026-03-01T00:00:00+00:00",
        )

        history = self.repository.list_lifecycles(record.object_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].lifecycle_record_id, first.lifecycle_record_id)
        self.assertIsNotNone(history[0].valid_to)
        self.assertEqual(history[1], second)
        self.assertIsNone(history[1].valid_to)

    def test_graph_supports_multiple_incoming_relationships(self) -> None:
        first = self.repository.create_object("First Source")
        second = self.repository.create_object("Second Source")
        target = self.repository.create_object("Shared Target")

        self.repository.create_relationship(
            first.object_id, "test_fixture_relation", target.object_id
        )
        self.repository.create_relationship(
            second.object_id, "test_fixture_relation", target.object_id
        )

        relationships = self.repository.list_relationships(object_id=target.object_id)
        self.assertEqual(len(relationships), 2)
        self.assertEqual(
            {item.from_object_id for item in relationships},
            {first.object_id, second.object_id},
        )

    def test_dangling_relationship_is_rejected(self) -> None:
        source = self.repository.create_object("Existing Source")

        with self.assertRaisesRegex(ValueError, "object not found"):
            self.repository.create_relationship(
                source.object_id,
                "test_fixture_relation",
                "obj_missing",
            )

        self.assertEqual(self.repository.list_relationships(), ())

    def test_relationship_end_preserves_history(self) -> None:
        source = self.repository.create_object("Relationship Source")
        target = self.repository.create_object("Relationship Target")
        created = self.repository.create_relationship(
            source.object_id,
            "test_fixture_relation",
            target.object_id,
        )

        ended = self.repository.end_relationship(created.relationship_id)

        self.assertIsNotNone(ended.valid_to)
        self.assertEqual(self.repository.list_relationships(), ())
        self.assertEqual(
            self.repository.list_relationships(active_only=False),
            (ended,),
        )

    def test_unknown_role_is_rejected_without_creating_object(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported role: initiative"):
            self.repository.create_object("Unsupported", roles=("initiative",))

        self.assertEqual(self.repository.list_objects(), ())

    def test_resolver_returns_human_readable_name_roles_status_and_id(self) -> None:
        record = self.repository.create_object(
            "Readable Object", roles=("company", "brand")
        )

        resolved = self.resolver.resolve(record.object_id)

        self.assertRegex(record.object_id, r"^obj_[0-9a-f]{32}$")
        self.assertEqual(resolved.object_id, record.object_id)
        self.assertEqual(resolved.current_name, "Readable Object")
        self.assertEqual(resolved.roles, ("brand", "company"))
        self.assertEqual(resolved.status, "active")

    def test_role_and_relationship_preserve_optional_source_fields(self) -> None:
        source = self.repository.create_object("Sourced Object")
        target = self.repository.create_object("Sourced Target")

        role = self.repository.add_role(
            source.object_id,
            "project",
            source_note_id="note_test_fixture",
            confidence=0.75,
        )
        relationship = self.repository.create_relationship(
            source.object_id,
            "test_fixture_relation",
            target.object_id,
            source_note_id="note_test_fixture",
            confidence=0.6,
        )

        self.assertEqual(role.source_note_id, "note_test_fixture")
        self.assertEqual(role.confidence, 0.75)
        self.assertEqual(relationship.source_note_id, "note_test_fixture")
        self.assertEqual(relationship.confidence, 0.6)

    def test_rename_does_not_change_relationship_ids_and_resolution(self) -> None:
        source = self.repository.create_object("Old Source Name")
        target = self.repository.create_object("Old Target Name")
        relationship = self.repository.create_relationship(
            source.object_id,
            "test_fixture_relation",
            target.object_id,
        )

        self.repository.rename_object(source.object_id, "New Source Name")
        self.repository.rename_object(target.object_id, "New Target Name")

        stored = self.repository.list_relationships()[0]
        self.assertEqual(stored.relationship_id, relationship.relationship_id)
        self.assertEqual(stored.from_object_id, source.object_id)
        self.assertEqual(stored.to_object_id, target.object_id)
        self.assertEqual(
            self.resolver.resolve(stored.from_object_id).current_name,
            "New Source Name",
        )
        self.assertEqual(
            self.resolver.resolve(stored.to_object_id).current_name,
            "New Target Name",
        )

    def test_initialization_is_idempotent_and_preserves_existing_objects(self) -> None:
        record = self.repository.create_object("Persistent Synthetic Object")

        self.repository.initialize()
        self.repository.initialize()
        self.repository.close()
        self.repository = SQLiteWorldModelRepository(self.database)
        self.resolver = ObjectResolver(self.repository)

        self.assertEqual(self.repository.list_objects(), (record,))
        self.assertEqual(
            self.resolver.resolve(record.object_id).current_name,
            "Persistent Synthetic Object",
        )

    def test_no_parallel_role_specific_base_models_exist(self) -> None:
        forbidden = ("PersonObject", "ProjectObject", "BusinessLineObject")

        for model_name in forbidden:
            self.assertFalse(hasattr(world_model_models, model_name), model_name)


if __name__ == "__main__":
    unittest.main()
