# ArcheOS（元枢）

> A Capability Operating System for building, governing, evaluating, and evolving domain agents.

ArcheOS 不是 Tolaria 的替代品，也不是单个 Agent。

它是一套位于多个工具与业务之间的**能力工程架构与治理系统**：

- **Tolaria**：笔记与结构化记忆载体，以 Type 为核心。
- **Codex**：工程实现者与仓库维护者。
- **Hermes**：Agent 运行时与任务执行环境。
- **Domain Agents**：面向具体业务的智能体，例如 Amazon OPT Agent、Showroom Agent。
- **ArcheOS**：定义上述对象之间的层级、边界、能力依赖、评价方法与进化闭环。

## 北极星问题

> 每一项新内容属于哪一层、负责什么、不负责什么、如何被验证、最终沉淀到哪里？

## v0.1 目标

1. 建立稳定的分层架构和术语。
2. 盘点 Tolaria、Amazon OPT Agent、协议、Type、Tutorial、Skill 等现有资产。
3. 定义 Capability、Skill、Protocol、Workflow、Evaluation 的边界。
4. 配置 Codex，使其依据架构工作，而不是自由发散。
5. 以 Amazon 店铺和黄鸿儒展厅为两个真实验证场景。
6. 建立“业务事实 → Issue → 抽象 → 能力 → 实现 → 评估 → 记忆”的闭环。

## 快速入口

- [系统架构](docs/architecture/system-architecture.md)
- [对象边界](docs/architecture/object-boundaries.md)
- [Codex 角色](docs/governance/codex-role.md)
- [首席架构师章程](docs/governance/chief-architect-charter.md)
- [路线图](docs/project/roadmap.md)
- [现有内容归类规则](docs/migration/classification-guide.md)
