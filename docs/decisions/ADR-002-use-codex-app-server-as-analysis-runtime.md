# ADR-002: Use Codex app-server as the Analysis Runtime

## Status

Accepted

## Context

ArcheOS M1 requires semantic analysis of processed information while keeping the Core lifecycle independent from any single model implementation. M1 also needs neutral speaker attribution so semantic evidence can preserve who said what without prematurely binding a voice to a real `Person`.

## Decision

ArcheOS uses provider boundaries for transcription, speaker attribution, and semantic analysis:

```text
Audio
 ↓
TranscriptionProvider
 ↓
SpeakerProvider
 ↓
AnalysisProvider
 ↓
Meeting Summary + Atomic Notes + Residue
 ↓
Human Review
```

### Analysis runtime

The first production `AnalysisProvider` implementation uses the local Codex app-server runtime through the **official `openai-codex` Python SDK**.

M1 pins:

- `openai-codex==0.144.4`.

The boundary is:

```text
ArcheOS Core
    ↓
AnalysisProvider
    ↓
CodexAnalysisProvider
    ↓
official openai-codex Python SDK
    ↓
Codex app-server runtime
```

ArcheOS must not maintain a hand-written Codex JSON-RPC/app-server protocol client when the official SDK provides the required surface.

All `openai_codex` imports and SDK-specific types remain isolated inside the Codex provider adapter. ArcheOS Core remains SDK-agnostic.

The Codex provider uses a deny-all approval policy, read-only sandbox, isolated temporary working directory, ephemeral thread, and ArcheOS-owned structured output schema. ArcheOS does not manage Codex authentication tokens, login flows, model retry strategy, or model-specific output repair.

### M1 speaker diarization

Automatic local neutral speaker diarization is part of M1.

The first production `SpeakerProvider` implementation uses:

- `pyannote.audio==4.0.7`;
- `pyannote/speaker-diarization-community-1`.

Use exclusive diarization output when practical for transcript alignment.

Backend speaker labels are normalized to neutral ArcheOS labels:

- `Speaker_1`
- `Speaker_2`
- `Speaker_3`

Labels are ordered by first chronological appearance. They are not identities and must not be interpreted as `Person` objects.

Speaker-to-transcript alignment is conservative:

- use positive timestamp overlap;
- assign a speaker only when one speaker clearly dominates the segment;
- do not resolve ambiguous ties through speaker number, first appearance, or another arbitrary heuristic;
- preserve speaker as unknown/`None` when attribution is unsafe;
- if a transcript lacks usable timestamps and safe automatic alignment cannot be performed, fail with an actionable message or require a supplied speaker map.

M1 does **not** implement:

- voice embeddings;
- voiceprint storage;
- automatic real-person identification;
- Person matching or binding.

`FileSpeakerProvider` remains supported for deterministic tests, imported diarization, and manual correction.

### Privacy

ArcheOS defaults the pyannote execution path to:

```text
PYANNOTE_METRICS_ENABLED=0
```

ArcheOS does not store, print, copy, or commit Hugging Face access tokens. Model-download authentication remains local runtime configuration.

## Responsibilities

### ArcheOS Core

Responsible for:

- information lifecycle;
- provider contracts;
- source traceability;
- processing artifacts;
- semantic output contracts;
- human-review boundaries.

Not responsible for:

- Codex authentication/token management;
- Codex runtime retry strategy;
- model-specific output repair;
- Hugging Face credential storage;
- speaker identity inference.

### Provider runtimes

Responsible for their own runtime execution behind the provider boundary.

Runtime/configuration failures are processing failures, not residue, and must not be converted into information assets.

## Consequences

Benefits:

- Core remains independent from Codex and pyannote implementation details;
- official Codex SDK absorbs app-server protocol changes;
- speaker attribution becomes available in evidence without prematurely introducing identity models;
- future analysis or speaker providers can be added without changing the canonical lifecycle.

Costs:

- M1 depends on local Codex runtime availability;
- M1 speaker diarization depends on pyannote model availability and local model setup;
- provider adapters require version-pinned compatibility tests.
