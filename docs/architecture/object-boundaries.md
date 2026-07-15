# 核心对象与边界

本文件是 ArcheOS 核心对象语义的权威事实源。一级对象必须直接参与“经营意图与 KPI → Agent 履职 → 业务结果 → 证据评价 → 受控变更”闭环，并具有独立职责与生命周期。

## 1. Workspace｜业务空间

**定义**

Workspace 是业务、数据、权限和治理的最高隔离边界，对应一个经营主体、客户、业务单元或独立 Agent 环境。

**负责**

- 隔离 Business Intent、KPI、Agent Deployment、企业私有 Knowledge、数据接入和变更历史；
- 确定成员、权限和资产适用范围；
- 为业务结果和审计提供归属边界。

**不负责**

- 表达具体经营策略；
- 执行业务动作；
- 代替项目管理工具中的 Project。

---

## 2. Business Intent｜经营意图

**定义**

Business Intent 是人类对 Workspace 经营方向、优先级、权衡、风险偏好和最终决策权的正式表达。KPI 是 Business Intent 中以 Measure、目标值和时间窗口表达的可验证委托。

**负责**

- 定义企业追求什么以及如何取舍；
- 为 Agent Deployment 分配 KPI、目标窗口和安全约束；
- 为 Policy、Capability 和 Measure 提供方向。

**不负责**

- 规定执行步骤；
- 直接调用 Capability、Skill 或 Tool；
- 替代 Policy 或评价过程。

---

## 3. Agent Deployment｜Agent 部署

**定义**

Agent Deployment 是已配置、已部署、可观察且可治理的业务 Agent 实例，是 Workspace KPI 的直接履职责任主体。

**负责**

- 接受 Business Intent/KPI 委托并持续承担业务职责；
- 装载精确版本的 Capability Package、Policy、Knowledge、Runtime、权限、SecretRef 和 Schedule；
- 协调 Capability 的运行、人工交接、异常、暂停和恢复；
- 汇总业务结果与证据，说明 KPI 的完成或未完成状态。

**不负责**

- 定义 Capability 或 Skill 的通用实现；
- 因某个 Capability 局部达标而宣称整体 KPI 已达标；
- 绕过 Change 修改治理规则、最高级目标或权限。

---

## 4. Capability / Capability Package｜业务能力 / 能力包

**定义**

Capability 是可由 Agent 独立调用、可向经营者承诺并可独立验收的一项业务职责。Capability Package 是该 Capability 的不可变、版本化发布制品。

Capability 与 Capability Package **不是两个核心对象**：

- Capability 表达稳定的业务语义和对象身份；
- Capability Package 表达该对象在某一版本的结果契约、资产组合和发布内容；
- 二者共享同一 `capability_id`、版本谱系、生命周期和治理记录。

**负责**

- 声明业务结果契约、适用范围、前置条件、失败影响和非职责；
- 声明输入、输出、成功阈值、证据、权限、审批、兼容范围和责任人；
- 组合精确版本的 Skill，并引用所需 Tool、Knowledge、Policy 和 Measure；
- 声明调用接口、验收标准、证据要求和回滚目标；
- 独立经历草稿、验证、发布、暂停、废弃和回滚生命周期；
- 在不改变结果契约时允许替换兼容 Skill 实现。

**不负责**

- 承担具体 Workspace 的最终 KPI；该责任属于 Agent Deployment；
- 保存客户运行状态或秘密；
- 把技术步骤、数据检查或单一算法包装成虚假业务能力；
- 作为 UI 派生视图或 Runtime 可用性快照。

**成立条件**

一项内容只有同时满足以下条件，才应成为 Capability：

1. 经营者可以把它作为完整业务职责委托给 Agent；
2. 具有业务语言表达的结果，而不是技术中间输出；
3. 可以独立调用和验收；
4. 通常需要组合多个 Skill、Tool、Knowledge 或 Policy；
5. 失败会在业务责任层面可见。

**例子**

- `amazon-ads-optimization`：在经营目标和安全边界内持续改善广告投入效率；
- `amazon-ads-operating-review`：形成可追溯、可行动的周期经营复盘。

**反例**

- 广告数据完整性检查：Skill、Data Contract 或质量门禁；
- 关键词学习：通常是广告优化 Capability 内部的 Skill；
- 只生成中间异常分数：Skill 或 Measure。

v0.1 由 Agent Deployment 组合多个 Capability；Capability Package 只组合 Skill，不嵌套其他 Capability。未来若真实业务证明需要 Capability 依赖，必须单独 ADR 说明循环依赖和责任传播规则。

---

## 5. Skill｜技能

**定义**

Skill 是完成任务的版本化执行方法。它可以是原子方法，也可以包含顺序、分支、并行、循环和人工交接，并调用子 Skill 或 Tool。

**负责**

- 声明输入、输出、执行目的和失败语义；
- 实现 Capability 所需的专业方法；
- 声明 Tool/子 Skill 依赖、权限、测试和评价证据；
- 对局部执行正确性和可重复性负责。

**不负责**

- 承担 Workspace KPI 或完整业务职责；
- 定义 Business Intent；
- 覆盖高优先级 Policy；
- 保存客户业务状态或长期 Knowledge。

Workflow 不是一级对象；编排是复合 Skill 的内部实现。Skill 不因代码量大或步骤多自动升级为 Capability。

---

## 6. Tool｜工具接口

**定义**

Tool 是 Runtime 可调用的底层操作接口，用于访问数据、服务、文件或外部业务系统。

**负责**

- 定义调用输入、输出、错误、权限和幂等语义；
- 执行读取、写入或计算操作；
- 返回外部系统响应和可追踪事实。

**不负责**

- 做复杂经营判断；
- 承担 Capability 结果契约；
- 决定是否允许某项高风险业务动作。

External System 不属于 ArcheOS；Amazon、ERP、CRM 等是 Tool 调用的外部事实源和行动对象。

---

## 7. Policy｜治理规则

**定义**

Policy 是作用于 Workspace、Agent Deployment、Capability、Skill 或业务动作的可执行规则，把经营意图转化为允许、禁止、要求、审批和优先级约束。

**负责**

- 声明作用域、优先级、冲突处理和例外；
- 约束资产选择、运行、外部写入和 Change；
- 在证据、授权或依赖不足时 fail closed。

**不负责**

- 描述完整任务流程；
- 实现业务动作；
- 保存经营目标或一次具体审批记录。

---

## 8. Knowledge｜知识资产

**定义**

Knowledge 是 Agent、Capability 或 Skill 可引用的版本化知识内容。每项 Knowledge 必须声明分类、所有者、作用域、版本、权限、来源、分发和提升规则。

**四类 Knowledge**

| 分类 | 作用域与分发 | 典型内容 | 约束 |
|---|---|---|---|
| System Core Knowledge | ArcheOS 系统级；默认不向企业 Agent 分发 | 架构原则、安全原则、接口标准、治理规则 | 只能经系统治理变更 |
| Distributable Domain Knowledge | 领域级；可随 Capability Package 或领域资产包分发 | Amazon Ads 术语、广告方法、展厅接待框架 | 必须脱离客户事实并有来源、版本和验证 |
| Enterprise Private Knowledge | 单一 Workspace；禁止跨企业分发 | 店铺成本、客户档案、企业策略、项目和会议 | 由企业所有者授权，保持隔离 |
| Runtime Evidence / Learning Candidate | 运行与候选作用域；默认不可作为正式 Knowledge 加载 | Observation、失败案例、反馈、改进假设 | 先作为证据；只有经脱敏、抽象、验证和人工批准后才能提升 |

**负责**

- 提供业务、领域或系统上下文；
- 保留来源、版本、权限和分发边界；
- 在发布或提升时触发依赖影响检查。

**不负责**

- 直接执行业务动作；
- 自动成为 Policy；
- 把企业私有事实静默提升为可分发知识；
- 依赖特定笔记产品才能被已发布 Agent 使用。

Tolaria 只是当前的 Markdown 管理与展示载体，可通过 Knowledge Adapter 导入、导出或编辑知识。它不是 ArcheOS 模块、核心对象、事实语义或运行依赖。已发布 Agent 使用已解析、版本化的 Knowledge 制品；Tolaria 不可用时仍可运行。

正式 Knowledge 使用 `草稿 → 评审中 → 已批准 → 已发布 → 已暂停 → 已废弃`；Runtime Evidence / Learning Candidate 使用 `已记录 → 待筛选 → 已拒绝 / 已提升候选`，两套状态不得混用。

---

## 9. Runtime Instance｜运行实例

**定义**

Runtime Instance 是承载一个或多个 Agent Deployment 的执行环境。

**负责**

- 装载已发布 Agent、Capability Package、Skill 和 Knowledge 制品；
- 执行任务、调用 Tool、调度和输出 Telemetry；
- 执行启动、暂停、恢复和回滚指令。

**不负责**

- 决定 KPI、Capability 语义或 Policy；
- 拥有业务资产生命周期；
- 在控制面不可验证时沿用旧写入授权。

---

## 10. Measure｜衡量定义

**定义**

Measure 定义观察什么、如何计算、采用什么时间窗口以及如何解释，是 KPI、Capability 验收、Skill 质量和 Change 判断的共同基础。

**负责**

- 声明数据来源、公式、窗口、方向和置信度；
- 支撑 Agent KPI、Capability 结果契约和回滚条件；
- 区分真实、估算和不可判断结果。

**不负责**

- 采集原始事实；
- 单独决定经营目标；
- 代替完整 Evaluation 过程。

Evaluation 是使用一个或多个 Measure、Policy 和 Observation 对 Agent、Capability、Skill 或 Change 作出判断的过程，不建立并列核心对象。

---

## 11. Observation｜观察记录

**定义**

Observation 是来自外部业务系统、Runtime 或人类交互的不可变事实记录，描述“发生了什么”。

**负责**

- 记录业务结果、Agent 行为、Skill 输出、Tool 响应、错误和人工反馈；
- 保留时间、来源、对象、摘要和 trace；
- 为 Measure、Evaluation 和 Change 提供证据。

**不负责**

- 解释事实意味着什么；
- 被覆盖或修改成预期结果；
- 自动成为正式 Knowledge 或 Policy。

---

## 12. Change｜受治理的变更

**定义**

Change 是一次可审查、可验证、可发布、可观察和可回滚的系统改变。

**负责**

- 描述问题、假设、目标对象、差异、风险和验证标准；
- 记录审批、测试、发布、观察和回滚条件；
- 修改 Business Intent、Policy、Capability Package、Skill、Knowledge、Agent Deployment、Runtime 配置或 Measure；
- 根据评价证据保留或回滚版本。

**不负责**

- 代替 Observation 或普通项目 Issue；
- 绕过审批修改高风险写入方法、参数或范围；
- 将运行候选自动提升为正式资产。

---

# 核心责任链

```text
Business Intent / KPI
        ↓ 委托
Agent Deployment
        ↓ 装载
Capability Package
        ↓ 组合
Skill
        ↓ 调用
Tool / External System
        ↓ 产生
Business Result
```

责任不能向下转移：

- Agent Deployment 对 Workspace KPI 负责；
- Capability 对业务结果契约负责；
- Skill 对执行方法和局部正确性负责；
- Tool 对调用契约负责；External System 对外部事实和响应负责；
- Capability 局部达标不等于 Agent KPI 已达标。

# 横向支撑与反馈

- Knowledge 为 Agent、Capability 和 Skill 提供受控上下文；
- Policy 约束 Agent、Capability、Skill 和 Tool 调用；
- Governance 管理所有资产的验证、版本、发布、影响和回滚；
- 独立反馈链为 `Business Result → Observation → Measure / Evaluation → Change → 新版本`。

# 非核心对象

- Agent Profile：Agent Deployment 的发布前模板或候选；
- Workflow：复合 Skill 的内部编排；
- Evaluation：基于 Measure 的评价过程；
- Decision、Experiment、Release、Rollback：Change 生命周期中的记录、方法或阶段；
- Project、Issue、Milestone、Pull Request、ADR：开发与协作对象；
- Tolaria：可替换 Markdown 管理载体；
- Runtime memory：执行环境的存储机制；
- UI projection：界面展示数据，不建立独立业务生命周期。
