# ADR-002: Use Codex app-server as the Analysis Runtime

## Status

Accepted

## Context

ArcheOS M1 requires semantic analysis of processed information. The system should not embed a specific model implementation into its core information lifecycle.

The first implementation originally used deterministic rules after transcription. This does not satisfy the goal of transforming unstructured information into meaningful atomic information, context summaries, and residue.

ArcheOS needs an analysis runtime that can provide reasoning capability while keeping the Core architecture independent from any single model provider.

## Decision

ArcheOS will use a provider-based analysis architecture.

The first production implementation of `AnalysisProvider` will use the local Codex app-server runtime.

The architecture boundary is:

```
ArcheOS Core
    |
    ↓
AnalysisProvider Interface
    |
    ↓
Codex App Server Provider
```

ArcheOS Core does not directly depend on Codex implementation details.

## Responsibilities

### ArcheOS Core

Responsible for:

- information lifecycle;
- input/output contracts;
- traceability;
- processing artifacts;
- human review boundaries.

Not responsible for:

- model authentication;
- token management;
- runtime scheduling;
- model retry strategy;
- model-specific output repair.

### Codex Runtime

Responsible for:

- model execution;
- ChatGPT authentication state;
- structured analysis generation;
- runtime-level failures.

## Analysis Pipeline

```
Audio
 ↓
Transcription Provider
 ↓
Speaker Provider
 ↓
Analysis Provider
 ↓
Meeting Summary
Atomic Notes
Residue
 ↓
Human Review
```

## Speaker Handling

M1 only supports speaker attribution with neutral labels:

- Speaker_1
- Speaker_2

Automatic identity matching is not implemented.

Future voice profile and Person matching capabilities require separate architecture decisions.

## Alternatives Considered

### OpenAI API directly from ArcheOS

Rejected for M1 because it couples the Core system to API credentials and provider implementation details.

### codex exec

Rejected as the primary runtime because ArcheOS requires a persistent agent-oriented runtime boundary in future evolution.

### Local model directly embedded in ArcheOS

Deferred. It may become another AnalysisProvider implementation later.

## Consequences

Benefits:

- clean separation between information lifecycle and intelligence runtime;
- future support for multiple analysis providers;
- compatible with local Codex workflows.

Costs:

- requires local Codex runtime availability;
- requires clear provider contracts;
- introduces another runtime dependency.
