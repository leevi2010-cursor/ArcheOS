# ADR-003 — Use stable Object identity with mutable Roles and human Views

- Status: Accepted
- Date: 2026-08-11

## Context

ArcheOS is entering M2, where confirmed Atomic Information must begin to participate in a durable structured world model.

Real operating scenarios show that business nouns are not stable enough to be used as object identity. For example:

- “展厅经营” may initially be described as a project, but is more accurately a long-running business line without a fixed end date;
- “海丝金融中心家具采购” is a bounded project with an expected completion condition;
- one real-world referent may simultaneously be understood as a company and a brand;
- names and classifications may change while accumulated Notes, Relationships and history must continue referring to the same thing;
- human presentation may prefer a tree, timeline or profile even though the underlying world model is a graph.

If `Project`, `BusinessLine`, `Company`, `Person`, `Goal`, `Decision`, etc. are implemented as mutually exclusive storage identities, later terminology changes will cause migrations, duplicated objects and divergence between documents and code.

## Decision

ArcheOS adopts the following durable model:

1. **Object is the stable identity abstraction.**
   - Each Object has an immutable `object_id`.
   - Names, roles and presentation structures do not define identity.

2. **Business classifications are Roles, not separate base object types.**
   - Current accepted Roles include `person`, `company`, `brand`, `project`, `business_line`, `event`, `goal`, and `decision`.
   - One Object may hold multiple Roles.
   - Role assignments may have history and time boundaries.

3. **Lifecycle is separate from Role.**
   - A bounded project and an ongoing business line are represented through Role plus lifecycle properties rather than separate storage systems.

4. **Relationship forms the durable object graph.**
   - Core does not require all Objects to belong to one tree.
   - Typed Relationships connect Objects and preserve source and uncertainty where applicable.

5. **Note remains in the Information layer.**
   - A confirmed Note is durable atomic information, not an Object and not an Object Role.
   - Notes can support changes to Roles, Relationships and Lifecycle while preserving their own Evidence.

6. **View is a human-facing projection, not Core truth.**
   - Core stores a graph.
   - Tree, timeline, relationship graph, object profile, decision view and similar structures are projections over the same Core data.
   - HTML or other renderers are presentation outputs, not authoritative storage.

7. **Internal references use IDs; humans see resolved names.**
   - Object references use `object_id`.
   - Presentation resolves IDs to current names, roles, aliases and status through an Object Resolver/read model.

8. **Canonical concept definitions live in `docs/architecture/CONCEPTS.md`.**
   - Future architecture and implementation must reuse those concepts where possible.
   - A new durable concept requires updating that document and explaining why existing concepts are insufficient.

## Consequences

### Positive

- Renaming an Object does not break references.
- Reclassifying a project as a business line does not require creating a new identity.
- Different human Views can coexist without duplicating Core data.
- The model can evolve without prematurely expanding a rigid ontology.
- Codex and other agents have a stable vocabulary to use when implementing later Issues.

### Costs

- Read paths need ID resolution before presentation.
- Role and name history require explicit modeling.
- Some business concepts that look like “types” in UI must be treated as Roles in Core.
- Tree rendering requires a projection rule rather than simply traversing a single parent field.

## Non-decisions

This ADR does not yet define:

- the physical database/storage engine;
- exact ID format;
- the complete controlled vocabulary of Relationship types;
- the human confirmation workflow;
- the exact schema for RoleAssignment or Lifecycle;
- the first production UI implementation.

Those are designed in subsequent M2 Issues while remaining consistent with this decision.
