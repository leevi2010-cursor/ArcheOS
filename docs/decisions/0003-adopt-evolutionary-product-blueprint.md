# ADR-0003: 采用稳定对象内核与分阶段产品蓝图

- Status: Superseded
- Date: 2026-07-12
- Architecture from: 0.3.0
- Architecture to: 0.4.0-in-review
- Depends on: AR-PRD@0.5.0 approved
- Superseded by: ADR-0004

> 历史说明：本 ADR 未获最终批准。PRD v0.6.0 改变了 Capability、KPI、Knowledge 和 Tolaria 产品语义，新的责任链与蓝图由 ADR-0004 统一处理。

## 背景

Architecture v0.3 准确表达了 Human Governance、Control & Evolution、Execution、Knowledge 和 External Business 的边界，但其架构图过度压缩，无法展示一个实际产品如何从 Business Cockpit、Agent 创建、执行资产、Runtime、数据工具一直协作到治理与演进。用户已审核的早期 v1.0 图包含更完整的产品分面，但其中 Capability、Workflow、Protocol 和 Evaluation 的对象语义与当前权威边界冲突。

## 决策

1. 保留 `object-boundaries.md` 的核心对象模型作为稳定内核。
2. 引入“产品系统视图”而不是另一套核心分层：体验与业务接入、Agent 管理与执行、受治理资产、Runtime 与集成、证据与学习五个分面。
3. ArcheOS Control Plane 位于右侧并横切所有分面，包含架构与契约、身份安全、资产生命周期、质量评价、运营可观测性、知识治理。
4. Capability 仍为派生视图；Workflow 仍为复合 Skill 内部编排；Protocol/Tutorial 不恢复为核心对象。
5. 使用 v0.1 / 平台化 / 跨领域三个成熟度阶段标注蓝图；未进入当前 PRD 的模块是扩展点，不构成交付承诺。
6. Amazon Ops Kit 作为 Amazon Domain Adapter 和执行资产来源接入，通过来源版本与 digest 追踪，不成为 ArcheOS 核心事实源。
7. Architecture v0.4 保持 `in_review`，直到产品经理处理或明确接受 `blueprint-review.md` 中 P0 产品缺口。

## 被否决的方案

- 继续使用 v0.3 四框图作为唯一架构图：边界正确但遗漏产品模块和契约，无法指导增量交付。
- 原样恢复早期 v1.0 图：会重新引入独立 Capability Registry、一级 Workflow、Protocol/Tutorial 等模型冲突。
- 把未来蓝图全部写入 v0.1 PRD：造成过度范围和虚假承诺。
- 先实现 Business Cockpit 页面再反推后端模块：会让前端信息架构替代权限、资产和生命周期事实源。
- 把 Amazon Ops Kit 复制进 ArcheOS：会制造双事实源、版本漂移和客户事实泄漏风险。

## 受影响模块

- `docs/architecture/system-architecture.md`：主图和演进视图。
- `docs/architecture/blueprint-review.md`：旧图、PRD 与实际资产差距。
- Architecture 附件：详细产品蓝图。
- 后续 Agent Factory、Asset Catalog、Contract、Control Plane、Runtime Adapter 和 Cockpit 信息架构。

## 下游影响

- Product Brief：不修改。
- PRD：v0.5.0 保持 approved；P0/P1 缺口由产品经理决定是否形成 v0.6.0。
- Roadmap：保持 stale，不得依据 v0.4 in-review 架构批准。
- Milestone / Issue / Implementation：保持 blocked。
- Amazon Ops Kit：只读盘点；后续需 ready Issue 才能实现适配。

## 迁移

1. 保留 v0.3 compact control-loop 图作为概念辅助图。
2. 新增详细产品蓝图作为 v0.4 主图，不删除历史图或用户原始输入。
3. 后续 Roadmap 将蓝图节点映射到 v0.1 / 平台化 / 跨领域阶段，不按视觉盒子一一创建服务。
4. 现有 Ops Kit 资产先生成来源与接口清单，再登记为 Skill、Tool、Policy、Measure 或 Runtime Adapter。

## 回滚

若 P0 产品问题未获处理或 v0.4 被否决，恢复 manifest 中 Architecture v0.3.0 approved 的版本与摘要，保留本 ADR 和差距审查作为未采纳提案。回滚不修改 Product Brief、PRD、Roadmap 或 Ops Kit。

## 开放风险

- 产品经理尚未决定领域资产生命周期、异常通知、证据保留、人工接管和 Ops Kit 产品边界。
- 蓝图展示的是逻辑模块，不等于每个模块都应成为独立服务。
- M1/M2 的多 Workspace、组织角色、成本与商业化范围尚未产品批准。

## 审批

当前为 `in_review`。技术负责人完成架构自检后交项目经理，由项目经理协调产品经理处理 P0 缺口并安排 Leo 审核；不得自行推进 Roadmap。
