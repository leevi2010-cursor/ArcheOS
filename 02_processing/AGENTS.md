# Processing Agent Rules

## Purpose

`02_processing/` contains derived, reviewable artifacts produced from immutable inputs. It is the processing layer between raw sources and the long-term Information layer.

## Rules

- Always preserve traceability to the raw source in `01_inbox/`.
- Processing may transcribe, summarize, extract, classify, and preserve context.
- Atomic Information Candidates remain processing-stage artifacts until they satisfy the downstream information contract. A per-item human review is not inherently required.
- Contract-valid Atomic Information may be automatically ingested into durable `Note` records when the current approved workflow implements that path.
- Processing must not directly mutate the Structured World Model. Object/Name/Role/Relationship/Lifecycle changes follow `docs/product/INFORMATION_GOVERNANCE.md`.
- Do not write directly to downstream directories unless the current Issue explicitly implements the corresponding approved ingestion workflow.
- Never silently discard information that cannot be safely interpreted or atomized. Preserve it as Residue with source evidence, the reason it was not absorbed, and uncertainty or possible future value.
- Context-preserving artifacts such as meeting summaries complement Atomic Information; they do not replace Evidence.
- Do not introduce domain-specific logic such as sales, brand, or project analysis into the Core processing pipeline unless the Issue explicitly defines that domain layer.
- Follow the current GitHub Issue for the exact package schema, supported media types, and acceptance criteria.
- Actual generated outputs containing user or business data remain local and ignored by Git unless explicitly sanitized and approved as fixtures.

A healthy processing pipeline makes both successful extraction and Residue visible while keeping raw source, Information, and World Model responsibilities separate.
