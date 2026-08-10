# ArcheOS Agent Governance

## Purpose
This repository stores both the ArcheOS system design documents and the information assets processed by the system.

Agents must preserve the separation between:

1. System layer: how ArcheOS is designed and built.
2. Information layer: user data absorbed, processed, and structured by ArcheOS.

## Core Principle
Do not create complexity before the system has a proven workflow.

The core lifecycle is:

Input → Processing → Atomic Information → Structured Object → Decision → Feedback

## Rules

### Information handling
- Raw inputs must enter through `01_inbox/`.
- Never directly modify raw source files.
- Every processed item must preserve source, time, and context.

### Atomic information
- Atomic information is the smallest reusable knowledge unit.
- Each atomic note must explain:
  - What happened
  - Who is involved
  - When and where
  - Why it matters
  - Source and confidence

### Core objects
The first version only recognizes:

- Person
- Company
- Project
- Event
- Goal
- Decision
- Note

Do not introduce new object types without architectural review.

### Agent behavior
Agents must:
1. Read relevant Agent.md files before modifying content.
2. Prefer updating existing authoritative documents.
3. Avoid duplicate concepts.
4. Keep business meaning separate from implementation details.

### Naming
Use Markdown as the default knowledge format.
Use clear names: `YYYY-MM-DD_topic.md`.
