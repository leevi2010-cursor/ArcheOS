# AGENTS.md — ArcheOS Repository Constitution

本文件适用于所有参与本仓库开发的人工开发者、IDE 与 Coding Agent。

## 1. 开发者角色

开发者是 **ArcheOS Implementation Engineer**，不是产品战略制定者，也不是自由架构师。

职责：

- 按已经批准的架构实现文件、工具、Schema、自动化和测试；
- 在动手前识别变更所属 Layer、Object 和 Domain；
- 保持向后兼容，避免未经批准的大规模重构；
- 将不明确的边界问题记录为 Architecture Decision 或 Issue；
- 提交可审查的小变更，并说明验证结果。

不负责：

- 擅自改变 ArcheOS 的核心定位；
- 用某个开发工具的能力或限制反向定义产品架构；
- 为单个客户创建无法复用的核心抽象；
- 把临时业务事实写成长期规则；
- 用实现便利取代架构边界。

## 2. 产品边界硬规则

- ArcheOS 的产品架构中不得出现特定 Coding Agent、IDE 或开发平台。
- 开发工具属于仓库外部，不是 Control Plane、Execution Plane、Runtime、Agent 或治理模块的一部分。
- 更换开发工具不得影响 ArcheOS 的领域模型、接口或部署形态。
- Hermes 或其他业务 Agent Runtime 可作为可插拔执行面；Coding Agent 不属于业务执行面。

## 3. 每项任务开始前必须回答

1. **Layer**：该变更属于哪一层？
2. **Object**：修改或新增什么核心对象？
3. **Responsibility**：它负责什么？
4. **Non-responsibility**：它明确不负责什么？
5. **Source of truth**：事实、规则、代码分别存放在哪里？
6. **Validation**：如何证明它有效？
7. **Reuse**：它是领域专用，还是跨领域可复用？

无法回答时，先创建或引用 `architecture-question` Issue，不得自行发明边界。

## 4. 当前分层模型

### L0 — External Business Environment
Amazon、展厅、ERP、CRM、银行、客户、员工与市场等外部业务环境。

### L1 — Human Governance
经营哲学、业务宪章、目标、策略、风险偏好、审批与最终裁决。

### L2 — ArcheOS Control & Evolution
业务治理、Policy、Skill 治理、Evaluation、实验、版本、发布、回滚和能力视图。

### L3 — Execution Plane
可插拔 Agent Runtime、业务 Agent、Skills、Tools、Scheduler 与 Runtime Telemetry。

### L4 — Knowledge & Memory
Tolaria、业务上下文、决策历史、案例和长期记忆。

## 5. 工作方式

- 优先修改已有对象，谨慎新增概念；
- 不新增可由现有对象属性或派生视图表达的一级对象；
- 每个 PR 只解决一个主要问题；
- 文档和 Schema 同步更新；
- 新 Policy 必须说明作用域、优先级、冲突处理和例外；
- 新 Skill 必须声明输入、输出、Tool/子 Skill 依赖和 Evaluation；
- 领域需求先在 Domain 层验证，再决定是否上升到 Core；
- 不得宣称“自动进化”，除非存在可审计的反馈、评价、版本、审批与回滚链路。

## 6. Definition of Done

- 边界说明；
- 实现或文档变更；
- 验证方法及结果；
- 影响范围；
- 是否需要迁移现有 Tolaria 内容；
- 是否产生新的 ADR。
