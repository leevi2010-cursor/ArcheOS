# ArcheOS（元枢）

> **项目状态：Archived（2026-07-16）**
>
> ArcheOS 已停止 PRD v0.7.0 审批及全部下游建设，当前只作为可恢复的历史与候选概念资产保留。它不是现行生产依赖；Amazon Ops Kit、开放信息系统、Codex、Hermes 和领域业务系统均不依赖本仓库运行。代码、文档、Schema、原型和 Git 历史均未删除。

## 归档快照

归档状态与精确摘要以 [`governance/manifest.json`](governance/manifest.json) 为准。历史工件保持归档前的真实生命周期状态，不因项目归档而伪造批准或完成：

| 工件 | 归档快照 |
| --- | --- |
| Product Brief | v0.4.0 · `approved` |
| PRD | v0.7.0 · `in_review` |
| Architecture | v0.5.0 · `stale` |
| Roadmap | v0.3.0 · `stale` |
| UX Prototype | v0.2.2-prototype · `stale`；Leo walkthrough/approval 仍为 pending |
| Milestone / Issue / Implementation | 未解锁 / blocked |

公开 GitHub Pages 若仍可访问，只是历史交互原型，不代表生产服务或获批产品；仓库内 Pages workflow 已改为仅可人工触发。

## 恢复条件

满足以下任一条件时，只触发一次新的恢复评估，不自动恢复旧路线：

1. 两个以上领域 Agent 出现重复的部署、权限、版本或审计需求；
2. 需要向第二个真实客户或独立环境复制；
3. 现有 Codex、OIOS 或领域系统无法以更轻量方式解决；
4. 出现经过验证的统一平台付费需求。

恢复时必须由 Sponsor 形成新的 Decision，重新核验当前 Product Brief、PRD、Architecture、Roadmap 和外部依赖，再显式更新 manifest 的项目状态。历史批准、候选状态和摘要不得自动沿用为当前批准。

## 历史设计摘要

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
