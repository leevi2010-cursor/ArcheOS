# ArcheOS（元枢）

> A business-agent control and evolution system.

ArcheOS 是一个面向业务 Agent 的经营控制、能力治理与持续进化系统。

它自身关注：

- 人类经营意图、业务宪章与策略；
- Agent 的业务表现与运行反馈；
- Skill、Policy、Evaluation 的治理与演化；
- Runtime 实例的接入、观察、发布与回滚；
- Tolaria 中的业务上下文、决策与长期记忆。

## 系统边界

- Hermes 或其他 Agent Runtime 可以作为 ArcheOS 的可插拔执行面实例。
- Tolaria 是笔记与结构化记忆载体，以 Type 为核心。
- Amazon、展厅、ERP、CRM 等属于 ArcheOS 外部的业务环境。

## 核心闭环

```text
人类经营意图
    ↓
业务宪章 / Strategy / Policy
    ↓
Agent + Skill 在 Runtime 中执行
    ↓
业务结果 + Runtime Telemetry + 人类反馈
    ↓
Evaluation 与问题诊断
    ↓
形成变更提案、测试、发布或回滚
    ↓
Agent 能力持续演化
```

## 北极星问题

> Agent 是否在遵循企业经营方向的前提下，持续产生可验证的业务价值？

## 快速入口

- [系统架构](docs/architecture/system-architecture.md)
- [对象边界](docs/architecture/object-boundaries.md)
- [首席架构师章程](docs/governance/chief-architect-charter.md)
- [路线图](docs/project/roadmap.md)
- [现有内容归类规则](docs/migration/classification-guide.md)
