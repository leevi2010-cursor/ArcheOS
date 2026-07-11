# ADR-0001: ArcheOS 的范围

- Status: Proposed
- Date: 2026-07-11

## Context

Tolaria 是以 Type 为核心的笔记工具，被用作 Agent 长期记忆载体。Codex、Hermes 与多个领域 Agent 分别承担构建、运行和业务执行职责。此前这些角色容易被混为一个“大系统”。

## Decision

ArcheOS 被定义为 Capability Operating System 的架构与治理层，不替代 Tolaria、Codex、Hermes 或领域 Agent。

## Consequences

- Tolaria 保持笔记与记忆载体定位。
- 领域 Agent 不直接定义系统级架构。
- 核心抽象需要进入本仓库并接受版本治理。
- 真实业务先在 Domain 层验证，再决定是否上升到 Core。
