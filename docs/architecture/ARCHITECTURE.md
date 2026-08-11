# ArcheOS 系统架构说明

## 1. 核心生命周期

ArcheOS 只保留一条主生命周期：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

任何新增能力都必须说明自己服务于这条主线的哪一个阶段，不建立同义或平行生命周期。

其中 `Structured Object` 表示进入长期结构化世界模型的阶段，不意味着系统采用一组互斥的 `Person / Company / Project` 基础表。ArcheOS 的核心概念定义以 `docs/architecture/CONCEPTS.md` 为唯一权威词典。

## 2. 五个核心阶段

### 2.1 Input

接收原始信息，例如音频、PDF、图片、PPT、视频等。

原始输入必须保持不可修改并可追溯。

### 2.2 Processing

负责把原始输入转化为可理解、可审查的中间产物。

音频第一版产生：

- transcript：原始转写；
- meeting summary：整体上下文保留；
- atomic information candidates：原子信息候选；
- residue：当前无法安全吸收的信息。

会议纪要和残渣属于 Processing 的辅助产物，不是新的核心对象层，也不是平行生命周期。

### 2.3 Atomic Information / Note

形成最小的、可独立追溯的信息单元。

每个原子信息至少需要保存：

- statement；
- source evidence；
- context；
- confidence / uncertainty；
- 可能关联的 Object。

M2 起，符合信息契约的 Atomic Information Candidate 可以自动吸收为 durable `Note`，不要求逐条人工审核。

Note 属于 Information 层。后续修改必须保留历史，不允许静默覆盖旧版本。

### 2.4 Structured Object / World Model

ArcheOS 采用：

```text
Object
├─ stable object_id
├─ Name
├─ Role[]
├─ Lifecycle
└─ status

Object ── Relationship ── Object

Note ── concerns / supports ── Object / Name / Role / Relationship / Lifecycle
```

核心原则：

- `Object` 是长期稳定身份；
- `Role` 是 Object 当前或某段时间的业务解释，不是身份；
- `Name` 是面向人的可变标签，不是内部外键；
- `Lifecycle` 描述时间、结束和完成特征，与 Role 分离；
- `Relationship` 把 Object 组成 Graph；
- `Note` 属于 Information 层，不与 Object 混成同一实体；
- 多来源信息可以逐步更新同一 World Model，但必须保留 Note、Evidence、来源和历史。

当前已接受 Role：

- `person`
- `company`
- `brand`
- `project`
- `business_line`
- `event`
- `goal`
- `decision`

例如：

```text
私享国际家具
  Object A
  roles: company + brand

展厅经营
  Object B
  role: business_line
  lifecycle: ongoing

海丝金融中心家具采购
  Object C
  role: project
  lifecycle: bounded
```

如果“展厅经营”过去被理解为 Project，后来调整为 Business Line，Object ID 不变，只修改 Role 及历史。

### 2.5 World Model Change Boundary

Note 可以自动吸收，但改变长期世界结构需要更严格的治理。

系统在读取一条新 Note 并关联已有 Object 时，应区分：

```text
新增信息
  → 与已有认知相容
  → 自动关联 / 保留

需要更新
  → 新信息说明 Name / Role / Relationship / Lifecycle 等长期认知需要调整
  → 形成变更建议

存在冲突
  → 新旧信息无法安全同时成立
  → 不静默覆盖，交给人类判断
```

新建、删除或修改 Object，以及修改 Name / Role / Relationship / Lifecycle，都属于 World Model Change。当前原则是：先形成可解释的建议，获得人类授权后再执行。

审核可以直接通过 AI 对话或 prompt 完成，不依赖专门审核前端。

Object 删除还需要检查业务关系安全：不应因为删除一个 Object，使仍需保留的其他 Object 因失去唯一有效联系而意外成为孤立对象。

### 2.6 Decision

结构化信息最终用于支持目标下的判断、决策与行动，并由行动结果形成反馈。

`goal` 与 `decision` 当前已经被定义为 Object 可承担的 Role，但更完整的 Goal → Decision → Action → Feedback 行为模型仍在进入对应里程碑时逐步实现，不提前增加平行 ontology。

## 3. Persistence Boundary

ArcheOS 的领域语义不能绑定某一种数据库。

```text
Information / World Model Contracts
            ↓
Repository / Store Interface
            ↓
JSONL | SQLite | future database
```

原则：

- JSONL 可以是正式存储方式之一；
- SQLite 可以是第一版本地 World Model 实现；
- 未来可以替换或增加其他数据库；
- 更换存储方式不能改变 Object、Note、Role、Relationship、Lifecycle、Name 的业务语义；
- 审核和业务规则应位于 Repository/Store 之上，而不是写死在 SQLite adapter 内。

## 4. Core Graph 与 Human View

ArcheOS Core 保存的是 Graph，而不是强制唯一目录树。

```text
Object + Role + Relationship + Lifecycle + Name
                ↓
        Structured World Model
                ↓
          Projection / View
                ↓
             View Model
                ↓
 HTML / Markdown / React / Mobile / AI UI
```

### 4.1 View

`View` 是人类观察同一份 Core 数据的一种投影，不是新的 Core Object。

未来可以包含：

- Object Profile；
- 向阳生长树；
- Relationship Graph；
- Timeline；
- Decision View。

例如“向阳生长树”可以把同一 Graph 投影为：

```text
私享国际家具
├─ 展厅经营 [business_line]
│  ├─ 产品库
│  └─ 销售工具
└─ 海丝金融中心家具采购 [project]
```

这棵树只是一种 View，不是 Core 中唯一真实的父子关系。

### 4.2 Object Resolver

内部关系使用 `object_id`，展示层通过 Object Resolver / Read Model 获取：

- current name；
- display role；
- aliases；
- status；
- 其他人类可读信息。

因此 Object 改名或 Role 调整，不需要修改所有 Note、Relationship 或 View 中的引用。

### 4.3 Presentation 与业务语言

HTML、Markdown、AI 对话或其他前端只是 Presentation。

Core Data 才是权威，HTML 不应成为关系和对象定义的唯一存储位置。

所有面向人的内容都必须使用通俗的业务语言，包括：

- 页面标题和说明；
- AI 提示和问题；
- 审核请求；
- 冲突与风险说明；
- 报告、建议与错误提示。

默认读者是一位普通大学本科毕业生，不要求理解 `object_id`、schema、foreign key、repository、graph edge、mutation、adapter 等内部技术概念。

系统内部可以保持精确的技术表示，但在人类界面必须翻译为：发生了什么、为什么重要、依据是什么、需要做什么选择、选择后会有什么业务后果。

例如内部动作可能是：

```text
add_role(obj_x, business_line)
```

面向人类应表达为：

> 系统发现“展厅经营”更像一项持续经营的业务，而不是一个有明确结束时间的项目。是否将它调整为“业务线”？原有历史记录会保留。

## 5. Domain Agent 的位置

Sales Agent、Brand Agent、Project Agent 等领域能力不是新的核心层。

它们是建立在 Core 之上的领域解释能力，可以读取：

- Processing 产物；
- Atomic Information / Note；
- Structured World Model；
- 面向用途的 View Model。

然后产生：

- 领域报告；
- 领域判断；
- 对 Note、Name、Role、Relationship、Lifecycle、Decision 等的更新建议。

Domain Agent 不应该为自己的领域重新创建一套 Input → Processing → Object 生命周期，也不应该因为业务术语增加新的 Core 基础对象类型。

## 6. 概念治理

所有架构和实现应先查阅 `docs/architecture/CONCEPTS.md`。

规则：

1. 能复用已有概念时，不新增概念；
2. 不建立同义或平行模型；
3. 新业务词优先作为 Name、Role、Relationship、Note 或 View 表达；
4. 只有已有概念确实不足时，才通过架构决策新增概念；
5. 新增概念必须同步更新 `CONCEPTS.md` 后才能进入实现。

## 7. 当前实现边界

M1 已完成通用音频信息消化：

**音频 → 转写 → Speaker Attribution → 会议纪要 → 原子信息候选 + 残渣**

PR #5 已合并到 `main`。

M2-A 正在建立稳定 World Model 基础：

**Object / Name / Role / Lifecycle / Relationship + Resolver + Repository contract**

M2 后续进入：

```text
Atomic Information Candidate
  → automatic durable Note ingestion
  → existing Object interpretation
  → compatible addition / update / conflict detection
  → governed World Model change when needed
  → human authorization for structural changes
```

前端 Human View 暂后，优先完成自动 Note、受控 World Model Change 与后续统一上下文能力。长期阶段顺序以 `docs/development/ROADMAP.md` 为准。
