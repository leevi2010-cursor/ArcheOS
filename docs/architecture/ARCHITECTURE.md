# ArcheOS 系统架构说明

> 产品名称：**向阳经营系统（Sunward Operating System）**  
> 当前工程 / 仓库代号：`ArcheOS`。ArcheOS 用于重构迁移阶段；完成替代后，对外仍使用“向阳经营系统”。

## 文档职责

本文件只说明 canonical concepts 如何组成系统边界与数据流，不定义产品阶段，不替代 `PRODUCT_ROADMAP.md`，也不承担单个 Issue 的实现规格。

权威分工：

- `docs/product/PRODUCT_SPEC.md`：产品长期是什么；
- `docs/product/PRODUCT_ROADMAP.md`：产品依次必须证明什么；
- `docs/development/ROADMAP.md`：为了当前 Product Stage 还缺什么技术能力 / Evidence；
- `docs/architecture/CONCEPTS.md`：canonical concepts；
- `docs/product/INFORMATION_GOVERNANCE.md`：运行时产品行为规则；
- ADR：关键架构决策与原因；
- 本文：这些边界如何连接。

## 当前版本化架构图

最近一版完整图形快照仍为 `v0.2.0`（M2 Target Architecture，2026-08-11）：

- 系统架构图：`docs/architecture/diagrams/v0.2.0/system-architecture-v0.2.0.svg`
- 数据流图：`docs/architecture/diagrams/v0.2.0/data-flow-v0.2.0.svg`
- 版本说明：`docs/architecture/diagrams/v0.2.0/README.md`
- 版本索引：`docs/architecture/diagrams/README.md`

`v0.2.0` 早于 Claim、Hypothesis、首批 Relationship vocabulary、Context Builder、Managed Source、Normalized Representation、多格式 Adapter 和 External Agent MCP 接入。当前文字权威以 `CONCEPTS.md`、本文件、`INFORMATION_GOVERNANCE.md` 与已 Accepted ADR 为准；旧 SVG 只作为历史架构快照，不覆盖当前文字契约。

下一版图形快照应在 Stage 1 的 Representation / Conversation / Consolidation 边界进一步稳定后统一重画，避免为了每个 Issue 频繁改图。

---

## 1. 核心生命周期

ArcheOS 只保留一条主生命周期：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

其中：

- Claim 是 Atomic Information 的可选归因结构，不建立第二条生命周期；
- Hypothesis 是 Atomic Information 的 canonical 语义形态，复用 Atomic Information identity / Revision / Evidence，不建立第二条生命周期；
- `Structured Object` 表示进入长期结构化世界模型的阶段，不意味着系统采用互斥的 Person / Company / Project 基础表；
- Context、Projection、View 和 Derived Artifact 是读取 / 处理能力，不成为第二份 truth。

核心概念定义以 `docs/architecture/CONCEPTS.md` 为准；信息吸收和长期认知更新的业务规则以 `docs/product/INFORMATION_GOVERNANCE.md` 为准。

---

## 2. Input、Source 与 Processing

Input 可以来自音频、Markdown、PDF、XLSX、PPTX、图片、Conversation 以及未来其他渠道。正式 `Source` 必须经过用户明确准入、完整字节复制以及 `size` / `content_hash` 校验后才成立。

Source 的权威边界：

```text
外部输入
  = 临时 intake candidate / 可失效接入线索
  ↓ 用户明确准入 + 完整字节校验
Managed Source
  = stable source_id + immutable managed bytes
  ↓
Normalized Representation / audio processing artifact
  ↓
Evidence
  ↓
Atomic Information
```

Managed Source 是后续 Processing、Evidence 和 Normalized Representation 的唯一 Source 权威。外部文件准入完成后不再被系统自动跟踪、同步或重新处理；用户修改外部文件后若要进入系统，必须显式重新准入并创建新的 Source。已被 Evidence 引用的 Source 字节不得由同一 `source_id` 原地覆盖。

`content_hash` 用于完整性校验和存储去重，不单独定义 `source_id`，也不自动合并不同接入语境。Archive replica、TOS replica 和 Handoff Marker 都不是新的 Source。

第一版本地 Managed Source 根目录为 `01_inbox/` 的受控 Source 区；实际字节和 Manifest 保持本地、Git-ignored。

### 当前已实现的 Processing / Representation 边界

- 音频 `process` 已切换为只接受已验证的 Managed Source `source_id`；新 package 不再把外部绝对路径作为 Source / Evidence 权威；历史 audio package 保持只读兼容。
- `Normalized Representation` 已有统一 contract、stable identity、strict manifest、completeness / warning、artifact verify 与 no-replace publish。
- 首批 production Adapter 已覆盖 Markdown、text PDF、XLSX、PPTX 和 image structural preflight。
- OCR、扫描 PDF 语义与复杂视觉语义仍是独立能力门禁，不是其他非 OCR 数据进入 Information lifecycle 的前置。
- Representation → Atomic Information 使用统一 Analysis Unit / Candidate / Residue / Evidence contract；semantic execution provider 只是 Adapter，不改变 Core。

Conversation 也遵循相同原则：Conversation 是一种 Representation / Processing 形态，不是新的业务 Core。WeChat、Codex、ChatGPT 等 provider 只负责 capture / mapping；长期信息仍进入统一 Atomic Information / Evidence 生命周期。

Conversation connector 对一个冻结窗口只执行一次 full capture。完整 capture、conversation index 与匿名 summary 作为私有 Processing Derived Artifact 持久化，并由 receipt-last 的运行计划绑定；分段恢复和历史验证只读 durable snapshot。不同完整 Conversation / Representation 可以进行 1–4 路（默认 2）result-only 语义分析，但同一会话的有序 Analysis Units 不跨 lane 拆分，并在一次有界模型调用中完整理解。详见 [ADR-006](../decisions/ADR-006-durable-capture-and-bounded-semantic-parallelism.md)。

微信日常入口在 connector 边界进一步收窄为一个已唯一绑定的联系人会话：

```text
contact metadata discovery（不读正文）
  → display name 唯一解析 + stable conversation technical key receipt
  → target-only frozen capture + attachment binding
  → Conversation Representation
  → semantic parallelism = 1 / ordered contact context continuation
  → serial Information + Identity + Governance + World Model apply
  → contact-level Event / Timeline / current-state View
```

联系人 selection receipt、独立 plan/checkpoint、context continuation 与 acceptance pack 都是 Processing / Derived Artifact / View，不是 Contact、Conversation、Message Core，也不形成第二套 Source 或 Information truth。恢复绑定已保存的 provider identity 与 technical key；展示名称作为可变历史留痕，同名联系人以不同 technical key 分别隔离。technical key 或 provider identity 漂移才在正文、Provider 和长期写入前拒绝。

contact context continuation 以有序 Atomic Information prefix 为 cursor：每个有界 segment 的 request 绑定前一完整 synthesis、下一段 Evidence、稳定联系人身份和 Provider profile；结构化 Event synthesis 先写入私有 result，再写 receipt 与 cursor。恢复优先验证 result / receipt / cursor 并零 Provider 收敛。acceptance pack 只把该 durable synthesis 投影为 Event、Timeline、current state、Evidence、冲突和未知，不在 View 层重新分组消息。

normal contact run 会把 legacy 全局 provenance 分成 terminal 与 nonterminal：terminal message keys 可安全过滤；任一捕获消息与 nonterminal legacy item 重叠时，在新 contact Source 写入前 fail closed。isolated acceptance 不使用该过滤，因为其完整运行边界不写 primary Workspace。

隔离验收使用独立私有 Workspace 复用同一 Source → Representation → Information → World Model 链，只在该 Workspace 产生 Derived Artifact；主 Workspace 与所有 primary checkpoint 保持只读不变。

---

## 3. Information Layer

Information Layer 承载：

```text
Atomic Information Candidate
Atomic Information
  ├─ Claim（可选归因）
  └─ Hypothesis（canonical 语义形态）
Evidence
Residue
```

其中：

- Candidate 来自 Processing；
- Atomic Information 是进入长期 Information Layer 的原子信息；
- Claim 表示声明主体 / 来源及其立场；
- Hypothesis 表示尚未成为稳定知识、但可由 Evidence / Feedback 支持、反对和修订的可检验命题；
- Evidence 提供来源追溯；
- Residue 保留当前无法安全结构化的内容。

Information Layer 的重要特征是：**允许矛盾和未决命题并存。**

```text
销售 A：客户预算约 20 万
设计师 B：客户预算至少 30 万
Hypothesis：如果先解决搭配不确定性，成交概率可能提高
```

它们都可以长期保存，而不要求 Information Layer 先选出唯一事实或把 Hypothesis 当作已验证知识。

`Atomic Information.confidence` 主要反映提取 / 语义理解置信度；Claim attribution confidence 反映归因置信度。Hypothesis 被现实支持到什么程度属于另一维度，不能复用这两个 confidence 字段表达真实性概率。

---

## 4. Structured World Model

长期 World Model 使用：

```text
Object
├─ stable object_id
├─ Name
├─ Role[]
├─ Lifecycle
└─ status

Object ── Relationship ── Object
```

当前通用 Relationship vocabulary：

```text
part_of
member_of
responsible_for
depends_on
related_to
```

核心结构原则：

- Object 提供稳定身份；
- Role 与身份分离；
- Name 与身份分离；
- Lifecycle 与 Role 分离；
- Relationship 形成有方向的 Graph；
- 只保存一条 canonical Relationship，不为纯查询需要重复保存 inverse Relationship；
- Atomic Information / Claim / Hypothesis 与 Object 保持 Information Layer / World Model Layer 分离；
- 来源与历史可以跨层追溯。

最重要的边界：

```text
Information Layer
= 保存现实中获得的信息、声明、Hypothesis、矛盾与不确定性

World Model
= ArcheOS 当前受治理的长期结构化认知
```

所以一条 Claim 或 Hypothesis 被保存，不代表对应内容已经成为 World Model state。

---

## 5. Information Digestion / Governance Boundary

语义并行的边界止于完整、严格验证并持久化的 recovery result bundle。Package publish、Atomic Information ingestion、Identity Gate、Interpretation / Governance、Proposal / Journal / World Model apply、item terminal state 与 checkpoint 必须按 durable plan 顺序串行执行，并由 global commit cursor 证明未越过更早 ordinal。后续 lane 可以先完成结果，但不能越过更早项提交；每次长期应用仍重读当前状态并沿用既有冲突与 Human Judgment 门。

Atomic Information 与 World Model 之间存在独立的信息消化与治理边界。

它负责：

- 识别 Atomic Information 涉及哪些已有 Object；
- 保留 / 解析 Claim attribution；
- 判断新信息对现有长期认知的影响；
- 发现不同 Claim、Information 与 World Model 之间的冲突；
- 判断一段信息是否只需留在 Information Layer，还是应形成 Name / Role / Lifecycle / Relationship 变化；
- 决定是安全自动执行还是请求人类判断；
- 在执行后保留来源、Evidence、Claim、Hypothesis 与历史。

该边界的默认执行单位是同一 Source / Representation 边界内的相关 Atomic Information 批次，而不是一条 Atomic Information 对应一次模型调用：

```text
Bounded ordered Atomic Information batch
                 ↓ one interpretation call
Validated ordered interpretation result
                 ↓ durable Processing receipt
Sequential idempotent World Model application
                 ↓ exception only
Existing Human Judgment / Change Proposal
```

Provider 只负责一次性形成整批有序判断，不能直接写 Store。Provider 返回并持久保存完整结果前，准备阶段保持只读，旧收据恢复与身份归位也不能提前写入。系统随后绑定输入顺序、结果与指纹；应用阶段逐条、幂等、可恢复。应用中断只恢复尚未完成的顺序写入，不再次请求模型；恢复读回按 receipt 与 cursor 的实际阶段验证已应用效果和未应用后缀。缺失、重复、乱序、未知 ID 或不完整结果必须在长期写入前 fail closed。

该批量边界复用现有 Processing / Audit、Information Governance 与 Change Proposal，不建立 Batch、Governance Job、Exception Queue 或 Agent Session 等新的 Core / Store。单条兼容入口可以存在，但生产多条入口不得静默退化为逐条 Provider 调用。

架构位置：

```text
Ordered Atomic Information + Claim + Hypothesis + Evidence batch
                              ↓
               Batch Interpretation + Governance
                              ↓
          Durable result → sequential application
                              ↓
                  World Model Change Service
                              ↓
                  WorldModelRepository
```

**业务规则不写在 Repository 内。**

如果更新是否成立依赖“不同 claimant 中应该相信谁”，或依赖尚未验证的 Hypothesis，系统不得因为模型 confidence 高就自动把它写成 World Model fact；应保留冲突 / Hypothesis，并在需要时请求人类判断或等待新的 Evidence / Feedback。

---

## 6. Relationship Boundary

Relationship 只连接两个已经存在、值得长期保持稳定身份的 Object。

```text
Object A
   ↓ typed Relationship
Object B
```

系统不应因为 Atomic Information 中出现一个普通工作内容、动作或名词，就为了建立 Relationship 自动创建 Object。

例如：

```text
“小沈负责产品拍摄”
```

如果“产品拍摄”只是一次工作描述：保留 Atomic Information / Claim 即可。

如果未来“内容生产”已经成为稳定长期 Object，且 Evidence 明确支持：

```text
小沈 → responsible_for → 内容生产
```

才进入 World Model。

当前 vocabulary 不足时，先保留信息，不临时发明 relation 值。只有真实数据反复证明有必要时，才先更新 `CONCEPTS.md` / ADR，再进入实现。

---

## 7. Persistence Boundary

ArcheOS 的领域语义和业务规则不能绑定某一种数据库。

```text
Domain Contracts
      ↓
Repository / Store Interfaces
      ↓
JSONL | SQLite | future database
```

因此：

- JSONL 可以作为正式存储 Adapter；
- SQLite 可以作为第一版本地 World Model Adapter；
- Claim 作为 Atomic Information 的结构随 Information Store 持久化，不建立 ClaimStore；
- Hypothesis 复用 Atomic Information 的 identity / Revision / Evidence，不建立 HypothesisStore；
- 更换存储方式不改变 Object、Atomic Information、Claim、Hypothesis、Role、Relationship 等语义；
- 更换存储方式也不改变信息吸收和审核规则；
- 避免无治理的双写导致多个事实源分叉。

---

## 8. Object Resolver 与 Context Builder

### 8.1 Object Resolver

内部关系使用稳定 `object_id`；读取时通过 Object Resolver 获得当前名称、Role、status、Lifecycle 等人类可读信息。

这样 Object 改名或 Role 调整，不需要修改所有 Atomic Information、Relationship 或 View 中的引用。

Object Resolver 是基础读取能力，不负责组装完整业务上下文。

### 8.2 Context Builder

在进入 External Agent、Human View 或未来 Domain Product 前，ArcheOS 提供统一的 `Context Builder` 作为上下文读取与组装能力。

Context Builder 根据调用目的和范围，从同一份长期数据中构建**有限边界、可追溯、明确说明完整性与截断情况**的 Context Bundle。

第一版优先实现 Object-scoped Context：

```text
Object
+ Name / Role / Lifecycle
+ Relationships
+ related Atomic Information / Claim / Hypothesis
+ Evidence / history
+ pending judgments
        ↓
   Context Builder
        ↓
   Context Bundle
```

Context Builder 必须区分：

- World Model 当前 state；
- Atomic Information / Claim / Hypothesis；
- 尚待判断的冲突 / Change Proposal。

不能把 Claim、Hypothesis 或 pending judgment 在读取时偷偷升级成 fact。

`Object Context` 不作为新的一级架构概念存在；它只是 `Context Builder(scope = Object)` 的一种使用范围。

未来 Goal、业务问题、经营态势、Conversation、Decision 等上下文需求继续复用同一个 Context Builder，不为每种用途建立平行的 `*ContextBuilder` 概念。

---

## 9. Core Graph 与 Human View

Core 保存 Graph，而不是强制唯一目录树。

```text
Canonical State
        ↓
Context Builder / Projection / View
        ↓
View Model
        ↓
Presentation
```

未来可以有：

- Object Profile；
- 向阳生长树；
- Relationship Graph；
- Timeline；
- Decision View；
- Protocol / Pattern / Hypothesis / Evidence drill-down。

树只是 View，不是 Core 唯一真实父子结构。

反向关系也可以在 View 中友好显示。例如 durable state 保存：

```text
产品库 → part_of → 展厅经营
```

View 可以显示“展厅经营包含产品库”，无需额外保存一条 `has_part`。

Human View 只消费 canonical read contracts，不拥有第二份 Object、Decision、Protocol、Pattern 或 Information truth。

---

## 10. External Agent 与 Domain Product Boundary

ArcheOS **不开发自己的通用 Agent**。推理、探索、方案生成和建议由 Codex、GPT、Claude、本地 Agent 或未来其他 External Agent 执行；ArcheOS 提供长期 Context、Evidence、治理、Protocol / Policy / Pattern 约束、审计与受控写回边界。

```text
ArcheOS Core
 长期 Information / World Model / Context / Governance
        ↓
External Agent
  理解 / 推理 / Judgment / 建议 / 获授权执行
        ↓
Domain Product
  围绕明确 Job-to-be-Done 提供用户体验
```

未来的 Sales、Founder、Project、Research、Operations 等是 **Domain Product 候选方向**，不是 ArcheOS Core 内部需要各自实现的 Agent 类型。

Domain Product 可以：

- 选择合适的 View / Presentation；
- 配置 Protocol / Policy / Pattern；
- 调用 External Agent；
- 让 Human 在合适的业务界面中完成 Judgment / Decision。

但不得：

- 建立自己的 Atomic Information / World Model truth；
- 为领域用途复制新的 Context Builder；
- 因领域术语建立平行 Person / Company / Project / Decision 模型；
- 绕过 Evidence / Governance 把 Agent inference 写成事实。

---

## 11. 当前架构状态

当前产品阶段与技术优先级以 Product / Development Roadmap 为准；本文只记录架构能力是否存在。

截至当前 main，关键边界已经形成：

```text
Managed Source                    已有 production runtime
Audio → Managed Source processing 已完成切换
Normalized Representation         已有公共 contract/runtime
首批多格式 Adapter                已有 production adapter
Atomic Information / Claim        已有 durable information foundation
Object / Relationship / World Model 已有 foundation
Information Governance            已有安全自动 / human judgment 边界
Context Builder                   已有 Object scope v1
Codex read-only integration       已有 CLI + MCP
```

Stage 1 当前仍需取得的关键 Evidence 主要集中在：

```text
Representation → Atomic Information 的统一 contract 收口
+ stable production semantic execution route
+ Conversation Representation / real semantic digestion
+ Information Consolidation
+ Object Emergence
+ 大规模真实旧数据 / Context 压力验证
```

这些工作的具体 Issue 顺序、Gate 和当前状态只以 `docs/development/ROADMAP.md` 与对应 GitHub Issue 为准，本文件不维护第二份执行计划。
