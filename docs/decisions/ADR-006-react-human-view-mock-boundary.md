# ADR-006：采用独立 React Mock Human View 验证决策工作台

## 状态

In Review — 2026-08-19 — Issue #114

## 背景

ArcheOS 已定义 Human View 为 `Canonical State → Projection / View Model → Presentation` 的读取层，并明确旧 `sunward-operating-system` 只能作为 UI design reference。

Leo 已批准面向企业经营负责人的决策工作台 v0.1，并要求：

- 从资产浏览与左侧推进事项进入；
- 多图少字；
- 资产专属 View 从向阳总图节点打开；
- 人脉使用完整 graph；
- 财务使用三表；
- 公司采用组织与治理图；
- Decision / Evidence / Source 渐进追溯；
- 不接 backend、不部署、不写正式业务数据。

当前没有正式前端 read contract。若直接接 runtime 或复用旧向阳数据模型，会让 Presentation 反向定义 Core。

## Decision

### 1. 先实现隔离的 React mock-data Human View

第一版只验证产品结构、图形表达与桌面交互。mock View Model 位于前端内部，与 ArcheOS runtime、Store、Source 和真实 Workspace 数据隔离。

### 2. React Flow 仅用于 Presentation graph

复用旧向阳 React / React Flow 的交互经验，包括缩放、聚焦、路径高亮、节点详情和图形导航。React Flow node / edge / layout 不成为 Object / Relationship / View Model 的 canonical contract。

### 3. 大型详情使用 route，小信息使用侧栏

理念、财务、人脉、组织与治理等资产世界需要独立可复制 URL；浏览器原生支持新标签页。侧栏只承担不需要完整画布的快速信息。

### 4. 不复制设计 rationale 到最终页面

设计原则、Concept mapping、Roadmap Alignment 与技术边界只进入 docs / Issue。页面使用业务语言，关系由图形承担，文字只保留名称、数值、状态、关系标签与动作。

### 5. 严格保留推进 ownership

```text
Workspace → Roadmap → Milestone → Issue → Todo
Project   → Milestone → Issue → Todo
```

Business Line 的相关推进路径是 View；Project 不拥有 Roadmap。

### 6. Project / Business Line 先收敛 Presentation，不合并 Core

第一版使用同一节点、详情组件和关系交互呈现 Project / Business Line。新增 mock 条目默认使用 `Project + bounded`；只有用户明确指定，或持续经营责任确实无法定义可信完成条件时，才使用 `Business Line + ongoing`。

长期、重复或包含多个 Milestone 不自动构成 ongoing。第一版不为视觉覆盖人为创建 ongoing fixture。该策略用于观察 Business Line 是否产生独立产品行为，不废弃现有 Role，也不改变 Lifecycle 与 ownership。

### 7. backend integration 是后续独立 Decision

v0.1 不建立 backend client。产品验收后，另行批准 canonical read contract、fixture、integration Issue 与 rollback。

## Consequences

- 可以在不影响 Stage 1 数据主线的情况下验证 Human View；
- React 组件、URL 和 mock fixtures 可在后续 read contract 稳定后复用；
- mock 类型不是未来 backend schema 承诺；
- 第一版不能证明真实数据可用、backend readiness、release 或 production activation；
- 人脉与公司治理精确领域关系继续保持未批准，不因 UI 示例进入 Core；
- Project / Business Line 复用 Presentation 组件，但保留 Role、Lifecycle 与 ownership 的语义差异；
- Issue #114 合并前不得创建 Ready implementation Issue。

## Review 要点

- Product alignment：该探索是否保持有限、并行且不冒充 Stage 1 Gate；
- Architecture：mock / Core、View / truth、route / identity 边界是否清楚；
- Concept：是否错误新增 Asset type、Person relation、Roadmap ownership 或 Decision truth；
- Implementation readiness：PRD 验收是否足以创建一个独立 React implementation Issue。
