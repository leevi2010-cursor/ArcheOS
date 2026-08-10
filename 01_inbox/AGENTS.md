# Inbox Agent Rules

## Purpose

`01_inbox/` stores original, unprocessed information inputs.

## Rules

- Accept raw inputs such as audio, images, PDF, PPT, documents, video, and external captures.
- Treat every raw source as immutable: do not edit, rename, overwrite, move, summarize in place, or delete it during processing.
- Do not add conclusions, business classifications, or structured-object updates to this directory.
- Preserve enough source identity for downstream traceability.
- Actual user inputs must remain local and ignored by Git unless the product owner explicitly approves a sanitized fixture.
- Keep this `AGENTS.md` tracked even when directory contents are ignored.

Processing outputs belong in `02_processing/` and must follow `02_processing/AGENTS.md`.
