# Core Object Agent Rules

## Purpose

`04_core/` contains stable structured object state assembled from validated information.

## Current object directories

- `projects/`
- `persons/`
- `companies/`
- `events/`
- `goals/`
- `decisions/`

## Rules

- Only absorb information that has crossed the required validation boundary.
- Every material object update must remain traceable to supporting validated information and, through it, to source evidence.
- Prefer updating an existing authoritative object over creating a duplicate.
- When identity or object matching is uncertain, leave it unresolved instead of guessing.
- Do not overwrite history in a way that hides how an object's state changed over time.
- Do not introduce new core object types without an explicit architecture decision.
- `Roadmap`, `Issue`, `Task`, `Asset`, and `Knowledge` must not silently become core object types merely because they are useful document or workflow concepts.
- Follow the current GitHub issue for any approved absorption or object-update behavior.

Core objects represent organized state; they must not become dumping grounds for raw transcripts, summaries, or unvalidated extraction output.
