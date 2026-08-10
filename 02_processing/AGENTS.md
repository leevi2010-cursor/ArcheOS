# Processing Agent Rules

## Purpose

`02_processing/` contains derived, reviewable artifacts produced from raw inputs. This directory is the digestion layer between immutable sources and validated information assets.

## Rules

- Always preserve traceability to the raw source in `01_inbox/`.
- Processing may transcribe, summarize, extract, classify, and preserve context, but outputs remain proposed until the relevant human-validation boundary is satisfied.
- Do not write directly to `03_notes/`, `04_core/`, or `05_decisions/` unless the current issue explicitly implements an approved absorption workflow.
- Never silently discard information that cannot be safely interpreted or atomized. Preserve it as residue with the original excerpt, the reason it was not absorbed, and uncertainty or possible future value.
- Context-preserving artifacts such as meeting summaries complement atomic notes; they are not substitutes for source evidence.
- Do not introduce domain-specific logic (for example sales, brand, or project analysis) into the Core processing pipeline unless the issue explicitly defines that domain layer.
- Follow the current GitHub issue for the exact processing package, schemas, supported media types, and acceptance criteria.
- Actual generated outputs containing user or business data must remain local and ignored by Git unless explicitly sanitized and approved as test fixtures.

A healthy processing pipeline should make both successful absorption and residue visible so humans can diagnose information loss or extraction weaknesses.
