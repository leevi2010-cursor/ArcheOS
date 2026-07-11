# 核心对象与边界

本文件定义 ArcheOS 当前版本的核心对象。只有直接参与“经营意图 → Agent 执行 → 业务反馈 → 受控变更”闭环，并且拥有独立职责和生命周期的对象，才列为核心对象。

## 1. Workspace｜业务空间

**定义**

Workspace 是 ArcheOS 中最高级的业务与治理隔离边界。一个 Workspace 对应一个独立经营主体、客户、业务单元，或一套需要独立治理的 Agent 环境。

**负责**

- 隔离经营意图、Policy、Agent、数据接入、业务表现和变更历史；
- 确定租户或业务单元的权限边界；
- 作为其他核心对象的归属容器。

**不负责**

- 表达具体经营策略；
- 执行业务任务；
- 定义 Skill 的实现；
- 充当项目管理工具中的 Project。

**示例**

- `Amazon Business - JP Store`
- `黄鸿儒展厅`
- `Customer A - Amazon Operations`

---

## 2. Business Intent｜经营意图

**定义**

Business Intent 是人类对一个 Workspace 的经营方向、优先级、权衡方式、风险偏好和决策权的正式表达。

**负责**

- 定义企业当前追求什么；
- 定义利润、现金流、增长、规模等目标之间的优先级；
- 定义风险承受能力和最终决策权；
- 为 Policy、Measure 和 Agent 治理提供方向。

**不负责**

- 规定具体执行步骤；
- 直接调用 Tool 或 Skill；
- 保存日常业务数据；
- 代替可执行 Policy。

**示例**

- 现金流安全优先于销售规模；
- 在毛利率不低于 15% 的前提下追求增长；
- 战略决策由企业负责人最终裁决，Agent 负责提出建议。

---

## 3. Policy｜治理规则

**定义**

Policy 是对 Agent、Skill 或业务动作施加的可执行规则，用于把经营意图转化为允许、禁止、要求、审批和优先级约束。

**负责**

- 定义在什么条件下允许、禁止或要求执行某类动作；
- 规定作用域、优先级、例外和冲突处理；
- 将 Business Intent 转化为可执行边界；
- 为 Runtime 和 Agent 提供治理约束。

**不负责**

- 描述完整任务流程；
- 实现业务动作；
- 保存企业经营目标本身；
- 记录一次具体决策过程。

**示例**

- 未经人工审批，不得将广告预算提高超过 20%；
- 当现金储备低于安全线时，禁止启动高风险扩张动作；
- 客户级 Policy 优先于 Skill 的默认建议。

---

## 4. Agent Deployment｜Agent 部署

**定义**

Agent Deployment 是一个已经被配置、部署、可观察和可治理的业务 Agent 实例。它代表 ArcheOS 实际管理的 Agent 单元，而不是抽象角色、聊天线程或代码仓库。

**负责**

- 承担明确的业务职责；
- 绑定 Workspace、Runtime、Skill、Policy、Tool 权限和 Schedule；
- 产生可观察的业务行为和运行轨迹；
- 接受版本发布、暂停、升级和回滚。

**不负责**

- 定义 Skill 的通用实现；
- 拥有 Runtime；
- 保存全部业务知识；
- 自行修改自身治理规则而不经过 Change。

**示例**

- `Amazon OPT Agent - JP Store 01`
- `Showroom Operations Agent - 黄鸿儒展厅`

---

## 5. Runtime Instance｜运行实例

**定义**

Runtime Instance 是承载一个或多个 Agent Deployment 的实际运行环境。它负责执行任务、调用工具、调度任务并输出运行状态。

**负责**

- 承载 Agent 的运行；
- 执行任务、调用 Tool 和子 Skill；
- 管理 Scheduler、任务状态和 Runtime Telemetry；
- 执行部署、启动、暂停和回滚指令。

**不负责**

- 决定企业经营目标；
- 定义 Policy；
- 判断业务表现是否成功；
- 拥有 Skill 的治理生命周期。

**示例**

- 某个 Hermes 运行实例；
- 未来兼容 ArcheOS 接口的其他 Agent Runtime 实例。

---

## 6. Skill｜技能

**定义**

Skill 是 Agent 完成某类任务的可执行方法，是 ArcheOS 中最核心的可复用执行资产。Skill 可以是原子能力，也可以组合并调用其他 Skill 和 Tool。

**负责**

- 声明输入、输出和执行目的；
- 调用 Tool 或子 Skill 完成任务；
- 声明依赖、权限、默认 Policy 和测试；
- 以版本化方式被 Agent Deployment 装载。

**不负责**

- 定义企业经营方向；
- 保存客户业务状态；
- 决定自身是否应被部署；
- 覆盖 Workspace 或 Agent 已有的高优先级 Policy；
- 直接判断长期业务成败。

**示例**

- `analyze-amazon-ppc`
- `generate-daily-operations-report`
- `review-showroom-customer-followup`

---

## 7. Measure｜衡量定义

**定义**

Measure 定义系统观察什么、如何计算、采用什么时间窗口，以及结果如何解释。它是业务评价、Agent 评价和 Skill 评价的统一基础。

**负责**

- 定义指标名称、数据来源、计算公式和时间窗口；
- 指明数值变化的方向和解释方式；
- 支撑业务表现、Agent 表现、Skill 质量和风险评估；
- 为 Change 提供成功和回滚标准。

**不负责**

- 采集所有原始事件；
- 单独决定经营目标；
- 直接修改 Agent；
- 代替一次完整的 Evaluation 过程。

**示例**

- 贡献毛利；
- 广告建议采纳率；
- Agent 任务成功率；
- 库存周转天数；
- 高风险动作误执行次数。

---

## 8. Observation｜观察记录

**定义**

Observation 是系统从业务环境、Runtime 或人类交互中获得的不可变事实记录。它描述“发生了什么”，是 ArcheOS 获得反馈和形成生命循环的基础。

**负责**

- 记录业务指标变化、Agent 行为、Skill 输出、Runtime 错误和人类反馈；
- 保留时间、来源、对象和追踪信息；
- 为 Measure 计算、问题诊断和 Change 提供证据。

**不负责**

- 解释事实意味着什么；
- 自动成为 Policy 或长期规则；
- 被人工修改成更符合预期的结果；
- 直接决定系统应如何变化。

**示例**

- 某广告活动在 2026-07-11 被 Agent 下调预算 10%；
- 某 Skill 本次执行失败；
- 企业负责人拒绝了一个扩张建议；
- 过去 30 天现金余额下降 18%。

---

## 9. Change｜受治理的变更

**定义**

Change 是 ArcheOS 中一次可审查、可验证、可发布、可观察和可回滚的系统改变，是 Agent 持续进化的基本单位。

**负责**

- 描述问题、假设、目标对象和拟议修改；
- 记录证据、风险、审批、测试、发布和回滚条件；
- 修改 Business Intent、Policy、Skill、Agent Deployment、Runtime 配置或 Measure；
- 追踪变更结果并决定保留或回滚。

**不负责**

- 代替 Observation；
- 跳过审批直接修改高风险配置；
- 将一次讨论自动提升为正式规则；
- 作为普通项目管理 Issue 的同义词。

**示例**

- 为广告优化 Skill 增加现金流上下文；
- 将某 Agent 的自动预算调整权限从 10% 降至 5%；
- 替换库存预测 Skill 的模型版本；
- 调整主要经营目标，从销售增长切换为现金流安全。

---

# 概念之间的边界与差异

## Workspace 与 Project

- Workspace 是 ArcheOS 的业务和治理隔离边界；
- Project 是 GitHub、Tolaria 或其他协作工具中的项目管理载体；
- 一个 Workspace 可以长期存在，并包含多个项目；
- Project 不属于 ArcheOS 的运行时核心对象。

## Business Intent 与 Policy

- Business Intent 回答“我们追求什么、如何取舍”；
- Policy 回答“在什么条件下允许、禁止或要求做什么”；
- Intent 是方向，Policy 是可执行边界。

**示例**

- Intent：现金流安全优先；
- Policy：现金余额低于安全线时不得自动增加广告预算。

## Policy 与 Skill

- Policy 规定“能不能做、何时做、需要谁批准”；
- Skill 规定“具体怎样完成”；
- Policy 不应包含复杂执行步骤；
- Skill 不应自行覆盖更高层级的 Policy。

## Agent Deployment 与 Agent Profile

- Agent Deployment 是实际已部署、可运行、可观察的 Agent 实例；
- Agent Profile 可以作为模板或配置草案存在，但不是独立核心对象；
- Profile 被实例化并绑定 Workspace 与 Runtime 后，才形成 Agent Deployment。

## Agent Deployment 与 Runtime Instance

- Agent Deployment 是业务责任主体；
- Runtime Instance 是运行容器；
- 一个 Runtime Instance 可以承载多个 Agent Deployment；
- Runtime 不拥有 Agent 的经营目标和治理规则。

## Agent Deployment 与 Skill

- Agent Deployment 承担完整业务职责；
- Skill 提供局部可复用的执行方法；
- Agent 可以装载多个 Skill；
- Skill 可以被多个 Agent Deployment 复用。

## Skill 与 Workflow

- Workflow 不作为 ArcheOS 的一级核心对象；
- 顺序、分支、循环、并行、人工审批等编排逻辑属于 Skill 的内部实现；
- 复杂 Skill 可以调用子 Skill；
- 业务语言中仍可使用“工作流”一词，但系统对象统一为 Skill。

## Skill 与 Tool

- Skill 是带业务语义的执行方法；
- Tool 是 Runtime 可调用的底层操作能力；
- Skill 可以调用 Tool；
- Tool 不应包含复杂经营判断。

**示例**

- Skill：分析 Amazon PPC 并提出优化建议；
- Tool：读取 Amazon Ads API 报表。

## Capability 与 Skill

- Capability 不作为独立核心对象；
- Capability 是根据 Agent 已装载的 Skill、可用 Tool、权限、Policy 和验证结果推导出的能力视图；
- Runtime 不需要理解 Capability；
- 当一项能力完全对应一个 Skill 时，不再重复建立 Capability 实体。

## Measure 与 Observation

- Observation 回答“发生了什么”；
- Measure 回答“如何计算和解释这些事实”；
- Observation 是原始证据；
- Measure 是计算与解释规则。

## Measure 与 Evaluation

- Measure 是稳定定义；
- Evaluation 是使用一个或多个 Measure 对 Agent、Skill 或 Change 进行判断的过程；
- Evaluation 可以产生结果记录，但不必作为并列核心对象。

## Observation 与 Memory

- Observation 是不可变事实；
- Memory 是 Runtime 或知识系统对信息的保存和提取机制；
- ArcheOS 不建立一个宽泛的统一 Memory 核心对象；
- Tolaria 可以保存业务上下文、案例和长期记忆，但这些内容不自动成为 Observation 或 Policy。

## Change 与 Issue

- Change 是 ArcheOS 的受治理变更对象；
- Issue 是 GitHub 或其他协作工具中的任务和讨论载体；
- 一个 Issue 可以提出一个 Change；
- Change 的状态、审批、发布和结果不能只依赖 Issue 文本表达。

## Change 与 Decision

- Decision 是 Change 或 Business Intent 中的一次选择及其理由；
- Decision 可以作为记录存在，但不需要成为并列核心对象；
- Change 负责完整的验证、发布、观察和回滚生命周期。

## Change 与 Experiment

- Experiment 是 Change 的一种验证方式；
- 并非所有 Change 都需要实验；
- Experiment 不独立承担发布和回滚责任。

## Business Intent 与 Measure

- Business Intent 定义什么重要；
- Measure 定义如何知道它有没有实现；
- 没有 Intent 的 Measure 可能只产生数据，没有经营意义；
- 没有 Measure 的 Intent 无法被验证。

# 非核心对象归属

以下概念可以继续存在，但不属于 ArcheOS 核心领域对象：

- `Type`：Tolaria 的笔记结构机制；
- `Knowledge`、`Tutorial`、`Note`、`Case`、`Meeting`：知识与内容载体；
- `Project`、`Issue`、`Pull Request`、`Milestone`、`ADR`：开发和协作流程对象；
- `Capability`：派生能力视图；
- `Workflow`：Skill 的内部编排方式；
- `Evaluation`：基于 Measure 的评价过程；
- `Decision`、`Experiment`、`Release`、`Rollback`：Change 生命周期中的记录、方法或阶段；
- `Memory`：知识系统或 Runtime 的存储机制，而非统一核心对象。
