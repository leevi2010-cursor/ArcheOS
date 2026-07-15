# ADR-0004: Capability、KPI 与 Knowledge 责任模型

- Status: approved
- Date: 2026-07-12
- Architecture from: 0.4.0-in-review stale
- Architecture to: 0.5.0
- Previous approved rollback baseline: 0.3.0
- Depends on: AR-PRD@0.6.0 approved
- Supersedes: ADR-0002 的 Capability 派生视图和 Knowledge/Tolaria 语义；ADR-0003 全部候选蓝图语义

## 背景

Architecture v0.3/0.4 把 Capability 降为由 Skill、Tool、权限、Policy 和证据计算的投影视图，并把 Tolaria 画入 Knowledge & Memory 模块。后续产品讨论和已批准 PRD v0.6.0 明确了不同语义：Capability Package 是可独立承诺、调用和验收业务结果的一级资产；Agent Deployment 对 KPI 负责；Knowledge 需要四分类治理；Tolaria 只是可替换 Markdown 管理载体。

旧蓝图还使用五个产品分面，容易被误读为新的系统层级，并弱化了从 KPI 到业务结果的单向责任链。

## 决策

### 1. Capability 与 Package

Capability 与 Capability Package 不建立两个核心实体：

- Capability 是稳定的业务语义和身份；
- Capability Package 是该对象的不可变、版本化发布制品；
- 两者共享 `capability_id`、版本谱系、生命周期、依赖和审计记录。

Capability 必须具备可独立承诺和验收的业务结果契约。纯技术步骤、数据质量检查和中间算法默认属于 Skill、Tool、Policy、Measure 或质量门禁。

### 2. 责任链

固定主链：

`Business Intent/KPI → Agent Deployment → Capability Package → Skill → Tool/External System → Business Result`

- Agent Deployment 对具体 Workspace KPI 负责；
- Capability 对结果契约负责；
- Skill 对执行方法和局部正确性负责；
- Tool 对调用契约负责；External System 提供外部事实和响应；
- Capability 局部达标不得替代 Agent KPI 判断。

v0.1 由 Agent Deployment 组合 Capability；Capability 组合 Skill，不嵌套 Capability。

### 3. 横向支撑与反馈

- Knowledge、Policy、Governance 作为横向支撑，不成为主执行层；
- 反馈链固定为 `Business Result → Observation → Measure/Evaluation → Change → 新版本`；
- Evaluation 是过程，不建立一级对象；Workflow 是复合 Skill 内部编排。

### 4. Knowledge 与 Tolaria

Knowledge 使用四分类：System Core、Distributable Domain、Enterprise Private、Runtime Evidence / Learning Candidate。每项 Knowledge 声明所有者、作用域、版本、摘要、权限、来源、分发和提升规则。

Tolaria 只是可替换 Markdown 管理与展示载体，通过 Knowledge Adapter 接入。它不是 ArcheOS 模块、核心对象、语义事实源或运行依赖。已发布 Agent 使用已解析的版本化 Knowledge 制品。

### 5. 蓝图

废弃五层产品分面作为正式架构表达，采用一条从上到下的主责任链；Knowledge 放左侧，Policy/Governance 放右侧，反馈链放底部。用实线、虚线和点线分别标记 v0.1、M1 平台化和 M2 远期演进。

### 6. Amazon Ops Kit

Amazon Ops Kit 保持店铺经营者不可见的内部 Amazon 执行内核和领域资产来源。其 Agent Port、Skill、Tool、Policy、Measure 和证据通过精确版本/digest 接入；不复制为第二事实源，也不拥有经营目标、用户权限、审批或 Agent 生命周期。

## 被否决的方案

- **保留 Capability View 正式术语**：没有独立业务价值或生命周期，并与真实 Capability Package 混淆。
- **把 Capability 和 Capability Package 建成两个实体**：产生重复 ID、版本、状态和依赖同步问题。
- **所有 Ops Kit Capability Matrix 行都升级为 Capability**：会把数据检查、内部方法和技术组件包装成虚假业务职责。
- **KPI 直接分配给 Capability**：失去具体 Workspace 的持续履职、异常和人工交接责任主体。
- **Tolaria 作为 Knowledge 模块或运行依赖**：把当前工具选择固化为核心架构，并破坏可分发 Knowledge 的独立运行。
- **Knowledge 只分“系统/用户记忆”两类**：无法表达领域分发、企业隔离和学习候选提升边界。
- **继续使用五层产品分面**：把观察视图误读为依赖层，主责任方向不清。
- **把未来所有模块纳入 v0.1**：制造隐性范围；M1/M2 只保留演进位置和稳定接口。

## 受影响模块

- `AGENTS.md`：分层和工作规则中的 Capability/Knowledge/Tolaria 语义；
- `docs/architecture/object-boundaries.md`：新增 Capability/Package、Tool、Knowledge 边界并固化责任链；
- `docs/architecture/system-architecture.md`：主链、横向支撑、反馈、接口和阶段；
- `docs/architecture/blueprint-review.md`：历史图与 Ops Kit 资产重新分类；
- 架构 SVG/PNG；
- 后续 Schema、Agent Factory、Asset Lifecycle、Business Cockpit、Knowledge Adapter 和 Ops Kit Domain Adapter。

## 下游影响

- Product Brief / PRD：不修改；PRD v0.6.0 是本 ADR 的批准输入。
- Roadmap：继续 `stale`，待 Architecture 批准后由交付负责人重新评审。
- Milestone / Issue / Implementation：继续 blocked；本 ADR 不创建或授权任何下游工作。
- 历史 ADR：ADR-0002 仅 Capability/Knowledge 语义被取代；ADR-0003 标记 Superseded。

## 迁移

1. 删除正式 Capability View 术语；UI 直接展示真实 Capability Package 版本、状态、结果契约、依赖和证据。
2. 旧 Capability 内容按结果契约判定：满足条件则迁移为 Package，否则分类为 Skill、Tool、Policy、Measure、Knowledge 或质量门禁。
3. Agent Profile 与 Deployment 增加精确 Capability Package 引用和 KPI Assignment。
4. Tolaria 内容不强制迁移；补充 Knowledge 分类和元数据，通过 Adapter 解析为版本化制品。
5. Ops Kit Capability Matrix 逐项评审，不机械迁移；来源仓库、版本、digest 和证据保持可追溯。
6. 旧架构图保留为历史附件；v0.5 新图成为候选主图。

## 回滚

在 Architecture v0.5 获批前，上一批准基线仍是 v0.3.0。若本候选被否决：

1. manifest 恢复 Architecture v0.3.0 approved 版本与摘要；
2. Roadmap 继续 stale，不依据候选语义创建下游；
3. 保留本 ADR、对象边界和蓝图 diff 作为未采纳证据；
4. 不修改 PRD v0.6.0，产品与架构冲突需由项目经理重新协调，而不是选择实现便利的版本。

若未来已有 v0.5 下游实现，再回滚必须先暂停受影响 Agent、冻结写入、恢复前一已验证 Capability/Skill/Knowledge/Policy 制品和 Deployment 配置，然后回退架构版本；不得删除 Observation 或审计证据。

## 开放风险

1. Capability 兼容性判定需要 Schema 和契约测试，不能仅依赖语义版本号。
2. Ops Kit Capability Matrix 与 Capability Package 的映射尚未逐项完成。
3. Knowledge 脱敏、抽象和提升需要人工质量门禁，自动化范围尚待 M1 验证。
4. Capability 不嵌套规则在跨领域扩展时可能需要复审，但 v0.1 不提前引入复杂依赖。
5. Writer Lease、Secret Provider 和证据恢复仍需实现级故障测试。

## 审批

Leo 于 2026-07-12 正式批准 Architecture v0.5.0 与本 ADR。批准只确认本 ADR 的架构语义、迁移和回滚边界；Roadmap 仍需由项目经理派发交付负责人独立重审，不因本批准自动生效。
