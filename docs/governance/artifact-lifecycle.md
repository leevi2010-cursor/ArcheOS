# 开发工件生命周期

## 依赖链

`Product Brief -> PRD -> Architecture -> Roadmap -> Milestone -> Issue -> Implementation -> PR -> Release`

只有 `approved` 工件可以解锁下游。

## 状态

- `draft`：编辑中。
- `in_review`：等待指定角色或 Leo 审核。
- `approved`：当前有效，可供下游依赖。
- `stale`：上游版本或摘要变化，必须重新审核。
- `superseded`：已被新版本取代。
- `blocked`：缺少必要上游或授权。

## 版本和摘要

所有权威工件必须在 `governance/manifest.json` 登记版本、路径、状态和 SHA-256。摘要一致时允许摘要优先读取；摘要变化时必须重读变更、完成影响分析并更新下游状态。

## 架构变化

架构语义变化必须新增 ADR，并记录旧/新版本、受影响模块、Roadmap/Milestone/Issue 影响、迁移、回滚和审批状态。

## 能力与执行任务

独立 Codex 任务拥有独立对话上下文，但不构成独立事实源。ArcheOS 由一个长期项目经理上下文协调全链路；产品、架构和交付是按工件阶段调用的责任能力，不要求同名常驻线程。每次切换能力或进入执行前，都必须重新读取 `AGENTS.md`、`governance/manifest.json` 和所依赖的权威工件。manifest 中的 `owner_role` 是能力标签，不是线程存在证明。

- 项目经理是 Leo 的项目日常入口，维护权威入口、影响矩阵、当前状态与跨工件路由；可以按需调用专业能力，但不得自行批准 Sponsor 门禁或直接编码。
- 产品能力负责 Product Brief、PRD、用户旅程、信息架构、低成本可交互原型、Sponsor 走查与产品验收准备；原型只能使用假数据并保持在隔离原型目录，不得被视为正式 Implementation。
- 架构能力仅在 PRD approved 且 `create-architecture` 通过后维护 Architecture 与 ADR，不改写产品价值或流程。
- 交付能力仅在 Architecture approved 且相应门禁通过后维护 Roadmap 与 Milestone，不创建未解锁承诺。
- 实现工程师是唯一独立执行线程，只能依据 ready Issue 修改正式产品实现，并统一向项目经理交付验证证据。
- 常规原型不设置长期独立产品设计线程；只有明显独立高风险或上下文隔离需求才申请临时支持，完成后必须交回入口、原则、证据、未决问题、版本和摘要并归档。
