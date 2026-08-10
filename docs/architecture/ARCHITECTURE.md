# ArcheOS 系统架构说明

## 1. 核心生命周期

ArcheOS 只保留一条主生命周期：

**Input → Processing → Atomic Information → Structured Object → Decision → Feedback**

任何新增能力都必须说明自己服务于这条主线的哪一个阶段，不建立同义或平行生命周期。

## 2. 五个核心阶段

### 2.1 Input

接收原始信息，例如音频、PDF、图片、PPT、视频等。

原始输入必须保持不可修改并可追溯。

### 2.2 Processing

负责把原始输入转化为可理解、可审查的中间产物。

音频第一版可能产生：

- transcript：原始转写；
- meeting summary：整体上下文保留；
- atomic information candidates：原子信息候选；
- residue：当前无法安全吸收的信息。

会议纪要和残渣属于 Processing 的辅助产物，不是新的核心对象层，也不是平行生命周期。

### 2.3 Atomic Information

形成最小的、可独立审查、可追溯的信息单元。

每个原子信息至少需要保存：

- statement；
- source evidence；
- context；
- confidence / uncertainty；
- 可能关联的对象。

生成出来的原子信息首先是候选信息，未经过验证前不自动成为长期资产。

### 2.4 Structured Object

经过确认的信息可以逐步被长期对象吸收。

第一版核心对象保持收敛：

| 对象 | 含义 |
|---|---|
| Note | 已确认的原子信息 |
| Person | 人物 |
| Company | 公司或组织 |
| Project | 项目 |
| Event | 事件 |
| Goal | 目标 |
| Decision | 决策 |

多个来源可以逐步更新同一个对象，但必须保留来源和历史，不允许简单覆盖后失去上下文。

### 2.5 Decision

结构化信息最终用于支持目标下的判断、决策与行动，并由行动结果形成反馈。

具体的 Goal / Decision / Action / Feedback 模型在进入相应里程碑时再设计，不提前扩展 ontology。

## 3. Domain Agent 的位置

Sales Agent、Brand Agent、Project Agent 等领域能力不是新的核心层。

它们是建立在 Core 之上的“领域解释能力”，可以读取：

- Processing 产物；
- 原子信息；
- 已存在的结构化对象。

然后产生：

- 领域报告；
- 领域判断；
- 对对象更新或决策的建议。

Domain Agent 不应该为自己的领域重新创建一套 Input → Processing → Object 生命周期，也不应该因为业务术语增加新的 Core 对象类型。

## 4. 当前实现边界

当前 M1 只实现通用音频信息消化：

**音频 → 转写 → 会议纪要 → 原子信息候选 + 残渣 → 人工审核**

M1 不自动进入 Structured Object，也不做销售、品牌、项目等领域专用分析。

当前实现规格以 GitHub Issue #4 为准；长期阶段顺序以 `docs/development/ROADMAP.md` 为准。