# ArcheOS 系统架构说明

> 产品名称：**向阳经营系统（Sunward Operating System）**  
> 当前工程 / 仓库代号：`ArcheOS`。ArcheOS 用于重构迁移阶段；完成替代后，对外仍使用“向阳经营系统”。

## 当前版本化架构图

最近一版完整图形快照为 `v0.2.0`（M2 Target Architecture，2026-08-11）：

- 系统架构图：`docs/architecture/diagrams/v0.2.0/system-architecture-v0.2.0.svg`
- 数据流图：`docs/architecture/diagrams/v0.2.0/data-flow-v0.2.0.svg`
- 版本说明：`docs/architecture/diagrams/v0.2.0/README.md`
- 版本索引：`docs/architecture/diagrams/README.md`

本文件与 `CONCEPTS.md` 已在该图形快照之后加入 Claim 与首批 Relationship vocabulary。概念与产品规则分别以 `CONCEPTS.md` 和 `INFORMATION_GOVERNANCE.md` 为权威；图形快照将在 B2 实现经过真实验证后升级下一版本，避免仅因概念小幅调整频繁重画。

---

## 1. 核心生命周期

ArcheOS 只保留一条主生命周期：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

Claim 不建立第二条生命周期。它是 Atomic Information 中用于表达“谁以什么立场说了什么”的可选归因结构。

`Structured Object` 表示进入长期结构化世界模型的阶段，不意味着系统采用互斥的 Person / Company / Project 基础表。

核心概念定义以 `docs/architecture/CONCEPTS.md` 为准；信息吸收和长期认知更新的业务规则以 `docs/product/INFORMATION_GOVERNANCE.md` 为准。

---

## 2. Input 与 Processing

Input 接收音频、PDF、图片、PPT、视频等原始信息。正式 `Source` 必须经过用户明确准入、完整字节复制以及 `size` / `content_hash` 校验后才成立。

Source 的权威边界是：

```text
外部文件
  = 临时 intake candidate / 可失效接入线索
  ↓ 用户明确准入 + 完整字节校验
Managed Source
  = 稳定 source_id + managed_location + 不可变字节快照
  ↓
Normalized Representation
  ↓
Evidence
  ↓
Atomic Information
```

Managed Source 是后续 Processing、Evidence 和 Normalized Representation 的唯一权威输入。外部文件归档完成后不再被系统自动跟踪、同步或重新处理；用户修改旧文件必须显式重新接入，形成新的 Source。已被 Evidence 引用的 Source 字节不得由同一 `source_id` 原地覆盖。

`content_hash` 只表示某个受管字节快照的内容身份，可用于完整性校验和存储去重；它不单独定义 `source_id`，也不自动合并不同接入语境。Archive replica、TOS replica 和 Handoff Marker 都不是新的 Source。

第一版本地 Managed Source 根目录为 `01_inbox/` 的受控 Source 区；实际字节和 Manifest 保持本地、Git-ignored。外部扫描 candidate 不复制到正式 Source 区，也不创建稳定 `source_id`。

Processing 把输入转化为可理解、可追溯的中间产物。音频 M1 已支持：

- transcript；
- meeting summary；
- Atomic Information Candidate；
- Residue。

第一版不要求重新修改 M1 Candidate contract 来承载 Claim。B2 可以结合 Atomic Information 的 statement、Evidence、Source / speaker attribution 补充 Claim，并通过同一 `atomic_information_id` 的新 revision 长期保存。未来若真实数据证明 Processing 阶段直接产出 Claim 更合适，再单独调整 Candidate contract。

Claimant 尚未解析成 Object 时，可以继续使用 Source / speaker 归因，不强制创建 Person Object。

会议纪要和 Residue 是 Processing 辅助产物，不建立平行生命周期。

当前 M1 实现中的 `process_audio()` 仍直接接收外部本地音频路径，使用文件名 stem 与 SHA-256 前缀派生 Source ID，并在 processing manifest 中保存外部绝对路径。这些行为属于早期 legacy provenance，后续 Source runtime 迁移时必须替换；本 Issue 只固化架构权威，不修改代码或旧 Processing package 的读取兼容性。

---

## 3. Information Layer

Information Layer 承载：

```text
Atomic Information Candidate
Atomic Information
  └─ Claim（可选）
Evidence
Residue
```

其中：

- Candidate 来自 Processing；
- Atomic Information 是进入长期 Information Layer 的原子信息；
- Claim 表示声明主体 / 来源及其立场；
- Evidence 提供来源追溯；
- Residue 保留当前无法安全结构化的内容。

Information Layer 的重要特征是：**允许矛盾并存。**

```text
销售 A：客户预算约 20 万
设计师 B：客户预算至少 30 万
```

这两条都可以长期保存为 Atomic Information + Claim，而不要求 Information Layer 先选出唯一“事实”。

`Atomic Information.confidence` 主要反映提取 / 语义理解置信度；Claim attribution confidence 反映归因置信度。二者都不自动等于现实真实性概率。

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
- Atomic Information / Claim 与 Object 保持 Information Layer / World Model Layer 分离；
- 来源与历史可以跨层追溯。

最重要的边界：

```text
Information Layer
= 保存现实中获得的不同信息、声明、矛盾与不确定性

World Model
= ArcheOS 当前受治理的长期结构化认知
```

所以一条 Claim 被保存，不代表对应内容已经成为 World Model state。

---

## 5. Information Digestion / Governance Boundary

Atomic Information 与 World Model 之间存在独立的“信息消化与治理层”。

它负责：

- 识别 Atomic Information 涉及哪些已有 Object；
- 保留 / 解析 Claim attribution；
- 判断新信息对现有长期认知的影响；
- 发现不同 Claim 或 Claim 与 World Model 之间的冲突；
- 判断一段信息是否只需留在 Information Layer，还是应形成 Name / Role / Lifecycle / Relationship 变化；
- 决定是自动执行还是请求人类判断；
- 在执行后保留来源、Evidence、Claim 与历史。

架构位置：

```text
Atomic Information + Claim + Evidence
                ↓
      Interpretation + Governance
                ↓
        World Model Change Service
                ↓
        WorldModelRepository
```

**业务规则不写在 Repository 内。**

如果更新是否成立依赖“不同 claimant 中应该相信谁”，第一版不得由模型自动决定，必须保留冲突并请求人类判断。

---

## 6. Relationship Boundary

Relationship 只连接两个已经存在、值得长期保持稳定身份的 Object。

```text
Object A
   ↓ typed Relationship
Object B
```

B2 不应因为 Atomic Information 中出现一个普通工作内容、动作或名词，就为了建立 Relationship 自动创建 Object。

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

当前 vocabulary 不足时，先保留信息，不临时发明 relation 值。只有真实数据反复证明有必要时，再通过架构评审扩展 `CONCEPTS.md`。

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
- 更换存储方式不改变 Object、Atomic Information、Claim、Role、Relationship 等语义；
- 更换存储方式也不改变信息吸收和审核规则；
- 避免无治理的双写导致多个事实源分叉。

---

## 8. Object Resolver 与 Context Builder

### 8.1 Object Resolver

内部关系使用稳定 `object_id`；读取时通过 Object Resolver 获得当前名称、Role、status、Lifecycle 等人类可读信息。

这样 Object 改名或 Role 调整，不需要修改所有 Atomic Information、Relationship 或 View 中的引用。

Object Resolver 是基础读取能力，不负责组装完整业务上下文。

### 8.2 Context Builder

在进入 Domain Agent 和 Human View 前，ArcheOS 提供统一的 `Context Builder` 作为上下文读取与组装能力。

Context Builder 根据调用目的和范围，从同一份长期数据中构建**有限边界、可追溯、明确说明完整性与截断情况**的 Context Bundle。

第一版优先实现 Object-scoped Context：

```text
Object
+ Name / Role / Lifecycle
+ Relationships
+ related Atomic Information / Claim
+ Evidence / history
+ pending judgments
        ↓
   Context Builder
        ↓
   Context Bundle
```

Context Builder 必须区分：

- World Model 当前 state；
- Atomic Information / Claim；
- 尚待判断的冲突 / Proposal。

不能把 Claim 或 pending judgment 在读取时偷偷升级成 fact。

`Object Context` 不作为新的一级架构概念存在；它只是 `Context Builder(scope = Object)` 的第一种使用范围。

未来 Goal、业务问题、经营态势、Agent 对话等上下文需求继续复用同一个 Context Builder，不为每种用途建立平行的 `*ContextBuilder` 概念。

---

## 9. Core Graph 与 Human View

Core 保存 Graph，而不是强制唯一目录树。

```text
Structured World Model
        ↓
Projection / View
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
- Decision View。

树只是 View，不是 Core 唯一真实父子结构。

反向关系也可以在 View 中友好显示。例如 durable state 保存：

```text
产品库 → part_of → 展厅经营
```

View 可以显示“展厅经营包含产品库”，无需额外保存一条 `has_part`。

---

## 10. Domain Agent

Sales Agent、Brand Agent、Project Agent 等是 Core 之上的领域解释能力，不建立新的 Input → Processing → Object 生命周期。

它们读取：

- Processing 产物；
- Atomic Information / Claim；
- Structured World Model；
- Context Builder 输出；
- View Model。

它们可以产生领域报告、判断和更新建议，但不能因为领域术语新增平行 Core 对象体系，也不应各自创建一套独立 Context Builder。

---

## 11. 当前阶段

M1 已完成通用音频信息消化。

M2 当前推进顺序：

```text
M2-A  World Model foundation（已完成）
  ↓
M2-B1 Durable Atomic Information + automatic ingestion（已完成）
  ↓
M2-B2 Atomic Information / Claim → World Model digestion / governance
  ↓
M2-B3 Context Builder — Object-scoped v1
  ↓
M2-B4 Real End-to-End Validation
```

Claim 与首批 Relationship vocabulary 先在 B2 / B4 的真实数据中运行；是否扩展 Relationship 类型以真实缺口为依据，不提前建设复杂 ontology。
