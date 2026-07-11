# ADR-0001: ArcheOS 的范围

- Status: Accepted
- Date: 2026-07-11

## Context

Tolaria 是以 Type 为核心的笔记工具，并可承担 Agent 的长期记忆载体。Hermes 或其他 Agent Runtime 负责承载长期运行的业务 Agent。Amazon、展厅、ERP、CRM 等属于外部业务环境。

项目开发过程中可能使用人工开发者、IDE 或 Coding Agent，但开发工具不应进入产品架构。此前将开发工具与 Runtime、领域 Agent 和治理模块并列，造成了系统边界误判。

## Decision

ArcheOS 被定义为面向业务 Agent 的经营控制、能力治理与持续进化系统。

ArcheOS 包含：

- Human Governance 接口；
- Control & Evolution Plane；
- Knowledge & Memory；
- 可插拔的 Execution Plane 与 Runtime Instances。

ArcheOS 不包含任何特定 Coding Agent、IDE 或开发平台，也不依赖它们。开发工具仅用于修改和维护 ArcheOS 的实现。

## Consequences

- 产品架构图中不得出现具体开发工具。
- Coding Agent 不属于 Execution Plane，也不属于任何 ArcheOS 内部模块。
- 更换开发方式不会影响 ArcheOS 的领域模型、接口和部署结构。
- Hermes 或其他业务 Runtime 通过稳定接口接入执行面。
- Tolaria 保持知识、笔记与长期记忆载体定位。
- 外部业务先在真实环境中验证，反馈进入治理与进化闭环。
