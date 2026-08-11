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

---

## 2. Information

`Information` 表示 ArcheOS 已经从输入中获得、能够被理解和追溯的内容。

Information 层主要包含：

- `Atomic Information Candidate`
- `Atomic Information`
- `Evidence`
- `Residue`

Information 描述“我们知道了什么”，不等同于长期世界中的 Object。

`Note` **不是 ArcheOS Core 的正式概念**。如果未来产品中出现“笔记 / Note”功能，它可以作为面向人的业务或展示名称，但不应与 `Atomic Information` 建立一套平行的长期信息模型。

---

## 3. Object

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

## 4. Role

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

## 5. Name

`Name` 是 Object 面向人的可读名称。

Name 不是 Object 的身份键。

一个 Object 可以具有：

- 当前主要名称；
- 别名；
- 历史名称。

因此，对象改名与“创建一个新的 Object”是两个不同概念。

---

## 6. Lifecycle

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

## 7. Relationship

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

- `related_to`：表示两个 Object 之间存在明确、值得长期保留的业务联系，但当前没有必要或还没有足够依据定义更具体的 Relationship 语义。

`related_to` 是刻意保持宽泛的关系，不表示隶属、所有权、责任、因果或其他更具体含义。以后如果某类更具体的 Relationship 被正式定义，可以新增更精确的关系，而不改变两个 Object 的身份。

---

## 8. Atomic Information

`Atomic Information` 是一个**可独立理解、可独立追溯的长期原子信息单元**。

Atomic Information 属于 Information 层，不是 Object，也不是 Object 的 Role。

一条 Atomic Information 可以包含：

- statement；
- semantic type；
- concerns / related Object IDs；
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

Atomic Information 的后续修订仍属于同一条长期信息身份，并通过 Revision / 历史记录表达变化；Revision 是实现和历史结构，不是新的 Core 概念。

---

## 9. Atomic Information Candidate

`Atomic Information Candidate` 是 Processing 阶段产生的、尚未进入长期 Atomic Information 层的原子信息候选。

它与 durable Atomic Information 的主要区别是所处生命周期阶段：

```text
Processing
  → Atomic Information Candidate
  → Atomic Information
```

候选信息仍应保留 statement、Evidence、context、confidence / uncertainty 等可追溯信息。

---

## 10. Evidence

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
- Evidence 表示“这条信息依据什么来源”。

---

## 11. Residue

`Residue` 是 Processing 阶段当前无法安全转化为结构化信息的内容。

Residue 可能来自：

- 歧义；
- 上下文不足；
- 冲突；
- 证据不足；
- 重要性暂时无法判断。

Residue 不是运行错误，也不是垃圾。

---

## 12. Structured World Model

`Structured World Model` 是对以下长期结构化内容的统称：

```text
Object
+ Name
+ Role
+ Lifecycle
+ Relationship
```

它不是需要单独创建 ID 的 Object，而是一个架构层概念。

Atomic Information / Evidence 描述和支撑 World Model；World Model 表达 ArcheOS 对长期经营世界的结构化认知。

---

## 13. Projection

`Projection` 表示为了某种理解目的，从同一份 World Model 中选择、组织和计算出一个观察结果的过程。

Projection 不改变 Core Data 本身。

---

## 14. View

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

## 15. View Model

`View Model` 是根据某个 View / Projection 从 Core Data 得到的、适合展示层读取的数据结构。

它是 Core Data 与 Presentation 之间的读取模型，不是 Core 数据权威。

---

## 16. Presentation

`Presentation` 表示最终面向人的呈现方式。

例如：

- HTML；
- Markdown；
- React Web；
- Mobile；
- AI conversation。

Presentation 不是 Core Data，也不承担长期 Object / Relationship 的权威存储职责。

---

## 17. Object Resolver

`Object Resolver` 是读取时根据稳定 `object_id` 获取当前人类可读 Object 信息的机制。

例如可以解析出：

- 当前名称；
- 当前 Role；
- aliases；
- status；
- Lifecycle 摘要。

Object Resolver 是读取机制，不是新的 Domain Object。

---

## 18. 当前示例

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
