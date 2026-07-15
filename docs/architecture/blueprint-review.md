# ArcheOS 蓝图重审记录

## 结论

早期“ArcheOS 架构图 v1.0”对完整产品范围的表达优于后来过度压缩的控制闭环图，但它把 Capability、Workflow、Protocol、Evaluation 和基础设施混成层级。PRD v0.6.0 已批准新的稳定语义：以业务责任主链为骨架，以 Knowledge、Policy、Governance 为横向支撑，以 Observation/Measure/Evaluation/Change 为独立反馈链。

当前目标不是恢复旧图，而是保留它对业务系统、Agent、能力、执行资产、基础设施和治理的完整视野，同时消除伪层级和重复实体。

## 旧图处理决定

| 旧图内容 | 重审结论 | 当前表达 |
|---|---|---|
| 业务系统 / 业务场景 | 保留 | External System 和 Business Result；不纳入 ArcheOS 内部 |
| Domain Agent | 保留 | Agent Deployment，承担 Workspace KPI |
| Capability 模块库 | 保留并严格化 | Capability 是一级业务职责；Package 是同一对象的版本化发布制品 |
| Skills & Workflows | 重构 | Skill 是执行方法；Workflow 是复合 Skill 内部编排 |
| 基础设施与资源 | 保留但不作为业务层 | Runtime Instance、Tool Adapter、Secret Provider、Evidence Store |
| 治理与标准 | 保留为横向控制 | Policy、Authorization、Architecture Governance、Contracts |
| 资产生命周期 | 保留 | 验证、版本、发布、暂停、废弃、影响、回滚 |
| 评估与质量 | 保留为反馈与治理 | Observation、Measure、Evaluation process、Change |
| 知识与记忆 | 重构 | Knowledge 四分类；Tolaria 仅为可替换 Markdown 载体 |
| Capability View | 删除 | UI 直接展示真实 Capability Package 和 Agent 当前状态 |
| Protocol / Tutorial 一级对象 | 不保留 | 分类为 Policy、Skill、Knowledge 或流程文档 |

## 核心责任判定

```text
Business Intent / KPI
        ↓
Agent Deployment
        ↓
Capability Package
        ↓
Skill
        ↓
Tool / External System
        ↓
Business Result
```

- Agent Deployment 对具体经营 KPI 负责；
- Capability 对可独立承诺和验收的结果契约负责；
- Skill 对执行方法和局部正确性负责；
- Tool 对调用契约负责；External System 提供外部事实和响应。

## Capability / Skill 判断规则

只有经营者可以把一项内容作为完整业务职责委托给 Agent，且它具有独立结果、调用和验收时，才建立 Capability。

| Amazon Ops Kit 现有表述 | 初步分类 | 理由 |
|---|---|---|
| 广告优化 | Capability Package | 可承诺改善广告投入效率并形成闭环 |
| 广告经营复盘 | Capability Package 候选 | 可以独立交付、验收和产生行动价值 |
| 数据完整性检查 | Skill / Data Contract / Quality Gate | 是前置质量条件，不是完整业务职责 |
| 广告诊断 | 默认 Skill；独立顾问交付时可成为 Capability | 取决于是否为最终业务交付 |
| 关键词学习 | 默认 Skill | 产生中间资产，需要通过广告运营兑现价值 |
| 受控广告写入 | Skill + Tool + Policy | 是安全执行方法，不是经营职责 |

Ops Kit Capability Matrix 不能机械迁移为 Capability Package Catalog；必须逐项通过结果契约判定。

## Knowledge 决策

| 类型 | 能否分发 | 典型内容 |
|---|---|---|
| System Core Knowledge | 默认不分发给企业 Agent | 架构、安全、接口、治理原则 |
| Distributable Domain Knowledge | 可按版本跨 Workspace 分发 | Amazon Ads 知识、领域方法和术语 |
| Enterprise Private Knowledge | 禁止跨 Workspace 分发 | 店铺成本、客户档案、企业策略、会议 |
| Runtime Evidence / Learning Candidate | 默认不可作为正式知识加载 | Observation、失败案例、反馈和改进候选 |

Tolaria 只负责当前 Markdown 内容的编辑、关系展示和管理。ArcheOS 依赖 Knowledge Contract，不依赖 Tolaria；已发布 Agent 使用解析后的版本化 Knowledge 制品。

## 与 Amazon Ops Kit 的映射

| ArcheOS 语义 | Ops Kit 实际资产 | 接入原则 |
|---|---|---|
| Amazon Capability Package | 广告运营闭环中满足业务结果契约的资产组合 | 以精确来源版本/digest 发布，不复制仓库 |
| Skill | 数据读取、诊断、策略、报告、学习、受控写入方法 | 声明输入输出、权限、测试和失败语义 |
| Tool | Ads/SP-API、SQLite、文件、模型、投递接口 | 通过 Adapter 暴露调用契约 |
| Policy | 预算红线、权限、allowlist、审批和数据门禁 | 不能被客户策略或 Skill 放宽 |
| Measure / Observation | freshness、完整性、执行、readback、audit evidence | 支撑 KPI 和 Capability 验收 |
| Runtime Adapter | Agent Port、Hermes/Codex projection、scheduler | Ops Kit 不拥有 Agent 生命周期和用户体验 |

## 演进阶段

### v0.1 — Amazon 业务价值闭环

- 单 Workspace、单 Amazon Ads Agent；
- Capability Package 创建、验证、发布、暂停和回滚；
- Knowledge 四分类和一次受控提升；
- Ops Kit 内核、双容器、安全写入、证据与 Change 闭环。

### M1 — 平台化

- 多 Workspace、资产发现和依赖影响；
- 完整质量中心、Knowledge 治理、Runtime fleet、模型和成本治理；
- 不改变主责任链或核心对象语义。

### M2 — 跨领域与企业交付

- 多 Domain Agent、领域资产包和跨领域 Tool Adapter；
- 组织角色、租户隔离、合规、授权升级和商业化；
- Amazon 特例仍留在 Domain Adapter。

## 不可破坏的不变量

1. Workspace 是业务、权限和数据隔离边界。
2. KPI 委托给 Agent Deployment，而不是 Capability 或 Skill。
3. Capability 与 Package 共享一个对象身份和生命周期。
4. Capability 组合 Skill；v0.1 不嵌套 Capability。
5. Knowledge、Policy 和 Governance 是横向支撑，不伪装成执行层。
6. Observation 不可变；运行证据不会自动提升为正式资产。
7. Tolaria 和 Runtime 都可替换，不成为核心对象语义事实源。
8. Amazon Ops Kit 是内部执行内核和领域资产来源，不是第二产品或第二核心事实源。
9. 写入始终经过授权、审批边界、协调、审计和回滚。
10. MVP 可以少实现模块，但不能绕过稳定接口。
