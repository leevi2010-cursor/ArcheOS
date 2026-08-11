# Note Agent Rules

## Purpose

`03_notes/` contains durable atomic Information that is eligible for reuse by ArcheOS and for later interpretation against the Structured World Model.

## Rules

- A `Note` represents one independently traceable information statement, not a mixed paragraph of unrelated claims.
- Contract-valid Atomic Information Candidates may be automatically ingested as durable Notes; per-note human approval is not required by default.
- Preserve statement, semantic type, source Evidence, context, confidence or uncertainty, source identity, and processing provenance.
- Preserve the original source-level `concerns` text separately from later Object bindings; do not pretend a free-text concern is already an `object_id`.
- Note revisions are append-only in meaning: later corrections or enrichment must not silently overwrite prior history.
- Exact re-ingestion of the same candidate must be idempotent rather than creating duplicate Notes.
- If the same origin candidate appears with different content, fail or escalate instead of silently treating source mutation as a Note revision.
- Do not merge conflicting statements into false consensus. Preserve disagreement and provenance.
- Linking or interpreting a Note against Objects must follow `docs/product/INFORMATION_GOVERNANCE.md`.
- Do not create new semantic or Core concepts merely because a statement is difficult to classify; follow `docs/architecture/CONCEPTS.md` and the current Issue.
- Never sever the traceability chain back to the original source.
- Actual Note data is private local information and must remain ignored by Git unless explicitly sanitized as a fixture.

Notes are durable Information. They are not themselves Object state, final judgment, or authorization to change the World Model.
