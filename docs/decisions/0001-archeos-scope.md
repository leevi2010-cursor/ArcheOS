# ADR-0001: ArcheOS 的范围

- Status: Accepted
- Date: 2026-07-11

## Context

Tolaria 是以 Type 为核心的笔记工具，并可承担 Agent 的长期记忆载体。Hermes 或其他 Agent Runtime 负责承载长期运行的业务 Agent。Amazon、展厅、ERP、CRM 等属于外部业务环境。

## Decision

ArcheOS 被定义为面向业务 Agent 的经营控制、能力治理与持续进化系统。

ArcheOS 包含：

- Human Governance 接口；
- Control & Evolution Plane；
- Knowledge & Memory；
- 可插拔的 Execution Plane 与 Runtime Instances。

## Consequences

- Hermes 或其他业务 Runtime 通过稳定接口接入执行面。
- Tolaria 保持知识、笔记与长期记忆载体定位。
- 外部业务先在真实环境中验证，反馈进入治理与进化闭环。
- 业务目标、Policy、Skill、Evaluation、版本与回滚构成持续治理对象。
