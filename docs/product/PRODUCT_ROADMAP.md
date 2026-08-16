# ArcheOS 产品演化路线图

## 1. 文档职责

本文件定义 ArcheOS **从产品假设走向可重复、可商业化产品时，依次需要证明什么**。

它不按功能数量衡量进度，也不直接规定 PDF、微信、数据库、UI、SaaS 等技术实现顺序。技术能力是否值得开发，必须回到当前 Product Stage 的验证目标与证据缺口。

权威关系：

```text
docs/product/PRODUCT_SPEC.md
  定义：我们最终想成为什么、为谁创造什么价值、长期边界是什么
        ↓
docs/product/PRODUCT_ROADMAP.md
  定义：为了成为那个产品，依次必须证明什么
        ↓
docs/development/ROADMAP.md
  定义：为了通过当前 Product Stage，系统还缺什么能力与验证
        ↓
GitHub Issue
  定义：这一轮具体交付什么
        ↓
PR / Experiment / Real-world Validation
  产生：新的产品与技术证据
        ↓
Roadmap Feedback
  允许：证据反向修正上层假设与路线
```

`CONCEPTS.md`、`INFORMATION_GOVERNANCE.md` 与 ADR 是横向约束，不是另一套 Roadmap。

Product Roadmap、Product Stage、Stage Gate、Roadmap Feedback 都是**仓库治理术语**，不是 ArcheOS Core concept；不得因此建立运行时 ID、Store、生命周期、API 或第二套业务 truth。

---

## 2. 长期产品假设

ArcheOS 的长期方向是成为一套：

- **用户拥有**的长期认知资产，而不是被单个模型或平台锁定的黑盒记忆；
- **模型无关**的认知底座，可以被 Codex、GPT、Claude、本地模型或未来 Agent 使用；
- **来源可追溯**的信息系统，重要认知可以回到 Source、Evidence、时间、责任主体与历史版本；
- **受治理且可演化**的长期认知系统，允许修订、冲突、时间变化、不确定和人工裁决，而不是把模型推断直接升级为事实；
- **服务真实判断与行动**的系统，最终价值不止是保存资料，而是让人和外部 Agent 在更可靠的 Context 上进行判断、决策、行动与反馈。

长期产品结构优先保持：

```text
ArcheOS Core
长期信息、Evidence、World Model、Context、治理与审计
        ↓
External Agent
执行理解、推理、建议与受授权操作
        ↓
Domain Product
围绕一个明确用户问题提供可直接使用的产品体验
```

ArcheOS Core 不要求普通业务用户理解 Atomic Information、Representation、Store 或其他实现细节。真正面向客户售卖的产品可以是 Founder、Sales、Project、Research、Operations 等领域产品，但这些只是候选方向，必须由真实需求与证据选择，不预先承诺。

---

## 3. 产品化与商业化的关系

本 Roadmap 同时覆盖产品化与商业化，但当前不拆出独立 Go-to-Market Roadmap。

- **产品化**回答：一个能力能否被真实用户稳定、重复、低门槛地使用并得到价值。
- **商业化**回答：是否存在明确客户愿意持续付费，以及获客、交付、支持与成本是否可持续。

商业化不能替代产品价值验证；同样，技术能力成立也不等于形成了产品。

只有当外部使用与付费验证成为独立的大规模工作流时，才考虑建立单独的 Go-to-Market 规划。当前不得为了“以后可能商业化”提前建设 billing、multi-tenant、完整 SaaS 控制台或复杂企业基础设施。

---

## 4. Roadmap 的运行规则

每一个 Product Stage 都必须回答：

1. 当前核心假设是什么；
2. 这一阶段必须证明什么；
3. 什么 Evidence 才足以通过 Stage Gate；
4. 什么现象会反驳当前假设；
5. 当前明确不做什么；
6. 通过后，下一阶段要验证什么。

Product Stage 不是固定发布日期，也不是瀑布式项目计划。允许为了降低关键不确定性做有限的前置实验，但不能因为“未来肯定会需要”就绕过当前 Stage Gate 建设完整框架。

任何重大新能力进入 Development Roadmap 前，都应优先回答：

> 它正在关闭当前 Product Stage 的哪个 Evidence Gap？

如果答案不清楚，默认不进入当前主线。

---

## 5. Stage 0 — 产品命题成立（已形成，持续可挑战）

### 核心假设

长期、可追溯、受治理、模型无关的认知底座，对个人和组织具有独立于单次 AI 对话的价值。

### 当前已有依据

ArcheOS 已形成稳定的核心方向：Input → Information → Structured World Model → Context → Decision / Feedback，并明确不把 Agent 推断自动当成长期事实、不把 Core 绑定到单一 External Agent 或 Domain Product。

### Stage Gate

这一阶段不要求市场成功，而要求：

- 产品定义与边界足够清楚，可以指导技术取舍；
- 系统能够选择一个真实场景验证，而不是只能停留在抽象愿景；
- 产品命题没有被真实使用直接证伪。

### 反证信号

如果长期认知治理无法产生比普通文件夹、聊天记录或简单 RAG 更高的实际价值，产品命题必须重新评估。

---

## 6. Stage 1 — 证明“长期认知”真实成立（当前）

### 核心假设

当真实、异构、持续变化的信息不断进入系统后，ArcheOS 能够长期保持：

- 重要信息不丢失；
- 来源与 Evidence 可追溯；
- 重复与派生不会无限制造噪声；
- 时间变化不会被误当成重复；
- 冲突与不确定不会被静默覆盖；
- 长期 Object 不会因名称变化、模型猜测或重复输入而持续分裂；
- Context 会越来越有用，而不是随着数据增长越来越混乱。

### 必须获得的 Evidence

至少需要真实、持续、多来源的数据验证，并能人工回答：

- 系统现在认为某个长期对象“是什么、发生过什么、现在是什么状态”；
- 每个重要结论为什么成立、来自哪里；
- 哪些信息只是派生、重复、时间更新、冲突或未确定；
- 当系统理解错误时，是否可以修正而不破坏历史；
- 数据规模增长后，纠错、整理和治理成本是否仍可接受；
- 对真实工作而言，ArcheOS Context 是否显著比原始文件、聊天历史或临时摘要更可用。

Stage Gate 不以 synthetic tests 替代真实语义验证。任何未解决的信息丢失、provenance 错误、错误 World Model 写入或错误合并独立 Evidence 的 P0/P1 问题都会阻止通过。

### 当前 Development Gap 的来源

当前 Development Roadmap 中的 Managed Source、多格式 Representation、Conversation Ingestion、Information Consolidation、Object Emergence、Context 与真实旧数据压力测试，只有在它们帮助证明上述假设时才具有当前优先级。

具体技术顺序仍由 `docs/development/ROADMAP.md` 管理，本文件不绑定实现方案或 Issue 编号。

### 反证信号

以下情况会要求重新评估产品命题或架构：

- 信息越多，系统错误、重复、冲突隐藏或上下文噪声持续上升；
- 用户需要大量手工维护才能维持正确认知；
- provenance 无法稳定保留，重要结论不可解释；
- 长期 Context 对真实工作没有比普通搜索 / RAG / 文件整理更明显的价值；
- 系统为了“结构化”而频繁制造错误对象、错误关系或错误事实。

### 当前不做

- 不以 Web UI、SaaS、多租户、billing 或完整团队协作为阶段成功标准；
- 不为了展示产品完整度提前建设大而全的平台；
- 不开发 ArcheOS 自有 Agent 来掩盖底层认知质量问题。

---

## 7. Stage 2 — 证明认知能够改善判断与决策

### 核心假设

受治理的长期 Context 不仅能“记住”，还能够让 Human + External Agent 在真实问题上做出更有依据、更可追溯、更容易复盘的 Judgment / Decision。

### Stage Gate Evidence

使用多个真实、边界清楚的决策场景，验证：

- Context 能提供相关 Goal、Evidence、历史 Decision、Requirement、Preference、Hypothesis、Pattern 与未确定事项；
- Agent recommendation 与 Human Decision 清楚分离；
- 关键 Hypothesis、候选 Action、反证和不确定性可以被检查；
- 决策后 Feedback 能重新进入同一信息生命周期，并能够支持、反对或修订相关 Hypothesis；
- 用户认为这套流程比“临时把背景再讲给 AI”更有效；
- 能观察 Human edit / reject / defer、missing evidence、goal misalignment、rationale traceability、feedback traceability 等指标，而不是制造单一“AI 决策准确率”。

### 反证信号

- 长期 Context 很完整，但没有改善实际判断质量或效率；
- Protocol 只增加流程负担，没有减少遗漏或错误；
- 用户频繁忽略系统建议，且无法从 Evidence / Feedback 找到可改善原因。

### 当前不做

- 不追求自主决策；
- 不自动批准 consequential Goal / Decision；
- 不把模型私有 chain-of-thought 作为产品能力。

---

## 8. Stage 3 — 形成第一个明确的垂直产品

### 核心假设

ArcheOS Core 可以隐藏在一个明确 Job-to-be-Done 后面，让用户购买“问题被解决”，而不是购买一套信息治理架构。

### Stage Gate Evidence

从真实使用中选择一个价值最明确的 Domain Product，并证明：

- 用户问题、使用场景、输入和期望结果都足够具体；
- 用户不需要理解 ArcheOS Core 术语就能完成主要工作；
- 从信息进入到可行动结果形成稳定端到端体验；
- 价值会重复出现，而不是一次性的 demo；
- 与通用 ChatGPT / 文件搜索 / 普通知识库相比存在明确差异；
- Domain Product 复用同一 Core，不重新建立自己的长期 information truth。

Founder / Sales / Project / Research / Operations 等都只是候选，不在本阶段开始前预先锁定。

### 反证信号

- 只有了解 ArcheOS 架构的人才能使用；
- 每个客户都需要大量定制才能产生价值；
- 垂直产品必须绕过 Core 才能工作；
- 用户认可技术能力但没有高频、明确的问题愿意持续使用。

---

## 9. Stage 4 — 证明价值可以被其他用户重复获得

### 核心假设

产品价值不依赖 Leo、Architect 或开发者本人长期陪伴才能成立。

### Stage Gate Evidence

以小规模外部用户验证为主，目标规模可从数名真实用户起步，重点不是数量而是重复性：

- 新用户能够完成安装 / onboarding / 数据接入并理解核心结果；
- 用户自己的真实信息进入后仍能保持隐私、provenance 和治理边界；
- 用户在没有开发者持续解释的情况下能够完成核心任务；
- 多个用户获得相似的核心价值，而不是每个人得到完全不同的定制服务；
- 支持、纠错、初始化和运行成本可被观察并逐步下降；
- 出现持续使用 / 留存信号。

### 反证信号

- 每个用户都需要深度人工部署或长期陪跑；
- 数据差异导致系统可靠性无法复现；
- 核心价值高度依赖创建者个人背景知识。

---

## 10. Stage 5 — 商业验证

### 核心假设

存在明确购买者愿意持续为已验证的产品价值付费。

### Stage Gate Evidence

- 有真实付费试点或等价的强购买承诺；
- 能说明谁是 user、谁是 buyer、为什么现在付钱；
- 价格与客户获得的价值之间存在可解释关系；
- 至少出现续费、扩展使用或其他持续付费信号；
- 模型调用、部署、支持、存储、合规等成本不会吞掉核心价值；
- 隐私、数据所有权、模型 / 云服务边界可以在商业合同中清楚表达。

### 商业模式候选，而非当前承诺

未来可能包括：

- Local-first / self-hosted Core；
- 多设备同步与托管 Workspace；
- 团队共享、权限、审计和企业 Connector；
- Managed infrastructure；
- Domain Product / Pattern / Protocol / 行业能力；
- 实施、迁移或高价值专业服务。

是否开源 Core、哪些能力免费、哪些能力收费，必须在真实外部使用与付费证据出现后决定，不在当前阶段提前锁死。

### 反证信号

- 用户愿意用但不愿付费；
- 价值无法对应到明确预算；
- 支持 / 部署 / 模型成本显著高于可接受收入；
- 购买依赖一次性咨询而非产品本身。

---

## 11. Stage 6 — 可规模化交付

### 核心假设

已经验证的价值与商业模式可以在不同比例的客户中稳定交付，而不会随着客户数量增加线性增加人工成本和风险。

### Stage Gate Evidence

根据前面阶段的真实瓶颈再决定需要哪些能力，通常可能包括：

- 更成熟的 onboarding、diagnostics、upgrade 与 recovery；
- 跨设备 / 团队共享与明确 writer authority；
- 权限、审计、合规与企业治理；
- 可观察的成本、可靠性、SLA 与支持流程；
- 可重复的获客、销售、部署、续费路径；
- Domain Products 与 Core 的稳定版本边界。

这些能力不能因为“成熟 SaaS 都有”而提前进入主线，必须由前面阶段的真实 Evidence 触发。

---

## 12. 当前状态

当前 Product Stage：

> **Stage 1 — 证明“长期认知”真实成立。**

当前开发的首要问题不是“ArcheOS 还缺哪些常见软件功能”，而是：

> 哪些证据仍不足以证明系统在真实、长期、持续变化的信息环境下能够保持准确、可追溯、可治理，并产生越来越有用的 Context？

`docs/development/ROADMAP.md` 应围绕这个问题安排当前技术主线。

当 Ready backlog 需要补充时，ChatGPT Product / Technical Lead 必须先检查当前 Product Stage 的 Evidence Gap，再创建或重排 Development Roadmap / Issues；不得仅依据技术完整度补功能。

---

## 13. 自下而上的 Roadmap Feedback

Roadmap 不是单向命令链。Experiment、Issue、PR、真实使用和失败都可以产生反向反馈。

建议在需要时使用以下最小结构：

```markdown
## Roadmap Feedback

Observation:
实际观察到了什么？

Evidence:
依据是什么？

Affected Stage / Assumption:
影响 Product Roadmap 中哪一阶段或哪条假设？

Suggested Change:
建议继续、调整、提前、推迟、缩小还是放弃什么？

Decision:
keep | review | revise
```

权限边界：

- Codex Executor / Developer、实验 Agent 可以提出 Evidence-backed Roadmap Feedback；
- ChatGPT Product / Technical Lead 负责判断它是否只是实现问题、Development Roadmap 调整，还是已经影响 Product Roadmap；
- 在已批准 Product Stage 内的普通技术重排可以由 ChatGPT Product / Technical Lead 维护；
- 改变产品定义、目标用户、Stage Gate、产品边界、重大商业化方向或进入 / 退出 Product Stage，必须由 Product Owner 做最终产品判断；
- 未批准的反馈不得被 Executor 悄悄实现为新的产品方向。

---

## 14. Review 与更新触发

Product Roadmap 不按固定日期机械更新。至少在以下事件发生时重新检查：

- 当前 Stage Gate 获得重要新 Evidence；
- 真实数据或真实用户出现与核心假设冲突的结果；
- 准备启动一个明显超出当前 Product Stage 的大型能力；
- Development Roadmap 需要大幅重排；
- Ready backlog 需要补充下一批工作；
- 准备宣布进入下一 Product Stage。

每次检查的默认问题是：

> 新证据让我们更相信原来的产品路线，还是应该修正它？

Roadmap 的稳定来自明确的产品命题与 Stage Gate，而不是拒绝变化。
