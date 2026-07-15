# AGENTS.md — ArcheOS Repository Constitution

本文件适用于所有参与本仓库开发的贡献者，是本仓库的最高级开发指令。

## 0. 强制开发门禁

本仓库采用全局 `governed-vibe-coding` 协议。任何产品、架构、规划或实现动作开始前，必须：

1. 读取 `governance/manifest.json`；
2. 识别本次动作所依赖的上游工件；
3. 运行 `./scripts/governance-check <action>`；
4. 仅在退出码为 0 时继续；
5. 门禁失败时停止下游工作，只补齐最小必要上游。

依赖链固定为：

`Product Brief -> PRD -> Architecture -> Roadmap -> Milestone -> Issue -> Implementation -> PR -> Release`

只有 `approved` 状态能解锁下游。不得用聊天记忆代替版本、SHA-256 摘要或审批状态。上游摘要变化时，必须重读变化内容、记录影响，并把受影响下游标为 `stale`。

组织模型：Leo 是 Sponsor；笃善科技 CEO 只负责组织治理、跨项目冲突和升级；“元枢 ArcheOS 项目经理”是唯一长期项目上下文负责人和 Leo 的项目日常入口。项目经理在同一线程中依据 Skill、已批准上游工件和治理命令按需调用产品、架构与交付能力，不为这些能力保留常驻专业线程，也不得自行批准任何门禁工件或替代实现工程师编码。只有出现明显独立高风险或上下文隔离需求时，才向 CEO 申请临时专业支持。

能力边界：产品能力维护 Product Brief、PRD、用户旅程、信息架构、低成本可交互原型、Sponsor 走查与产品验收准备；架构能力在 PRD approved 后维护 Architecture/ADR；交付能力在 Architecture approved 后维护 Roadmap/Milestone。实现工程师是唯一独立执行线程，只能依据 ready Issue 工作。原型仅可在隔离目录使用假数据，不构成正式 Implementation，不得触碰后端/API、生产权限或运行系统。manifest 中的 `owner_role` 表示工件责任能力，不要求存在同名常驻线程。

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
- 为单个客户创建无法复用的核心抽象；
- 把临时业务事实写成长期规则；
- 用实现便利取代架构边界。

## 2. 每项任务开始前必须回答

1. **Layer**：该变更属于哪一层？
2. **Object**：修改或新增什么核心对象？
3. **Responsibility**：它负责什么？
4. **Non-responsibility**：它明确不负责什么？
5. **Source of truth**：事实、规则、代码分别存放在哪里？
6. **Validation**：如何证明它有效？
7. **Reuse**：它是领域专用，还是跨领域可复用？

无法回答时，先创建或引用 `architecture-question` Issue，不得自行发明边界。

## 3. 当前分层模型

### L0 — External Business Environment
Amazon、展厅、ERP、CRM、银行、客户、员工与市场等外部业务环境。

### L1 — Human Governance
经营哲学、业务宪章、目标、策略、风险偏好、审批与最终裁决。

### L2 — ArcheOS Control & Evolution
业务治理、Capability Package、Policy、Knowledge 治理、基于 Measure 的 Evaluation、Change、版本、发布和回滚。

### L3 — Execution Plane
可插拔 Agent Runtime、业务 Agent、Skills、Tools、Scheduler 与 Runtime Telemetry。

### L4 — Knowledge Assets
系统核心知识、可分发领域知识、企业私有知识、运行证据/学习候选，以及可替换的知识载体适配。

## 4. 架构事实源

- `docs/architecture/object-boundaries.md`：核心对象和概念边界的权威事实源；
- `docs/architecture/system-architecture.md`：模块、反馈闭环和 Source of Truth；
- `docs/decisions/`：架构语义变更与影响记录；
- `governance/manifest.json`：版本、摘要、依赖和审批状态。

发生冲突时，必须停止下游开发；由项目经理记录影响，并在上游门禁允许后调用架构能力通过 Architecture/ADR 解决。不得选择对实现最方便的版本。

## 5. 工作方式

- 优先修改已有对象，谨慎新增概念；
- 不新增可由现有对象属性或派生视图表达的一级对象；
- Capability 是可独立承诺和验收业务结果的一级对象；Capability Package 是该对象的版本化发布制品，不建立重复实体；
- Agent Deployment 对 Workspace KPI 负责，Capability 对结果契约负责，Skill 对执行方法负责；
- Evaluation 是基于 Measure 的评价过程，不是并列核心对象；
- Policy 是正式治理对象；旧 Protocol 内容必须先分类，再迁移为 Policy、Skill、知识或流程；
- 每个 PR 只解决一个主要问题；
- 文档和 Schema 同步更新；
- 新 Policy 必须说明作用域、优先级、冲突处理和例外；
- 新 Capability 必须声明业务结果契约、适用范围、依赖、Measure、验收、失败影响和回滚目标；
- 新 Skill 必须声明输入、输出、Tool/子 Skill 依赖和 Evaluation；
- Knowledge 必须声明分类、所有者、作用域、版本、权限、来源和分发规则；Tolaria 仅可作为可替换 Markdown 载体接入；
- 领域需求先在 Domain 层验证，再决定是否上升到 Core；
- 不得宣称“自动进化”，除非存在可审计的反馈、评价、版本、审批与回滚链路。

## 6. Definition of Done

- 边界说明；
- 实现或文档变更；
- 验证方法及结果；
- 影响范围；
- 是否需要迁移现有 Tolaria 内容；
- 是否产生新的 ADR；
- `governance/manifest.json` 已同步版本、摘要、依赖和状态；
- `./scripts/governance-check audit` 通过；若未通过，报告明确阻塞项且不得声称完成。
