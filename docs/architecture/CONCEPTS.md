# ArcheOS 核心概念词典

## 1. 文档职责

本文件是 ArcheOS **核心概念定义的唯一权威入口**。

它回答：

- ArcheOS 中一个概念叫什么；
- 它准确表示什么；
- 它不表示什么；
- 它与其他概念是什么关系；
- 哪些词只是 Role、View、展示名称、工作流或实现细节，而不是新的 Core 概念。

`ARCHITECTURE.md` 说明这些概念如何组成系统；ADR 记录为什么做出某项架构决策；GitHub Issue 定义当前要实现什么。不要在其他文档中建立一套平行概念体系。

## 2. 概念治理规则

未来产品设计、架构设计和开发必须遵守：

1. **先复用已有概念。** 在提出新对象类型、Role、Relationship、生命周期词汇或信息类型之前，先检查本文件是否已有可表达的概念。
2. **不要建立同义或平行概念。** 不能因为某个业务场景换了说法，就创建一个含义近似的新 Core 概念。
3. **业务名称不自动成为 Core 概念。** 业务术语可以先作为 Name、Role、Relationship、Note 内容或 View 中的分组存在。
4. **确有必要新增概念时，必须同步更新本文件。** 新概念需要写明定义、边界、与已有概念的差异和至少一个使用示例。
5. **概念变更不得破坏稳定身份。** 名称变化、Role 调整、展示结构变化不应导致 Object ID 改变。
6. **实现不得先于概念授权。** 如果开发需要一个本文件尚未定义、且会影响长期数据模型的新 Core 概念，应先升级为架构决策，而不是在代码中偷偷引入。

## 3. 核心分层

ArcheOS 的长期模型分为四个不同层次，不应混淆：

```text
Information
  └─ Note / Evidence / Residue

World Model
  └─ Object / Role / Relationship / Lifecycle / Name

Projection
  └─ View / View Model

Presentation
  └─ HTML / Markdown / React / Mobile / AI conversation
```

其中：

- **Information** 表示“我们知道了什么”；
- **World Model** 表示“世界中有哪些可长期引用的东西，以及它们如何关联”；
- **Projection** 表示“为了某种理解目的，我们如何观察这些数据”；
- **Presentation** 表示“最终如何向人展示、解释和请求确认”。

## 4. Object

### 定义

`Object` 是 ArcheOS 对现实世界或经营世界中一个**需要长期保持稳定身份的可引用对象**的统一抽象。

Object 的身份由不可变 `object_id` 表示，而不是由名称、目录位置或当前 Role 表示。

### 核心原则

- `object_id` 一旦建立，应保持稳定；
- Object 可以改名；
- Object 可以同时拥有多个 Role；
- Object 的 Role 可以随时间变化；
- Object 可以与多个其他 Object 建立 Relationship；
- Object 不要求必须属于一棵唯一的目录树。

### 何时应该创建 Object

只有当某个东西至少满足以下一种长期需要时，才应考虑升格为 Object：

- 需要长期累积信息；
- 会被多个来源反复引用；
- 有独立状态或生命周期；
- 需要与其他 Object 建立持续关系；
- 需要作为独立页面、档案或决策对象被追踪。

一个名词仅在录音或文档中出现，并不足以自动创建 Object。

### 示例

- “私享国际家具”可以是一个 Object；
- “展厅经营”可以是一个 Object；
- “海丝金融中心家具采购”可以是一个 Object；
- “内部产品库”如果需要长期维护、更新和引用，可以升格为 Object；
- “SKU 很重要”只是一个 Note，不是 Object。

## 5. Role

### 定义

`Role` 表示一个 Object **当前或在某段时间内以什么业务身份被理解和使用**。

Role 不是 Object 的身份，也不是独立 Object。

### 当前已接受的 Role

第一版允许使用：

- `person`
- `company`
- `brand`
- `project`
- `business_line`
- `event`
- `goal`
- `decision`

这些 Role 未来可以演化，但新增 Role 前必须先检查现有 Role 是否已经足够表达，并在必要时更新本文件。

### 多 Role

同一个 Object 可以同时拥有多个 Role。

例如：

```text
Object: 私享国际家具
roles:
- company
- brand
```

是否应拆成两个 Object，应根据它们是否需要独立身份、状态和关系来决定，而不是机械地“一种 Role 一个 Object”。

### Role 历史

Role 可以带时间边界和来源：

```text
object_id: obj_x
role: project
valid_from: ...
valid_to: ...
source: ...
confidence: ...
```

如果后来发现“展厅经营”不应理解为 `project`，而应理解为 `business_line`，Object ID 不变，只调整 Role 及其历史。

## 6. Name

### 定义

`Name` 是 Object 面向人的可读名称，不是引用键。

推荐至少区分：

- `current_name`：当前展示名称；
- `aliases`：其他可识别名称；
- `name_history`：历史名称及生效时间。

### 原则

- Relationship、Note、Decision 等内部引用必须使用 `object_id`；
- 面向人的界面默认显示可读名称，而不是裸 ID；
- 名称变化不应触发 Object 迁移。

## 7. Lifecycle

### 定义

`Lifecycle` 表示 Object 在时间上的存在、推进和结束特征。

Lifecycle 与 Role 分离。

### 原则

“它是什么”属于 Role；“它如何随时间存在和结束”属于 Lifecycle。

例如：

```text
展厅经营
role = business_line
start_at = ...
target_end_at = null
completion_condition = null
```

```text
海丝金融中心家具采购
role = project
start_at = ...
target_end_at = ...
completion_condition = ...
```

因此，不应为了“长期/短期”分别建立两套底层实体表。

## 8. Relationship

### 定义

`Relationship` 是两个 Object 之间可长期保存、可追溯的有类型关系。

ArcheOS Core 的真实结构是 Graph，而不是强制树。

Relationship 至少应能够保留：

- `from_object_id`
- `relation`
- `to_object_id`
- `valid_from / valid_to`（适用时）
- `source`
- `confidence / uncertainty`

### 原则

- 不把目录层级当作唯一关系；
- 一个 Object 可以同时连接多个父级、业务线、项目、人物或能力；
- Relationship 名称也受本文件的概念治理约束，避免出现大量同义边。

## 9. Note

### 定义

`Note` 是可独立追溯的长期原子信息记录，属于 Information 层，**不是 Object，也不是 Object 的 Role**。

M2 起，符合信息契约的 Atomic Information 可以**自动吸收为 durable Note，不要求逐条人工审核**。

一个 Note 至少保留：

- statement；
- semantic type；
- concerns / related Object IDs；
- source evidence；
- context；
- confidence / uncertainty；
- 来源与处理历史。

### 历史规则

Note 的修改不得覆盖历史。

如果模型或人类后来对 Note 进行了修订，应保留旧版本和新版本之间的可追溯关系，而不是把旧内容直接改写掉。具体存储可以使用 revision、append-only record 或等价机制，但“历史不可被静默覆盖”是长期语义规则。

### 与 Object 的关系

Note 可以：

- 描述一个已有 Object；
- 同时关联多个 Object；
- 为已有 Object 补充新的信息；
- 提示已有认知可能需要更新；
- 成为建立或修改 Name / Role / Relationship / Lifecycle 的证据；
- 保持为独立信息，而不一定改变 World Model。

系统把 Note 与已有 Object 关联时，应先判断这条信息属于：

1. **补充**：增加新的、与当前认知相容的信息；
2. **更新**：新信息说明已有认知需要调整；
3. **冲突**：新信息与当前长期认知或其他可信信息存在无法自动消解的矛盾。

补充信息可以自动进入 Note 与 Object 的关联上下文。需要改变 World Model 的更新，以及无法安全自动消解的冲突，应进入受控的 World Model change flow；冲突应优先交由人类判断，而不是覆盖旧信息。

## 10. Atomic Information Candidate

`Atomic Information Candidate` 是 Processing 阶段生成的原子信息候选。

它与 durable Note 的区别主要是所处生命周期阶段，而不是是否必须经过人工逐条确认。

典型状态演化：

```text
Atomic Information Candidate
  → contract validation
  → automatic ingestion
  → durable Note
```

如果候选不满足信息契约、Evidence 不完整或存在处理失败，应失败或进入适当的 Residue / processing failure 边界，不应通过“人工确认”掩盖数据质量问题。

## 11. Evidence

`Evidence` 是信息或结构化判断回到来源的可追溯依据。

对于音频 M1，目前至少包含：

```text
source
→ transcript segment
→ speaker
→ timestamp
→ excerpt
```

后续 Object Name、Role、Relationship、Lifecycle 更新也应能够回到支持它们的 Note / Evidence，而不是只保存最终结论。

## 12. Residue

`Residue` 是当前处理流程无法安全吸收的信息。

Residue 不是垃圾，也不是运行错误。

它用于：

- 保留歧义、冲突、上下文不足、证据不足或重要性不明的信息；
- 衡量信息消化过程的健康度；
- 为后续人工复核和模型改进保留入口。

## 13. World Model Change

### 定义

`World Model Change` 是会改变系统对长期世界结构认知的动作，例如：

- 新建或删除 Object；
- 修改 Object Name；
- 新增、结束或调整 Role；
- 新增、结束或调整 Relationship；
- 修改 Lifecycle。

这些动作不同于“新增一条 Note”。Note 可以自动吸收；World Model Change 会改变长期结构化认知，因此必须遵守更严格的治理边界。

### 人工判断边界

需要新建或修改 Object / Name / Role / Relationship / Lifecycle 的动作，应先形成可解释的变更建议，并获得人类授权后再执行。

模型可以直接通过对话或其他 prompt 方式请求授权，不要求先建设专门审核前端。

当新信息与现有 World Model 冲突时，不得静默覆盖；应把冲突和建议交给人类判断。

### 孤立 Object 原则

ArcheOS 应尽量避免产生与其他长期对象没有有效关系的孤立 Object。

- 新建 Object 时，优先同时说明它与已有 Object 的业务联系；
- 如果暂时无法建立关系，应要求人类确认为什么仍值得单独保留；
- 删除 Object 时，不应使仍需保留的其他 Object 因失去唯一有效关系而意外成为孤立 Object；
- 删除应保留必要历史与可追溯性，具体物理删除策略由后续实现决定。

## 14. View

### 定义

`View` 是对同一份 Core World Model 的一种**人类理解投影**，属于 Projection / Presentation 边界，不是新的 Core Object 类型。

Core 保存 Graph；View 可以把它投影成：

- 树；
- 关系图；
- 时间线；
- 对象档案；
- 决策链；
- 其他面向人的结构。

### 示例：向阳生长树

同一批 Object 可以被投影为：

```text
私享国际家具
├─ 展厅经营 [business_line]
│  ├─ 产品库
│  └─ 销售工具
└─ 海丝金融中心家具采购 [project]
```

这棵树只是 View，不是 Core 中唯一真实的父子结构。

## 15. View Model 与 Presentation

`View Model` 是从 Core Graph 根据某个 View 规则计算出来的、适合前端读取的数据结构。

`Presentation` 是最终面向人的表达方式，例如：

- HTML 图文；
- Markdown；
- React Web；
- Mobile；
- AI conversation rendering；
- 人工审核提示与确认问题。

原则：

```text
Core Data
  → Projection / View Model
  → Human-facing Presentation
```

HTML 不是 Core 数据权威，也不应反向成为 Object 关系的唯一存储位置。

### Human-facing Language 原则

**所有面向人类的内容必须使用业务语言，而不是内部技术语言。**

这包括但不限于：

- 前端页面；
- AI 对人发出的提示；
- 变更审核请求；
- 风险、冲突和错误说明；
- 报告与摘要；
- 人类需要做选择或确认的任何信息。

目标读者按“一般大学本科毕业生”理解能力设计：无需了解数据库、schema、foreign key、object_id、mutation、repository、graph edge 等内部技术概念，也应该能够理解发生了什么、为什么需要判断、各选项会带来什么业务后果。

例如，内部可能表示为：

```text
add_role(object_id=obj_x, role=business_line)
```

但面向人类应表达为类似：

> 系统发现“展厅经营”更像一项持续经营的业务，而不是一个有明确结束时间的项目。是否将它调整为“业务线”？原有历史记录会保留。

人类界面可以按需隐藏内部 ID；只有在审计、调试或用户明确要求技术细节时才展示。

## 16. Object Resolver

`Object Resolver` 是展示和读取时，将稳定 `object_id` 解析为当前人类可读信息的机制。

概念上它至少可以返回：

```text
object_id
current_name
primary/display role
status
aliases
```

因此，内部大量使用 ID 不会影响人类展示；名称或 Role 更新后，所有 View 可以在解析时自动显示新信息。

Object Resolver 是读取机制，不是新的 Domain Object。

## 17. Structured World Model

`Structured World Model` 是对 `Object + Role + Relationship + Lifecycle + Name` 形成的长期结构化世界的统称。

它是一个架构层概念，不是需要创建 ID 的独立 Object。

Note / Evidence 为 World Model 提供可追溯信息依据；View 将 World Model 投影给人类。

## 18. 存储与概念分离

JSONL、SQLite、未来的其他数据库都只是持久化实现方式，不是领域概念本身。

长期业务逻辑应依赖稳定的信息与 World Model contract，而不是依赖某一种具体数据库。

原则：

- JSONL 可以是正式存储方式之一，而不仅是导出格式；
- SQLite 可以是本地第一版实现；
- 未来可以增加其他 Store / Repository adapter；
- 更换存储后，不应迫使 Object、Note、Role、Relationship 等核心语义发生变化；
- 同一次权威写入流程应避免无治理的多存储双写，防止形成多个相互分叉的事实源。

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

A 与 B、C 的实际关系应通过 Relationship 表达，而不是靠目录名或 ID 编码表达。

## 20. 明确禁止的建模方式

除非新的架构决策明确修改本文件，否则不要：

- 建立 `ProjectObject`、`BusinessLineObject`、`PersonObject` 等彼此平行的底层对象体系；
- 用名称作为内部外键；
- 因改名而创建一个新的 Object；
- 把树形展示结构当成唯一真实关系；
- 因某个页面需要一个分组就增加新的 Core Object 类型；
- 把 Note 与 Object 混成同一类实体；
- 把 Note 的新版本静默覆盖到旧版本上；
- 将前端 HTML 视为 Core 数据源；
- 让面向人类的页面或提示直接暴露内部技术术语，要求普通业务用户理解实现细节；
- 在代码中引入本文件未定义、且会改变长期语义模型的新 Core 概念而不更新文档。
