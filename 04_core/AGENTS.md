# Structured World Model Agent Rules

## Purpose

`04_core/` contains private local persistence for the long-term Structured World Model.

The canonical model is defined in `docs/architecture/CONCEPTS.md` and uses stable `Object` identity with `Name`, `Role`, `Lifecycle`, and `Relationship` rather than separate Person/Company/Project base models.

## Rules

- Treat `Object` as the stable identity primitive. Do not recreate an Object because its Name or Role changes.
- Do not introduce `PersonObject`, `ProjectObject`, `BusinessLineObject`, or other parallel base entity hierarchies.
- Every material World Model change must remain traceable to supporting Atomic Information / Evidence or an explicit human decision where the product governance requires one.
- Automatic updates and human-review boundaries are defined only by `docs/product/INFORMATION_GOVERNANCE.md`; do not invent local approval rules inside persistence code.
- Prefer updating an existing authoritative Object over creating a duplicate.
- When Object identity or Relationship meaning is uncertain, do not guess.
- Preserve Name, Role, Lifecycle, and Relationship history rather than overwriting prior state.
- Avoid isolated Objects. New-object and deletion behavior must respect the connectivity safeguards in `INFORMATION_GOVERNANCE.md`.
- Persistence adapters expose storage primitives only. Business governance belongs above SQLite/JSONL/other adapters.
- Do not introduce new Core concepts, Roles, or governed Relationship semantics without first updating the canonical concept authority when required.
- Actual World Model data remains private local information and must be ignored by Git unless explicitly sanitized as a fixture.

The Structured World Model is organized long-term state. It must not become a dumping ground for raw transcripts, summaries, or unprocessed extraction output.
