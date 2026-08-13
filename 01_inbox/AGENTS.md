# Inbox Agent Rules

## Purpose

`01_inbox/` stores original, unprocessed information inputs.

In the first Managed Source architecture, `01_inbox/` is the local controlled
root for Source bytes after explicit user admission and successful
size/content-hash verification. It is not a general-purpose scan destination:
an external intake candidate must not be copied here before admission or used
to create a durable `source_id`.

## Rules

- Accept raw inputs such as audio, images, PDF, PPT, documents, video, and external captures.
- Treat every raw source as immutable: do not edit, rename, overwrite, move, summarize in place, or delete it during processing.
- Do not add conclusions, business classifications, or structured-object updates to this directory.
- Preserve enough intake provenance for downstream traceability, but do not use an external path or filename as the durable Source identity.
- A formal Managed Source is created only after explicit user admission, complete byte copy, and matching size/content hash verification.
- One `source_id` identifies one immutable Managed Source byte snapshot. Once its bytes are referenced by Evidence, do not overwrite them in place; a new byte snapshot requires explicit re-ingestion and a new `source_id`.
- After admission, downstream Processing and Evidence use the Managed Source bytes. Changes to an external old file are not automatically tracked or synchronized.
- Keep `ingested_from` as optional, potentially stale intake provenance. It is not an Evidence locator or a second Source.
- Actual user inputs must remain local and ignored by Git unless the product owner explicitly approves a sanitized fixture.
- Keep this `AGENTS.md` tracked even when directory contents are ignored.

Processing outputs belong in `02_processing/` and must follow `02_processing/AGENTS.md`.
