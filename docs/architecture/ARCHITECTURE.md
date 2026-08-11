# ArcheOS 系统架构说明

## 1. 核心生命周期

ArcheOS 只保留一条主生命周期：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

任何新增能力都必须说明自己服务于这条主线的哪一阶段，不建立同义或平行生命周期。

`Structured Object` 表示进入长期结构化世界模型的阶段，不意味着系统采用互斥的 Person / Company / Project 基础表。

核心概念定义以 `docs/architecture/CONCEPTS.md` 为准；信息吸收和长期认知更新的业务规则以 `docs/product/INFORMATION_GOVERNANCE.md` 为准。

---

## 2. Input 与 Processing

Input 接收音频、PDF、图片、PPT、视频等原始信息。原始输入保持不可修改并可追溯。

Processing 把输入转化为可理解、可追溯的中间产物。音频 M1 已支持：

- transcript；
- meeting summary；
- atomic information candidates；
- residue。

会议纪要和 Residue 是 Processing 辅助产物，不建立平行生命周期。

---

## 3. Information Layer

Information 层承载：

```text
Atomic Information Candidate
Atomic Information
Evidence
Residue
```

其中：

- Candidate 来自 Processing；
- Atomic Information 是进入长期 Information 层的原子信息；
- Evidence 提供来源追溯；
- Residue 保留当前无法安全结构化的内容。

Candidate 如何进入 Atomic Information、Atomic Information 如何修订等运行规则由 `INFORMATION_GOVERNANCE.md` 定义，不在架构文档重复定义。

`Note` 不再作为 Core 信息概念使用。

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

Atomic Information ── concerns / supports ── World Model
```

核心结构原则：

- Object 提供稳定身份；
- Role 与身份分离；
- Name 与身份分离；
- Lifecycle 与 Role 分离；
- Relationship 形成 Graph；
- Atomic Information 与 Object 保持 Information Layer / World Model Layer 分离；
- 来源与历史可以跨层追溯。

具体概念定义见 `CONCEPTS.md`。

---

## 5. Information Digestion / Governance Boundary

Atomic Information 与 World Model 之间需要一个独立的“信息消化与治理层”。

它负责：

- 识别 Atomic Information 涉及哪些已有 Object；
- 判断新信息对现有长期认知的影响；
- 发现冲突或歧义；
- 决定是自动执行还是请求人类判断；
- 在执行后保留来源、Evidence 与历史。

架构位置：

```text
Atomic Information / Evidence
          ↓
Interpretation + Governance
          ↓
World Model Change Service
          ↓
WorldModelRepository
```

**业务规则不写在 Repository 内。**

哪些情况可以自动执行、哪些情况必须由人类判断，以 `docs/product/INFORMATION_GOVERNANCE.md` 为唯一长期规则来源。

---

## 6. Persistence Boundary

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
- 未来可以增加其他数据库；
- 更换存储方式不改变 Object、Atomic Information、Role、Relationship 等语义；
- 更换存储方式也不改变信息吸收和审核规则；
- 避免无治理的双写导致多个事实源分叉。

---

## 7. Object Resolver 与 Context Builder

### 7.1 Object Resolver

内部关系使用稳定 `object_id`；读取时通过 Object Resolver 获得当前名称、Role、status、Lifecycle 等人类可读信息。

这样 Object 改名或 Role 调整，不需要修改所有 Atomic Information、Relationship 或 View 中的引用。

Object Resolver 是基础读取能力，不负责组装完整业务上下文。

### 7.2 Context Builder

在进入 Domain Agent 和 Human View 前，ArcheOS 提供统一的 `Context Builder` 作为上下文读取与组装能力。

Context Builder 根据调用目的和范围，从同一份长期数据中构建**有限边界、可追溯、明确说明完整性与截断情况**的 Context Bundle。

第一版优先实现 Object-scoped Context：

```text
Object
+ Name / Role / Lifecycle
+ Relationships
+ related Atomic Information
+ Evidence / history
        ↓
   Context Builder
        ↓
   Context Bundle
```

`Object Context` 不作为新的一级架构概念存在；它只是 `Context Builder(scope = Object)` 的第一种使用范围。

未来 Goal、业务问题、经营态势、Agent 对话等上下文需求继续复用同一个 Context Builder，不为每种用途建立平行的 `*ContextBuilder` 概念。

Sales Agent、Brand Agent、Project Agent 以及未来 View 优先消费统一 Context Builder 输出，而不是各自重新扫描全部原始资料。

---

## 8. Core Graph 与 Human View

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

面向人的页面、AI 提示和审核问题必须遵循 `INFORMATION_GOVERNANCE.md` 中的人类表达规则；架构层只定义 Presentation 边界，不重复业务文案规则。

---

## 9. Domain Agent

Sales Agent、Brand Agent、Project Agent 等是 Core 之上的领域解释能力，不建立新的 Input → Processing → Object 生命周期。

它们读取：

- Processing 产物；
- Atomic Information；
- Structured World Model；
- Context Builder 输出；
- View Model。

它们可以产生领域报告、判断和更新建议，但不能因为领域术语新增平行 Core 对象体系，也不应各自创建一套独立 Context Builder。

---

## 10. 当前阶段

M1 已完成通用音频信息消化。

M2 当前推进顺序：

```text
M2-A  World Model foundation
  ↓
M2-B1 Durable Atomic Information + automatic ingestion
  ↓
M2-B2 Atomic Information → World Model digestion / governance
  ↓
M2-B3 Context Builder — Object-scoped v1
```

前端 Human View 延后到上述核心链路稳定之后。
