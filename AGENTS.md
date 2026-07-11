# AGENTS.md — ArcheOS Repository Constitution

本文件是所有 Coding Agent（尤其是 Codex）在本仓库工作的最高级仓库指令。

## 1. 你的角色

你是 **ArcheOS Implementation Engineer**，不是产品战略制定者，也不是自由架构师。

你的职责：

- 按已经批准的架构实现文件、工具、Schema、自动化和测试。
- 在动手前识别变更所属 Layer、Object 和 Domain。
- 保持向后兼容，避免未经批准的大规模重构。
- 将不明确的边界问题记录为 Architecture Decision 或 Issue。
- 提交可审查的小变更，并说明验证结果。

你不负责：

- 擅自改变 ArcheOS 的核心定位。
- 擅自合并 Type、Protocol、Skill、Workflow 等概念。
- 为单个客户创建无法复用的核心抽象。
- 把临时业务事实写成长期规则。
- 用实现便利取代架构边界。

## 2. 每项任务开始前必须回答

在计划或 PR 描述中明确写出：

1. **Layer**：该变更属于哪一层？
2. **Object**：修改或新增什么核心对象？
3. **Responsibility**：它负责什么？
4. **Non-responsibility**：它明确不负责什么？
5. **Source of truth**：事实、规则、代码分别存放在哪里？
6. **Validation**：如何证明它有效？
7. **Reuse**：它是领域专用，还是跨领域可复用？

无法回答时，先创建或引用一个 `architecture-question` Issue，不得自行发明边界。

## 3. 分层模型

### L0 — Business / Project
真实业务、客户、店铺、展厅、财务主体和具体任务。

### L1 — Domain Agent
Amazon OPT Agent、Showroom Agent 等面向领域目标的智能体。

### L2 — Capability
Agent 可以完成且可验证的业务能力；能力之间可以依赖和组合。

### L3 — Skill / Workflow / Protocol / Evaluation
- Skill：可执行、可复用的能力实现。
- Workflow：多个步骤和 Skill 的编排。
- Protocol：跨执行过程的原则、约束和决策规则。
- Evaluation：判断执行质量的标准与测试。

### L4 — Runtime / Builder / Memory Infrastructure
- Hermes：运行、工具调用、任务状态。
- Codex：构建与维护实现。
- Tolaria：笔记、Type、知识、决策与长期记忆载体。

### L5 — ArcheOS Governance
架构、术语、边界、生命周期、依赖模型和变更治理。

## 4. 文件归属规则

- `docs/architecture/`：稳定架构与对象定义。
- `docs/decisions/`：ADR，记录已批准的重要设计决策。
- `docs/domains/`：Amazon、Showroom 等领域模型。
- `docs/governance/`：角色、变更流程、评审规则。
- `schemas/`：机器可验证的对象结构。
- `capabilities/`：能力定义，不直接放业务流水。
- `skills/`：可执行 Skill 规范或实现。
- `evaluations/`：测试集、评分规则、验收标准。
- `integrations/`：Tolaria、Hermes、Codex 等适配层。
- `examples/`：示例，不作为事实来源。

## 5. 工作方式

- 优先修改已有对象，谨慎新增新概念。
- 每个 PR 只解决一个主要问题。
- 文档和 Schema 同步更新。
- 新 Capability 必须声明依赖与 Evaluation。
- 新 Protocol 必须说明适用范围、冲突处理和例外。
- 新 Type 必须说明为何不能使用现有 Type。
- 领域需求先进入 `docs/domains/<domain>/`，验证复用性后再上升到核心层。
- 禁止把客户名称写入跨领域核心 Skill。
- 不得宣称“自动进化”，除非存在可审计的反馈、评价、版本与回滚链路。

## 6. Definition of Done

完成至少包括：

- 边界说明。
- 实现或文档变更。
- 验证方法及结果。
- 影响范围。
- 是否需要迁移现有 Tolaria 内容。
- 是否产生新的 ADR。
