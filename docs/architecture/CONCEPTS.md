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

1. **先复用已有概念。** 在提出新对象类型、Role、Relationship、Lifecycle 词汇或信息类型之前，先检查本文件是否已有可表达的概念。
2. **不要建立同义或平行概念。** 不能因为某个业务场景换了说法，就创建一个含义近似的新 Core 概念。
3. **业务名称不自动成为 Core 概念。** 业务术语可以先作为 Name、Role、Relationship、Note 内容或 View 中的分组存在。
4. **确有必要新增概念时，必须同步更新本文件。** 新概念需要写明定义、边界、与已有概念的差异和至少一个使用示例。
5. **概念变更不得破坏稳定身份。** 名称变化、Role 调整、展示结构变化不应导致 Object ID 改变。
6. **实现不得先于概念授权。** 如果开发需要一个本文件尚未定义、且会影响长期数据模型的新 Core 概念，应先升级为架构决策，而不是在代码中偷偷引入。

## 3. 核心分层

ArcheOS 的长期模型分为四个不同层次：

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

- **Information**：我们知道了什么；
- **World Model**：世界中有哪些可长期引用的东西，以及它们如何关联；
- **Projection**：为了某种理解目的，如何观察这些数据；
- **Presentation**：最终如何向人展示、解释和请求确认。

## 4. Object

`Object` 是现实世界或经营世界中一个**需要长期保持稳定身份的可引用对象**。

Object 的身份由不可变 `object_id` 表示，而不是由名称、目录位置或当前 Role 表示。

### 原则

- `object_id` 一旦建立，应保持稳定；
- Object 可以改名；
- Object 可以同时拥有多个 Role；
- Role 可以随时间变化；
- Object 可以与多个其他 Object 建立 Relationship；
- Object 不要求属于一棵唯一目录树。

### 何时创建 Object

只有当某个东西至少满足一种长期需要时，才应考虑升格为 Object：

- 需要长期累积信息；
- 会被多个来源反复引用；
- 有独立状态或 Lifecycle；
- 需要与其他 Object 建立持续关系；
- 需要作为独立档案或决策对象被追踪。

一个名词仅在录音或文档中出现，并不足以自动创建 Object。

例如：

- “私享国际家具”可以是 Object；
- “展厅经营”可以是 Object；
- “海丝金融中心家具采购”可以是 Object；
- “内部产品库”如果需要长期维护，可以升格为 Object；
- “SKU 很重要”只是 Note，不是 Object。

## 5. Role

`Role` 表示一个 Object **当前或在某段时间内以什么业务身份被理解和使用**。

Role 不是 Object 身份，也不是独立 Object。

### 当前已接受 Role

- `person`
- `company`
- `brand`
- `project`
- `business_line`
- `event`
- `goal`
- `decision`

新增 Role 前必须先判断已有 Role 是否已经足够表达，并在必要时先更新本文件。

### 多 Role 与历史

同一 Object 可以同时拥有多个 Role，例如：

```text
Object: 私享国际家具
roles:
- company
- brand
```

Role 应保留时间边界、来源和不确定性。若“展厅经营”从 `project` 重新理解为 `business_line`，Object ID 不变，只调整 Role 及其历史。

## 6. Name

`Name` 是 Object 面向人的可读名称，不是引用键。

至少应支持：

- 当前名称；
- aliases；
- 名称历史。

内部关系、Note 等引用稳定 `object_id`；人类界面默认显示可读名称。改名不触发 Object 迁移。

## 7. Lifecycle

`Lifecycle` 表示 Object 在时间上的存在、推进和结束特征，与 Role 分离。

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

不要为了“长期/短期”建立两套底层实体模型。

## 8. Relationship

`Relationship` 是两个 Object 之间可长期保存、可追溯的有类型关系。

ArcheOS Core 的真实结构是 Graph，而不是强制树。

Relationship 至少要能保留：

- from / to Object；
- relation；
- 生效与结束时间（适用时）；
- source / Evidence；
- confidence / uncertainty。

一个 Object 可以同时连接多个业务线、项目、人物或能力；不要把目录层级当成唯一真实关系。Relationship 词汇也受概念治理约束，避免大量同义边。

## 9. Note

`Note` 是可独立追溯的长期原子信息记录，属于 Information 层，**不是 Object，也不是 Object 的 Role**。

M2 起，符合信息契约的 Atomic Information 可以**自动吸收为 durable Note，不要求逐条人工审核**。

一个 Note 至少保留：

- statement；
- semantic type；
- concerns / related Object IDs；
- Evidence；
- context；
- confidence / uncertainty；
- 来源与处理历史。

### Note 历史

Note 的修改不得覆盖历史。后续修订应产生新 revision 或等价 append-only 历史，旧内容不能静默消失。

### Note 与已有 Object 的关系

系统把 Note 关联到已有 Object 时，应判断新信息属于：

1. **补充**：增加新的、与当前认知相容的信息；
2. **更新**：新信息说明已有认知需要调整；
3. **冲突**：新信息与当前长期认知或其他可信信息无法安全同时成立。

处理原则：

- 补充可以自动吸收；
- **更新如果目标 Object 明确、Evidence 足够、没有冲突或歧义，并且不会触发高风险结构变化，可以自动更新 World Model；**
- 冲突不得静默覆盖，应交给人类判断；
- Object 匹配不确定时不得猜测，应交给人类判断；
- Relationship 含义或连接对象不确定时不得自动建立。

## 10. Atomic Information Candidate

`Atomic Information Candidate` 是 Processing 阶段生成的原子信息候选。

它与 durable Note 的主要区别是生命周期阶段，而不是是否必须经过人工逐条确认。

```text
Atomic Information Candidate
  → contract validation
  → automatic ingestion
  → durable Note
```

不满足信息契约、Evidence 不完整或处理失败的候选，应失败或进入适当的 Residue / processing failure 边界，不应依赖人工确认掩盖质量问题。

## 11. Evidence

`Evidence` 是信息或结构化判断回到来源的可追溯依据。

音频 M1 至少保留：

```text
source
→ transcript segment
→ speaker
→ timestamp
→ excerpt
```

后续 Name、Role、Relationship、Lifecycle 更新也应能回到支持它们的 Note / Evidence。

## 12. Residue

`Residue` 是当前处理流程无法安全吸收的信息。

Residue 不是垃圾，也不是运行错误。它用于保留歧义、冲突、上下文不足、证据不足或重要性不明的信息，并作为信息消化健康度信号。

## 13. World Model Change

`World Model Change` 是会改变系统长期结构化认知的动作，例如：

- 新建或删除 Object；
- 修改 Name；
- 新增、结束或调整 Role；
- 新增、结束或调整 Relationship；
- 修改 Lifecycle。

### 13.1 自动更新边界

ArcheOS 采用**风险分级，而不是“所有变更都人工审核”**。

已有 Object 的信息更新，在同时满足以下条件时可以自动执行：

- 目标 Object 唯一且明确；
- Evidence 足够；
- 新信息与已有可信认知不冲突；
- 业务含义没有明显歧义；
- 使用的 Role / Relationship / Lifecycle 概念已经被 `CONCEPTS.md` 接受；
- 不需要创建新的 Object；
- 不涉及删除 Object；
- 不会造成仍需保留的 Object 变成孤立对象；
- 不需要模型猜测一条不确定的 Relationship。

例如，已明确存在“展厅经营”，新信息可靠地说明“9 月 1 日正式启动”，且与现有信息不冲突，可以自动补充其 Lifecycle，不要求人为点击批准。

所有自动更新都必须保存来源、Evidence 和历史，不能因为“自动”而失去可追溯性。

### 13.2 必须交给人类判断的情况

以下情况应停止自动修改，并请求人类判断：

- 新建 Object；
- 删除 Object；
- 新旧可信信息发生冲突；
- 无法确定 Note 对应哪个已有 Object；
- Relationship 的对象或业务含义不确定；
- 新增或调整 Role 时，无法清楚解释该 Role 与对象现有业务上下文、关系的联系；
- 变更可能制造孤立 Object；
- 其他需要业务取舍、而不是单纯信息更新的情况。

人类审核不要求专门前端，可以通过 AI 对话或其他 prompt 完成。

### 13.3 孤立 Object 原则

ArcheOS 应尽量避免产生与其他长期对象没有有效关系的孤立 Object。

- 新建 Object 时，优先同时说明它与已有 Object 的业务联系；
- 暂时无法建立关系时，应由人类确认为什么仍值得单独保留；
- 删除 Object 时，不应使仍需保留的其他 Object 因失去唯一有效关系而意外成为孤立 Object；
- 删除应保留必要历史与可追溯性，具体物理删除策略由后续实现决定。

## 14. Human-facing Language

**所有面向人类的内容必须使用业务语言，而不是内部技术语言。**

包括：

- 前端页面；
- AI 对人提出的问题；
- 审核请求；
- 冲突、风险和错误说明；
- 报告、摘要和建议。

目标读者按一般大学本科毕业生理解能力设计。用户无需理解数据库、schema、foreign key、object_id、mutation、repository、graph edge 等技术概念，也应该能理解：

1. 系统发现了什么；
2. 为什么重要；
3. 依据是什么；
4. 需要做什么选择；
5. 每个选择会带来什么业务后果。

例如内部可能表示：

```text
end_role(project)
add_role(business_line)
```

面向人类应表达为：

> 系统发现“展厅经营”更像一项持续经营的业务，而不是一个有明确结束时间的项目。建议将它调整为“业务线”，原来的历史记录会保留。是否调整？

内部 ID 和技术细节仅用于调试、审计、开发工具，或用户明确要求时展示。

## 15. View / View Model / Presentation

`View` 是对同一份 World Model 的人类理解投影，不是新的 Core Object。

Core 保存 Graph；View 可以把它投影成树、关系图、时间线、对象档案或决策链。

例如“向阳生长树”可以显示：

```text
私享国际家具
├─ 展厅经营 [business_line]
│  ├─ 产品库
│  └─ 销售工具
└─ 海丝金融中心家具采购 [project]
```

这棵树只是 View，不是 Core 中唯一真实父子关系。

`View Model` 是根据 View 规则计算出的前端读取结构；HTML、Markdown、React、Mobile、AI conversation 等是 Presentation / Renderer。Core Data 才是权威。

## 16. Object Resolver

`Object Resolver` 是读取时将稳定 `object_id` 解析成人类可读信息的机制，例如当前名称、显示 Role、aliases 和 status。

因此内部大量使用 ID 不影响人类展示。Object Resolver 是读取机制，不是新的 Domain Object。

## 17. Structured World Model

`Structured World Model` 是 `Object + Role + Relationship + Lifecycle + Name` 形成的长期结构化世界的统称。

它是架构层概念，不是需要创建 ID 的独立 Object。

Note / Evidence 为 World Model 提供依据；View 将 World Model 投影给人类。

## 18. 存储与概念分离

JSONL、SQLite、未来其他数据库都是持久化实现方式，不是领域概念。

- JSONL 可以是正式存储方式之一，而不仅是导出格式；
- SQLite 可以是本地第一版实现；
- 未来可以增加其他 Store / Repository adapter；
- 更换存储不能迫使 Object、Note、Role、Relationship 等语义发生变化；
- 同一次权威写入流程应避免无治理的多存储双写，防止形成多个分叉事实源。

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

真实关系通过 Relationship 表达，而不是靠目录名或 ID 编码表达。

## 20. 明确禁止的建模方式

除非新的架构决策明确修改本文件，否则不要：

- 建立 `ProjectObject`、`BusinessLineObject`、`PersonObject` 等平行底层对象体系；
- 用名称作为内部外键；
- 因改名而创建新 Object；
- 把树形展示结构当成唯一真实关系；
- 因某个页面需要分组就增加新 Core Object 类型；
- 把 Note 与 Object 混成同一类实体；
- 静默覆盖 Note 历史；
- 把前端 HTML 当成 Core 数据源；
- 让面向人的页面或提示直接暴露内部技术术语；
- 因为“方便”而把所有 World Model 更新都推给人类审核；
- 在代码中引入本文件未定义、且会改变长期语义模型的新 Core 概念而不更新文档。
