# ArcheOS 核心概念词典

## 1. 文档职责

本文件是 ArcheOS **核心概念定义的唯一权威入口**。

它只回答：

- 一个概念叫什么；
- 它表示什么；
- 它不表示什么；
- 它与其他概念有什么区别或关系。

本文件**不定义系统运行时的业务规则**，例如什么时候自动更新、什么时候需要人工判断、是否允许删除等。此类规则以 `docs/product/INFORMATION_GOVERNANCE.md` 为准。

`ARCHITECTURE.md` 说明这些概念如何组成系统；ADR 记录关键架构决策；GitHub Issue 定义当前要实现什么；`AGENTS.md` 约束 Agent 如何使用这些权威来源。

当其他项目、历史文档或旧系统的概念与本文件冲突时：

- 新设计和跨系统映射以本文件为准；
- 旧系统在完成受控迁移前继续保留自己的运行权威，不得只改名称冒充迁移完成；
- 本文件已定义的概念必须优先复用，别名不得再次发展成平行模型；
- 本文件未定义、且只服务某一业务领域的概念，先进入该项目的 `docs/domain/CONCEPTS.md`，不得直接升级为通用概念。

---

## 2. Information

`Information` 表示 ArcheOS 已经从输入中获得、能够被理解和追溯的内容。

Information 层主要包含：

- `Atomic Information Candidate`
- `Atomic Information`
- `Claim`
- `Hypothesis`
- `Evidence`
- `Residue`

Information 描述“我们获得了什么信息”，不等同于长期世界中的 Object，也不等同于系统已经确认的 World Model 状态。

Information Layer **允许彼此矛盾的 Atomic Information / Claim / Hypothesis 并存**。保存“某人说过什么”、系统当前提出什么可检验命题，或“某个来源表达了什么”，都不意味着 ArcheOS 已经把对应内容接受为当前事实。

`Note` **不是 ArcheOS Core 的正式概念**。在旧系统、历史文件或人类界面中遇到 `Note` 时，可以把它识别为 `Atomic Information` 的旧称或展示名称，但不得因此建立独立的 Note 模型、Store、ID 或生命周期。

---

## 3. Source

`Source` 是经过用户明确准入、由系统保存为不可变字节快照，并具有稳定 `source_id` 的正式信息输入。

Source 位于 Input / Information provenance 边界：

- Source 是后续 Processing、Evidence 和 Normalized Representation 的权威输入；
- Source 不是 World Model `Object`，不具有 Object 的业务身份、Role 或 Relationship；
- Source 不因名称、外部路径、当前格式支持或所在目录而获得身份；
- 一个 `source_id` 对应一份不可变 Managed Source 字节快照；
- Source 字节一旦进入 Evidence 链，不得被同一 `source_id` 原地覆盖；
- 新字节内容必须显式重新接入，并创建新的 `source_id`；
- 相同 `content_hash` 可以支持存储去重，但不能自动复用 `source_id` 或合并来源语境。

扫描到的外部文件在用户准入和复制校验完成前只是临时 intake candidate，不是正式 Source。归档完成后，外部文件可以作为可失效的 `ingested_from` 历史线索，但不参与后续 Evidence 定位，也不成为系统长期读取权威。

Source 与以下架构术语的边界如下：

- Managed Source 是 Source 已完成准入、复制和校验后的系统受管形态；
- Archive / storage replica 是 Managed Source 的受管字节位置，不是新的 Source；
- Handoff Marker 是外部目录中的交接说明，不是 Source、Evidence 或同步机制；
- Normalized Representation 是从 Source + `content_hash` 生成的可替换派生表示，不是 Source。

Source 与 Evidence 的关系是“Evidence 回到 Source”，不是 Source 本身等于 Evidence。Source 与 Atomic Information 的关系是“Atomic Information 可以引用 Source 作为依据”，不是 Source 等于信息内容。

---

## 4. Object

`Object` 是现实世界或经营世界中一个**需要长期保持稳定身份的可引用对象**。

Object 的身份由稳定 `object_id` 表示，而不是由名称、目录位置或当前 Role 表示。

Object 可以：

- 有一个或多个 Name；
- 同时拥有多个 Role；
- 拥有 Lifecycle；
- 与多个其他 Object 建立 Relationship；
- 被多个 Atomic Information 描述或引用。

示例：

- 私享国际家具；
- 展厅经营；
- 海丝金融中心家具采购；
- 一个需要长期维护的内部产品库。

一个普通描述、观点或事实本身不是 Object，例如“SKU 很重要”属于 Atomic Information。

---

## 5. Role

`Role` 表示一个 Object **当前或在某段时间内以什么业务身份被理解和使用**。

Role 不是 Object 身份，也不是独立 Object。

当前已接受的 Role：

- `person`
- `company`
- `brand`
- `project`
- `business_line`
- `event`
- `goal`
- `decision`

同一个 Object 可以同时拥有多个 Role。

例如：

```text
私享国际家具
roles:
- company
- brand
```

Role 可以随时间变化。Role 改变不意味着 Object 身份改变。

---

## 6. Name

`Name` 是 Object 面向人的可读名称。

Name 不是 Object 的身份键。

一个 Object 可以具有：

- 当前主要名称；
- 别名；
- 历史名称。

因此，对象改名与“创建一个新的 Object”是两个不同概念。

---

## 7. Lifecycle

`Lifecycle` 描述 Object 在时间上的存在、推进和结束特征。

Lifecycle 与 Role 是不同维度：

- Role 回答“它是什么”；
- Lifecycle 回答“它如何随时间存在和变化”。

Lifecycle 可以包含：

- 开始时间；
- 计划结束时间；
- 实际结束时间；
- 完成条件；
- 当前状态。

例如：

```text
展厅经营
role = business_line
lifecycle = ongoing
```

```text
海丝金融中心家具采购
role = project
lifecycle = bounded
```

---

## 8. Relationship

`Relationship` 表示两个 Object 之间可长期保存、可追溯的有类型关系。

概念结构：

```text
Object A
   ↓ Relationship
Object B
```

Relationship 可以包含：

- 两端 Object；
- relation；
- 生效与结束时间；
- 来源 / Evidence；
- confidence / uncertainty。

ArcheOS 的 World Model 因此天然可以形成 Graph，而不是只能形成一棵唯一目录树。

### 当前已接受的通用 Relationship

第一版只接受以下 5 种通用关系：

- `part_of`：A 是 B 的长期组成部分。例如“产品库 part_of 展厅经营”。
- `member_of`：A 是 B 组织范围中的成员。典型用于 Person 与 Company / Organization 之间的成员关系。
- `responsible_for`：A 对 B 承担明确、持续或阶段性的业务责任。
- `depends_on`：A 的运行、完成或有效性明确依赖 B。
- `related_to`：A 与 B 存在明确、值得长期保留的业务联系，但当前没有必要或还没有足够依据定义更具体的 Relationship 语义。

这些关系都要求两端已经是值得长期保持稳定身份的 Object。某段话中出现两个名词，不意味着必须为了建立 Relationship 而创建两个 Object。

Relationship 的方向属于其语义。例如：

```text
产品库 → part_of → 展厅经营
```

读取或展示时可以把它反向表达为“展厅经营包含产品库”，但反向表达本身不是第二条 durable Relationship。除非未来存在独立业务含义，否则不要为了正反查询方便而重复保存 inverse Relationship。

`related_to` 是刻意保持宽泛的兜底关系，不表示隶属、所有权、责任、因果或依赖。以后只有在真实数据反复证明现有 5 种关系不足时，才通过架构评审增加新的通用 Relationship 语义。

---

## 9. Atomic Information

`Atomic Information` 是一个**可独立理解、可独立追溯的长期原子信息单元**。

Atomic Information 属于 Information 层，不是 Object，也不是 Object 的 Role。

一条 Atomic Information 可以包含：

- statement；
- semantic type；
- concerns / related Object IDs；
- 可选 Claim；
- Evidence；
- context；
- confidence / uncertainty；
- 来源和版本信息。

Atomic Information 可以描述一个或多个 Object，也可以在尚未完成 Object 绑定时独立存在。

Atomic Information 与 Object 的区别：

```text
Object              = 被长期引用的“东西”
Atomic Information  = 关于这些东西的一条最小长期信息
```

Atomic Information 的 `confidence` 默认表示系统对**信息提取 / 语义理解正确性**的置信程度，不自动表示 statement 在现实世界中的真实性概率。

Atomic Information 的后续修订仍属于同一条长期信息身份，并通过 Revision / 历史记录表达变化；Revision 是实现和历史结构，不是新的 Core 概念。

### Claim

`Claim` 表示：**某个主体或某个来源对一条 statement 的声明立场**。

Claim 属于 Information Layer，并作为 Atomic Information 的可选归因结构存在。第一版不为 Claim 建立独立 Object、独立 Store、独立 ID 或第二套生命周期。

Claim 可以表达：

- claimant：谁作出这个声明；若已解析为长期 Object，可引用 `claimant_object_id`；尚未解析时仍可通过 Source / speaker 等来源信息保留归因；
- stance：声明立场，第一版使用 `assert` / `deny` / `uncertain`；
- claimed_at：声明发生时间（若来源可确定）；
- attribution confidence：系统对“是谁说的 / 是否正确归因”的置信度。

Claim 不等于事实，也不等于 World Model 当前状态。

例如：

```text
Atomic Information A
statement: 客户预算约 20 万
Claim:
  claimant: 销售 A
  stance: assert

Atomic Information B
statement: 客户预算至少 30 万
Claim:
  claimant: 设计师 B
  stance: assert
```

A 与 B 可以同时长期保存，即使彼此冲突。冲突如何影响 World Model 属于 Information Digestion / Governance，而不是 Claim 定义本身。

当多个主体独立表达同一内容时，优先保留各自可追溯的 Atomic Information / Claim，而不是为了减少记录而抹平不同来源。

### Hypothesis

`Hypothesis` 表示：**一个尚未被 ArcheOS 当作稳定知识接受、但可以通过后续 Evidence / Feedback 被支持、反对、修订或淘汰的可检验命题。**

Hypothesis 属于 Information Layer，并复用 Atomic Information 的长期身份、Revision、Evidence、context 与 provenance。第一版不建立独立 `HypothesisStore`、独立 Object、独立 ID 或第二套生命周期；实现上应把它作为 Atomic Information 的 canonical 语义形态，而不是平行信息模型。

Hypothesis 可以由 Human 或 Agent 基于一个或多个 Evidence 提出。提出 Hypothesis 不意味着它已经成立，也不意味着其内容已经成为 World Model state。

Hypothesis 与相邻概念的边界：

- `Observation / Evidence` 回答“观察到了什么 / 依据在哪里”；Hypothesis 回答“当前有哪些可被未来事实验证或反驳的解释、预测或条件性命题”；
- `Claim` 回答“谁以什么立场说了什么”；某个 Claim 可以提出 Hypothesis，但归因与命题是否被现实支持是两个维度；
- `Judgment` 表示在当前 Goal / Evidence / Requirement 下作出的判断；Judgment 可以依赖多个 Hypothesis，但不等于这些 Hypothesis；
- `Action` 回答“做什么”；Hypothesis 可以表达“为什么预期该 Action 会产生某种 Outcome”；
- `Decision` 是 Human 受治理确认的取舍；Decision 可以依赖 Hypothesis，但不会把 Hypothesis 自动升级为事实；
- `Pattern / Protocol / Policy / Principle` 是更稳定、可复用的方法或治理结构；被反复验证的 Hypothesis 可以为它们的新版本提供依据，但不能通过原地改类型的方式“变身”为这些概念。

影响 Judgment / Decision 的关键 Hypothesis 应能够追溯到 supporting Evidence、challenging Evidence、预期可观察结果与后续 Feedback。Hypothesis 被现实支持到什么程度，**不得复用 `Atomic Information.confidence` 表示**；后者继续只表示信息提取 / 语义理解正确性的置信程度。第一版优先保留支持/反对 Evidence、验证结果、适用条件和 Revision，不提前制造“真实性概率”。

### Atomic Information semantic types

`semantic_type` 是 Atomic Information 的**语义标签**，不是一组拥有独立 Store、ID、生命周期或基类的平行 Core concepts。

当前 production 已实现并验证的 vocabulary 为：

- `observation`：来源明确表达或系统提取到的观察 / 状态描述；
- `preference`：主体对选择、结果或方式的偏好；
- `requirement`：必须满足、必须避免或构成边界条件的要求；业务界面可以自然表达为“要求 / 约束”，但 Core 不另建 `constraint` semantic type；
- `judgment`：主体基于当前信息作出的判断、评估或推荐；
- `decision`：已经作出的选择 / 决定的原子记录；需要长期身份、责任、状态和复盘时，可进一步由 Object + `decision` Role 表达正式 Decision；
- `commitment`：主体已承诺承担或完成的事项；
- `action`：已经发生、正在发生或明确提出的动作；
- `question`：仍需回答或调查的问题；
- `other`：重要但当前不适合更具体标签的信息。

这些 label 的边界属于现有 production contract。以后若要合并、重命名或新增已持久化 semantic type，必须按**已开发兼容迁移**处理，不能只改文档导致历史 Atomic Information 失去可读性。

`Hypothesis` 已是 canonical Information 语义，但当前 production `SEMANTIC_TYPES` 尚未实现对应 label。未来实现 Hypothesis 时，应扩展同一 Atomic Information semantic mechanism，并先定义兼容 / schema 变更；不得为 Hypothesis 建立第二个 Store 或生命周期。

---

## 10. Atomic Information Candidate

`Atomic Information Candidate` 是 Processing 阶段产生的、尚未进入长期 Atomic Information 层的原子信息候选。

它与 durable Atomic Information 的主要区别是所处生命周期阶段：

```text
Processing
  → Atomic Information Candidate
  → Atomic Information
```

候选信息仍应保留 statement、Evidence、context、confidence / uncertainty 等可追溯信息。若来源已经明确表达声明主体与立场，Candidate 可以携带 Claim candidate 信息，后续进入同一 Atomic Information 生命周期，而不是建立平行 Claim 生命周期。

---

## 11. Evidence

`Evidence` 是 Information 或 World Model 认知回到原始来源的可追溯依据。

对于音频，Evidence 可以包含：

```text
source
→ transcript segment
→ speaker
→ timestamp
→ excerpt
```

Evidence 与 Atomic Information 不同：

- Atomic Information 表示“系统记录了什么信息”；
- Claim 表示“谁以什么立场表达了这条信息”；
- Hypothesis 表示“当前有哪些可检验但尚未成为稳定知识的命题”；
- Evidence 表示“这条信息、归因或 Hypothesis 依据什么来源”。

---

## 12. Residue

`Residue` 是 Processing 阶段当前无法安全转化为结构化信息的内容。

Residue 可能来自：

- 歧义；
- 上下文不足；
- 冲突；
- 证据不足；
- 重要性暂时无法判断。

Residue 不是运行错误，也不是垃圾。

---

## 13. Structured World Model

`Structured World Model` 是对以下长期结构化内容的统称：

```text
Object
+ Name
+ Role
+ Lifecycle
+ Relationship
```

它不是需要单独创建 ID 的 Object，而是一个架构层概念。

Atomic Information / Claim / Hypothesis / Evidence 描述和支撑 World Model；World Model 表达 ArcheOS 对长期经营世界的**当前受治理认知**。

Information Layer 可以保留相互矛盾的 Claim / Hypothesis；World Model 不需要把每个 Claim 或 Hypothesis 都复制成结构化事实。

---

## 14. Projection

`Projection` 表示为了某种理解目的，从同一份 World Model 中选择、组织和计算出一个观察结果的过程。

Projection 不改变 Core Data 本身。

---

## 15. View

`View` 是对同一份 Core World Model 的一种**人类理解视角**。

View 不是新的 Core Object。

例如：

- Object Profile；
- 向阳生长树；
- Relationship Graph；
- Timeline；
- Decision View。

同一批 Object 可以在不同 View 中呈现不同结构。

例如“向阳生长树”可以显示：

```text
私享国际家具
├─ 展厅经营 [business_line]
│  ├─ 产品库
│  └─ 销售工具
└─ 海丝金融中心家具采购 [project]
```

这棵树是一种 View，不代表 Core 中只有这一种真实关系结构。

---

## 16. View Model

`View Model` 是根据某个 View / Projection 从 Core Data 得到的、适合展示层读取的数据结构。

它是 Core Data 与 Presentation 之间的读取模型，不是 Core 数据权威。

---

## 17. Presentation

`Presentation` 表示最终面向人的呈现方式。

例如：

- HTML；
- Markdown；
- React Web；
- Mobile；
- AI conversation。

Presentation 不是 Core Data，也不承担长期 Object / Relationship 的权威存储职责。

---

## 18. Object Resolver

`Object Resolver` 是读取时根据稳定 `object_id` 获取当前人类可读 Object 信息的机制。

例如可以解析出：

- 当前名称；
- 当前 Role；
- aliases；
- status；
- Lifecycle 摘要。

Object Resolver 是读取机制，不是新的 Domain Object。

---

## 19. 当前示例

### 私享国际家具

```text
Object A
name: 私享国际家具
roles:
- company
- brand
```

### 展厅经营

```text
Object B
name: 展厅经营
role: business_line
lifecycle: ongoing
```

### 海丝金融中心家具采购

```text
Object C
name: 海丝金融中心家具采购
role: project
lifecycle: bounded
```

A、B、C 之间的业务联系通过 Relationship 表达。

---

## 20. 概念别名与收敛规则

ArcheOS 使用一个 canonical 概念体系。别名只帮助识别**已经存在的历史资料、旧代码、旧 API / package 或不同系统的既有说法**，不拥有独立定义，也不得在新设计中作为未开发规划词继续存在。

对尚未开发的设计：应直接使用 canonical concept；如果现有概念确实不足，应先完成 `CONCEPTS.md` 的 concept change，再进入实现。只有已经落地、需要兼容或迁移的旧名称才进入下面的映射。

| Canonical 概念 | 可识别旧称 / 别名 | 收敛规则 |
| --- | --- | --- |
| Object | Entity、Business Object、业务对象 | 都表示需要稳定身份的可引用对象；数据库 `entity` 只是实现名称 |
| Relationship | Relation、业务关系 | `Edge` 只作为图存储实现术语；超链接不自动成为 Relationship |
| Name | Label、Display Name、名称 | 都不承担 Object 身份 |
| Atomic Information | Atomic Note、Note、Durable Atomic Information、已确认 Semantic Unit | 都收敛为长期 Information 层的 Atomic Information；新代码不建立 Note 模型 |
| Claim | Assertion、Statement Attribution、声明 | 表示某主体 / 来源对 Atomic Information statement 的声明立场；不建立独立事实层 |
| Atomic Information Candidate | Atomic Information Unit Candidate、Semantic Unit Candidate | 表示尚处于 Processing 的原子信息候选 |
| Evidence | Evidence Ref、Citation | 作为来源依据；精确片段使用 Evidence Fragment |
| Structured World Model | Core、World Model、长期结构化认知 | 都指 Object / Name / Role / Lifecycle / Relationship 的组合，不是新 Object |
| View | Operating View、业务视图 | 只负责观察与组织，不成为 Core Truth |
| View Model | Read Model、Projection Result | 是给展示层读取的数据结构，不是长期权威 |
| Business Line | Operation、经营主线、长期经营责任 | 表达持续经营，不使用“长期 Project”建立第二套 Project 语义 |
| Todo | Action Item、执行待办 | 表达 Issue 下的具体动作；Task 是否映射为 Issue 或 Todo 取决于是否需要独立追踪 |

以下泛化词不得单独成为新 Core 概念：

- `Semantic Object`：若指稳定事物，使用 Object；若指一条意义，使用 Atomic Information Candidate 或 Atomic Information。
- `Candidate`：只表示某类内容尚未晋升的状态，必须说明是 Object、Information、Relationship、Revision 还是 Action 的候选。
- `Artifact`：必须说明是原始 Source、Derived Artifact，还是 World Model 中具有稳定身份的业务产物。
- `Record`：必须说明记录的业务语义，不能用它回避概念定义。

---

## 21. 信息生命周期补充概念

ArcheOS 的 canonical 生命周期仍是：

```text
Input → Processing → Atomic Information → Structured Object → Decision → Feedback
```

以下概念用于精确描述各阶段，不新增平行生命周期。

### Inbox

`Inbox` 是尚未完成来源登记的临时输入区。内容可能重复、无效、缺少权限或无法处理，因此不是长期事实权威。

### Derived Artifact

`Derived Artifact` 是从 Source 经处理产生的转写、摘要、解析结果或其他中间产物。它不得覆盖原始 Source，并应保留 Processing Run 与版本来路。

### Evidence Fragment

`Evidence Fragment` 是 Source 或 Derived Artifact 中可精确读回的位置，例如页码、时间段、表格行或文本片段。

Evidence Fragment 负责回答“依据在原文哪里”；Atomic Information Candidate / Atomic Information 负责回答“这段依据表达了什么”。一个 Fragment 可以支持多条信息，一条信息也可以引用多个 Fragment。

### Processing Run

`Processing Run` 是一次可审计的处理尝试，记录输入版本、处理器、模型或工具版本、开始和结束时间、结果与失败状态。它是运行来路，不是业务 Object。

### Change Proposal

`Change Proposal` 是基于 Atomic Information Candidate / Atomic Information 建议修改长期 World Model 或正式业务记录的受控提案。它不是 Decision，也不是已经执行的事实。

### Feedback

`Feedback` 是行动或写入后重新获得的现实状态和结果，用于修正下一轮 Atomic Information、Hypothesis、判断、Decision 或行为。

---

## 22. 经营与项目治理概念

这些概念用于组织经营推进。它们可以通过 Object、Role、Lifecycle、Relationship 和 View 实现，但业务含义保持一致。

### Workspace

`Workspace` 是长期存在的经营、权限和数据隔离范围。Workspace 可以围绕公司、店铺、个人或其他经营主体建立，但它本身不等于 Company、Store 或 Domain。

### Goal

`Goal` 是希望达到、并可用于判断行动方向的结果状态。

- 一次性提到的目标性表达可以先作为普通 Atomic Information 保存，不要求为了“出现一个目标句子”立即新增 Goal Object；
- 当该目标需要长期责任、状态、关系、完成条件和复盘时，建立 Object 并赋予 `goal` Role；
- `Vision` 是更长期的 Goal 层级；`Objective` 作为 Goal 别名，不新增概念。

### Roadmap

`Roadmap` 是 Workspace 基于 Goal、现实、资产、Requirement 和 Decision 形成的长期路径 View。它不是 Project 内部必须存在的层级，也不作为独立 Core 身份重复保存底层对象。

这里的业务 `Roadmap` 是 World Model / View 语义；仓库中的 `PRODUCT_ROADMAP.md` / Development Roadmap 是开发治理文档，不因此创建运行时 Roadmap Object。

### Project

`Project` 是为了明确成果组织、具有完成条件并可以结束的工作。在 World Model 中使用 Object + `project` Role + bounded Lifecycle 表达。

### Business Line

`Business Line` 是没有预设结束时间的持续经营责任。在 World Model 中使用 Object + `business_line` Role + ongoing Lifecycle 表达。

### Milestone

`Milestone` 是 Project 或 Roadmap 中可验收的阶段成果，不是普通任务清单。

### Issue

`Issue` 是需要独立负责人、状态、证据、验收或 Decision 的事项包。Incident、Risk、Blocker、Decision Request、Work Order 和 Task 可以作为 Issue 的 kind，而不是建立平行推进层级。

### Todo

`Todo` 是 Issue 下具体、可执行、可完成的小动作。无需独立治理和证据链的 Task 映射为 Todo；需要独立追踪的 Task 映射为 Issue。

推荐推进关系：

```text
长期经营：Workspace → Roadmap → Milestone → Issue → Todo
有限交付：Project   → Milestone → Issue → Todo
```

---

## 23. 系统设计概念

### System、Subsystem、Module、Component

```text
System → Subsystem → Module → Component
```

- `System`：在明确边界内，由人、规则、数据、软件和工具协同实现持续目标的整体。
- `Subsystem`：System 内具有相对独立责任的结构分区。
- `Module`：高内聚、接口明确、可独立测试和版本化的主责结构单元。
- `Component`：Module 内实现部分责任的结构单元。

### Business Process、Function、Action

```text
Business Process → Function → Action
```

- `Business Process`：业务从触发到结果的完整行为链。
- `Function`：系统能够完成的业务能力；人类页面可以显示为“系统能力”或 `Capability`。
- `Action`：Function 对现实或系统状态产生的具体行为。

结构维度与行为维度不得混用。流程阶段不是 Domain，Function 不是顶层流程，Module 也不是业务步骤。

### Cell-like Module

`Cell-like Module / 类细胞模块` 是 Module 采用的闭环运行 Pattern，不是新的结构层级。`闭环单元`、`类细胞单元`统一映射为“采用类细胞运行模式的 Module”。

Module 可以由确定性代码、Agent 或二者组合实现；是否调用模型不改变 Module 身份。

---

## 24. 运行、治理与反馈概念

| 概念 | 定义 |
| --- | --- |
| State | 对象或 Module 在某时点的业务或内部状态 |
| Status | 某治理或执行生命周期中的受控标签 |
| Health | 对某 Module 或来源能否正常履责的评价 |
| Freshness | 数据或版本是否仍满足当前时效要求 |
| Signal | Module 向环境发布、供其他消费者关注的信息或变化提示 |
| Event | Object 状态或 Relationship 发生的可识别变化 |
| Consumption Receipt | 某消费者已经处理某条 Signal / Atomic Information 的记录 |
| Decision Receipt | 人对候选作出接受、拒绝、补证或延后选择的记录 |
| Audit Event | 系统发生受控动作的不可变审计记录 |
| Readback | 写入后从权威来源重新读取并核对的验证动作 |

State、Status、Lifecycle、Health、Freshness 不得互相替代。Signal、Feedback、Receipt、Audit Event 也不得统一称为一个泛化“回执”。

### Constitution、Principle、Policy、Protocol、Pattern

- `Constitution`：最高层、稳定、跨任务的治理基线。
- `Principle`：用于判断取舍的稳定原则。
- `Policy`：某一明确范围内版本化、可执行的业务参数和要求。
- `Protocol`：跨任务可复用的交互、判断、门禁与流转规则。
- `Pattern`：反复问题对应的可复用解决结构。

这些概念不能统一叫“规则”。Policy 不承载通用方法，Protocol 不写死单一业务参数，Pattern 不替代 Module。

---

## 25. Asset、Artifact、Product 与 System

- `Asset` 是某个 Goal 语境下，可被复用并产生收益或降低风险的 View / 角色，不是互斥的根 Object 类型。
- `Artifact` 是被创建或维护、具有稳定身份的产物 Object。
- `Product` 是 Artifact 面向用户和价值交换时的业务 Role / Classification。
- `System` 是实现持续目标的结构整体或 Artifact 的功能 Classification。

同一个软件 Artifact 可以同时被理解为 Product 和 System，并在某个 Goal 下进入 Asset View；这不应创建三个重复 Object。

`Deliverable` 表示 Project / Milestone 承诺交付的 Artifact 或结果；`Result / Outcome` 表示行动后的现实业务状态。交付文档不等于获得业务结果。

---

## 26. 领域概念扩展

当本文件不能表达某个项目的真实业务含义时，允许新增领域概念，但必须先保留在项目范围内。

每个需要扩展概念的项目使用：

```text
<project-root>/docs/domain/CONCEPTS.md
```

领域概念文档中的每个概念至少说明：

- canonical 名称与中文名称；
- 定义；
- 不表示什么；
- 别名和旧说法；
- 与 ArcheOS 通用概念的关系；
- 适用 Domain / Workspace / Project；
- 至少一个真实例子；
- 当前状态：Candidate / Approved / Deprecated；
- 来源、owner 和复盘条件。

领域概念不得：

- 重定义本文件已有概念；
- 因方便写代码而新增名词；
- 自动传播到其他项目；
- 自动晋升为通用概念。

当同一领域概念在多个项目或 Domain 中稳定复用、且现有通用概念确实无法承载时，可以提出通用概念修订。晋升必须更新本文件并形成 ADR / Decision；晋升完成后，原领域文档保留别名、来源和迁移映射。
