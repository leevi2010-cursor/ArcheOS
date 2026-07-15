# ADR-0002: 统一系统架构与核心对象模型

- Status: Accepted
- Date: 2026-07-12
- Architecture from: 0.1.0-existing-baseline
- Architecture to: 0.3.0
- Supersedes: 旧 Capability Contract / Binding / Protocol / Evaluation 并列模型
- Superseded in part by: ADR-0004（Capability、Knowledge 与 Tolaria 语义）

> 历史说明：本 ADR 在 PRD v0.5.0 下被接受。PRD v0.6.0 后，Capability 派生视图、Knowledge/Tolaria 相关决定由 ADR-0004 取代；其余关于双工作台、授权、写入协调、秘密隔离和 Ops Kit 边界的决定继续有效。

## 背景

`object-boundaries.md` 已把 Workspace、Business Intent、Policy、Agent Deployment、Runtime Instance、Skill、Measure、Observation、Change 定义为核心对象，并明确 Capability 是派生视图、Evaluation 是基于 Measure 的过程。`system-architecture.md` 和旧附件仍保留独立 Capability Contract、Binding/Resolver、Protocol 和 Evaluation 模型，形成冲突。

## 决策

以 `object-boundaries.md` 为核心对象语义事实源；`system-architecture.md` 负责表达分层、模块协作和反馈闭环。Capability 作为派生治理视图，Evaluation 作为基于 Measure 的评价过程，Policy 作为正式治理对象。

针对 PRD v0.5.0，进一步决定：

1. Business Cockpit 采用单应用、双工作台；真正权限边界由服务端 Authorization 强制执行，活动角色必须显式且可审计。
2. Business Intent 由版本化 Resolver 确定性映射为有效 Policy 与策略参数，平台安全底线不可被 Workspace 放宽。
3. Agent 创建从版本化领域资产生成 Profile，经验证和平台审批后发布并实例化为 Agent Deployment；Capability 仅为投影视图。
4. 读取/分析 Change 可自动受限验证；任何可能影响 Amazon 写入方法、参数或范围的 Change 必须事前获得店铺经营者审批。
5. 相同写入范围采用带 fencing token 的单写入者租约，并用确定性 Write Ledger 去重作为补充防线。
6. 配置只保存 SecretRef；秘密只在 Runtime 隔离区按最小权限解析，帮助和验证接口与秘密值分离。
7. Amazon Ops Kit 通过 Domain Adapter 提供 Skill/Tool 实现来源，不成为核心对象事实源，也不能绕过 ArcheOS 写入治理。
8. `apps/business-cockpit/` 仅是原型来源；必须等 ready Issue 后才允许实现复用。

## 被否决的方案

- 同时保留两套模型：会让 Runtime、Schema 和 Roadmap 无法判断应实现哪一套对象。
- 以旧 Capability Contract 模型覆盖新对象边界：会丢失 Business Intent、Measure、Observation 和 Change 驱动的经营治理闭环。
- 把两个工作台拆成独立应用：增加重复交付，并不能替代服务端权限边界。
- 仅靠前端隐藏操作区分角色：无法阻止直接 API 调用。
- 双容器只靠调度错峰或容器名避免重复写：无法在重试、时钟偏差和故障切换时提供确定性保证。
- 仅使用幂等键而没有写入者租约：不能阻止两个实例对语义相近但 key 不同的并发动作。
- 把密钥复制进 Agent 包或容器配置：破坏独立部署的秘密隔离和撤销能力。

## 受影响模块

- 系统架构文档和架构附件；
- Roadmap M1-M7 的术语与交付对象；
- 未来 Schema、Runtime Adapter、治理界面和 Pilot；
- 旧 Protocol、Capability、Evaluation 内容的迁移分类。
- Business Cockpit 的双工作台授权与审计接口；
- Intent Resolver、Change Control、Writer Coordination、Write Ledger 和 Secret Provider；
- Amazon Ops Kit Domain Adapter 与资产来源追踪。

## 下游影响

- Roadmap：维持 `stale`，由交付负责人基于 Architecture v0.3.0 重新评审；不得由本 ADR 直接批准。
- Milestone：保持 `blocked`，不得依据旧模型创建。
- Issue：保持 `blocked`，已有草案需在 Roadmap 重新批准后复核。
- PRD：不由本 ADR 修改；仍由产品经理负责。

## 迁移

1. 旧 Capability 内容迁移为 Capability View 定义、Skill 或 Agent Deployment 配置。
2. 旧 Protocol 内容分类为 Policy、Skill、知识、决策原则或流程文档。
3. 旧 Evaluation 定义拆为 Measure、评价过程和结果记录。
4. 不删除历史资料；通过 Git 与 ADR 保留演进证据。
5. 现有 Amazon Ops Kit 能力先盘点来源、权限和接口，再登记为 Skill/Tool adapter；不复制店铺事实。
6. 现有 Business Cockpit 仅在 ready Issue 后迁移导航与术语，后端授权先于高风险操作接入。

## 回滚

若 v0.3.0 在下游实现前被否决，可把 Architecture 指针恢复到 v0.2 in-review 摘要并保持 Roadmap stale。下游实现后回滚必须先停用写入 Deployment、恢复前一已批准 Policy/Skill/Runtime 配置，再回退文档版本；不得关闭 Writer Coordination 或秘密隔离来兼容旧实现。

## 审批

PRD v0.5.0 已批准；技术负责人于 2026-07-12 复核其 SHA-256 为 `8b21a2d93081cbe0f0bf31245feb8e577384bbdadf6af45920cb6b36b3a0645b`，并确认 `governance-check audit` 与 `create-architecture` 均通过。本 ADR 与 Architecture v0.3.0 已接受；Roadmap 仍需独立门禁和交付负责人批准。

## 开放风险

- Amazon Ads API 对各写操作的原生幂等保证需要在适配设计中逐项核实，Write Ledger 不能假设上游统一支持。
- 租约协调服务的可用性、时钟与网络分区策略需要在技术设计中验证；安全优先于写入可用性。
- Amazon Ops Kit 当前资产与新对象模型的映射尚未完成，不能据此宣称所有 Skill 已可发布。
- 凭证存储供应商与 Runtime 身份机制尚未选定，但不得削弱 SecretRef 和最小权限不变量。
