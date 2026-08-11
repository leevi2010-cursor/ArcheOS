# Atomic Information Agent Rules

## Purpose

This directory contains durable Atomic Information that is eligible for reuse by ArcheOS and for later interpretation against the Structured World Model.

The active M2-B1 implementation is responsible for moving the durable local path from the legacy `03_notes/` name to the canonical Information-layer path. `Note` is no longer a Core concept.

## Rules

- An `Atomic Information` item represents one independently traceable information statement, not a mixed paragraph of unrelated claims.
- Contract-valid Atomic Information Candidates may be automatically ingested as durable Atomic Information; per-item human approval is not required by default.
- Preserve statement, semantic type, source Evidence, context, confidence or uncertainty, source identity, and processing provenance.
- Preserve the original source-level `concerns` text separately from later Object bindings; do not pretend a free-text concern is already an `object_id`.
- Atomic Information revisions are append-only in meaning: later corrections or enrichment must not silently overwrite prior history.
- Exact re-ingestion of the same candidate must be idempotent rather than creating duplicates.
- If the same origin candidate appears with different content, fail or escalate instead of silently treating source mutation as a revision.
- Do not merge conflicting statements into false consensus. Preserve disagreement and provenance.
- Linking or interpreting Atomic Information against Objects must follow `docs/product/INFORMATION_GOVERNANCE.md`.
- Do not create new semantic or Core concepts merely because a statement is difficult to classify; follow `docs/architecture/CONCEPTS.md` and the current Issue.
- Never sever the traceability chain back to the original source.
- Actual Atomic Information data is private local information and must remain ignored by Git unless explicitly sanitized as a fixture.

Atomic Information is durable Information. It is not itself Object state, final judgment, or authorization to change the World Model.
