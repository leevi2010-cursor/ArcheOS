# ArcheOS 开发路线图

## 文档职责

本文件只定义 ArcheOS 的阶段演化顺序，不承担单个开发任务的实现规格。

- `AGENTS.md`：定义 Agent 的工作规则与权威关系。
- `docs/architecture/CONCEPTS.md`：定义 Core 概念的唯一权威词典。
- 本 `ROADMAP.md`：定义长期阶段顺序。
- GitHub Issue：定义当前一次开发必须交付什么；复杂 Issue 可以内嵌 Architect 批准的 Implementation Plan 与 Test Cases。
- Durable Spec / ADR：仅在某个稳定契约或架构决策需要被多个 Issue 复用时建立。

---

## M0 — 治理与基础结构（已完成）

目标：建立最小且统一的系统治理方式。

交付：

- `AGENTS.md` 治理规则；
- 信息生命周期目录；
- 系统文档目录；
- `Issue → Approved Plan / Tests → 实现 → PR → 审核` 的协作协议；
- 概念治理机制：优先复用已有概念，新增概念必须先进入权威文档。

---

## M1 — 通用音频信息消化（已完成）

目标：证明 ArcheOS 能把一段未知业务领域的音频，从混乱输入转化为可人工审核的信息包。

第一版输入：

- 会议录音；
- 通话录音；
- 工作讨论；
- 头脑风暴；
- 其他本地音频。

核心流程：

**录音 → 转写 → Speaker Attribution → 上下文保留 → 原子信息候选 + 残渣 → 人工审核**

交付：

- `transcript` 保存原始转写；
- `meeting_summary` 保存整体上下文；
- `atomic_notes` 保存可独立审查、可追溯的信息候选；
- `residue` 保存当前无法安全吸收的信息，并作为信息消化健康度的反馈；
- 自动中性 Speaker diarization；
- Codex model-backed semantic analysis；
- 全 transcript digestion coverage fail-closed；
- 源文件不可变、隐私数据不进入 public repo。

实现：GitHub Issue #4 / PR #5。

---

## M2 — 人工确认与结构化世界模型（当前）

目标：让经过人工确认的信息，从“候选信息”安全进入长期可追溯的 Information + World Model，而不因业务术语变化破坏身份和历史。

M2 使用 `docs/architecture/CONCEPTS.md` 与 ADR-003 的统一模型：

```text
Atomic Information Candidate
        ↓
Human Confirmation
        ↓
       Note
        ↓ supports / concerns
Object + Role + Relationship + Lifecycle + Name
        ↓
Structured World Model
```

### M2-A — Object Identity & Role Foundation

目标：建立稳定的身份和关系基础，不把业务名词固化成彼此平行的底层实体类型。

需要实现/验证：

- stable `Object` identity；
- mutable `Name` / aliases / name history；
- multi-role `RoleAssignment`；
- Role history；
- `Lifecycle` 与 Role 分离；
- typed `Relationship` graph；
- Object Resolver / read model 的最小读取边界；
- `Note` 与 Object 分离。

当前已接受 Roles：

- `person`
- `company`
- `brand`
- `project`
- `business_line`
- `event`
- `goal`
- `decision`

真实验收场景至少覆盖：

- “私享国际家具”可以同时具有 company / brand Role；
- “展厅经营”是 ongoing business_line；
- “海丝金融中心家具采购”是 bounded project；
- 如果一个 Object 从 project 被重新理解为 business_line，Object ID 不变；
- “内部产品库”等长期维护对象可以按需升格为 Object，而普通陈述继续保持 Note。

### M2-B — Human Confirmation & Absorption

目标：定义并实现候选信息的确认、修改、拒绝、不确定保留和安全吸收。

需要解决：

- candidate → confirm / edit / reject / uncertain；
- confirmed candidate → durable Note；
- Note 如何提出 Object 创建/匹配建议；
- Note 如何提出 Role、Relationship、Lifecycle 更新建议；
- 多来源冲突如何保留而不是覆盖；
- 所有结构化变化如何保留来源、Evidence 和历史；
- 人工确认前不得自动改变 durable World Model。

### M2-C — Human Read Model / View Foundation

目标：让稳定 ID 的机器模型可以自然地向人展示，同时不把展示结构变成 Core 真相。

需要支持的架构边界：

```text
Core Graph
  → Projection / View Model
  → Renderer
```

优先 View：

- Object Profile；
- 向阳生长树；
- Relationship Graph；
- Timeline；
- Decision View（可先定义读取边界，完整能力在 M4）。

M2 不要求一次完成正式前端。HTML 图文可以作为早期 renderer / prototype，但 Core Graph 与 View Definition 才是可复用基础。

---

## M3 — Domain Agent（领域解释）

目标：在通用信息消化和 Structured World Model 之上增加业务领域的专门理解，而不污染 ArcheOS Core。

候选领域：

- Sales Agent：客户需求、担忧、决策信号、销售表现、下一步跟进；
- Brand Agent：品牌定位、目标客户、价值主张、差异化、待验证假设；
- Project Agent：项目状态、风险、阻塞、下一步行动。

原则：

- Domain Agent 使用 Core 已产生的上下文、Note 和 Structured World Model；
- Domain Agent 可以生成领域报告或提出 World Model 更新建议；
- 不为每一种录音重新实现一套独立的信息处理 Pipeline；
- 领域业务词优先复用 Name / Role / Relationship / Note / View，不自动升级为新 Core 概念。

---

## M4 — 决策与反馈闭环

目标：让结构化信息真正参与决策，并通过行动结果反过来更新系统。

核心链路：

**Goal → Decision → Action → Feedback**

需要解决：

- Goal Role 的具体行为语义；
- 哪些 Note / Object / Relationship 支持或反对某项判断；
- Decision 如何记录依据；
- Action 与 Feedback 如何连接 World Model 和后续决策。

`goal` 和 `decision` 已经是 Object 可承担的 Role；M4 重点设计行为、依据与反馈闭环，不重新建立平行 Goal/Decision 对象体系。

---

## M5 — 多格式输入

目标：把已经验证过的 Core Processing 扩展到更多输入格式。

候选输入：

- PDF；
- 图片；
- PPT；
- 视频；
- 其他文档与外部信息源。

原则：新增输入格式只扩展“如何进入 Processing”，不复制新的对象体系、决策体系或生命周期。

---

## 演化总原则

ArcheOS 始终沿一条主线演化：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

并遵守：

1. Core 概念以 `docs/architecture/CONCEPTS.md` 为准；
2. 优先复用已有概念，不因为新业务场景新增平行概念；
3. Object ID 稳定，Name / Role / View 可以演化；
4. Core 保存 Graph，人类可通过多个 View 理解同一份数据；
5. 先用真实数据验证当前阶段，再进入下一阶段；
6. 不因为未来可能需要某个能力，就提前建设完整框架。
