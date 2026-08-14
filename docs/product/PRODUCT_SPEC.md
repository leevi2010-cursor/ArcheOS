# ArcheOS 产品说明

## 1. 文档职责

本文件定义 ArcheOS **长期是什么、为谁创造什么价值、产品边界是什么**。

它不是阶段计划，也不决定某项技术能力何时开发。产品阶段与 Stage Gate 以 `docs/product/PRODUCT_ROADMAP.md` 为准；为了通过当前 Product Stage 所需的技术演化以 `docs/development/ROADMAP.md` 为准；单次交付以当前 GitHub Issue 为准。

权威链：

```text
PRODUCT_SPEC
  我们最终想成为什么
        ↓
PRODUCT_ROADMAP
  为了成为那个产品，依次必须证明什么
        ↓
DEVELOPMENT_ROADMAP
  为了通过当前阶段，系统还缺什么能力与验证
        ↓
GITHUB ISSUE
  这一轮具体交付什么
```

真实使用、Experiment、Issue 与 PR 产生的 Evidence 可以通过 Roadmap Feedback 反向挑战上层假设；产品方向不是不可修正的单向命令。

---

## 2. 产品定义

ArcheOS 是一个开放、可治理的长期认知与信息系统，用于把混乱、异构、持续变化的信息输入逐步转化为可追溯、可复用的长期认知，并支持 Human 与外部 Agent 基于这些认知进行判断、决策、行动与反馈。

ArcheOS 的核心不是某一种业务应用，也不是单纯的文件管理、向量检索或聊天记忆，而是一条统一、可审计的信息与认知生命周期：

```text
Input
→ Processing / Representation
→ Atomic Information + Claim + Evidence
→ Structured World Model
→ Context
→ Judgment / Decision / Action
→ Feedback
```

具体 canonical concepts 以 `docs/architecture/CONCEPTS.md` 为准；本文件只定义产品方向，不复制概念词典。

---

## 3. 长期用户价值

ArcheOS 希望帮助个人或组织获得一套自己能够长期拥有和治理的认知资产：

- **统一沉淀**：让重要信息不再散落于聊天、文件夹、录音、业务系统和不同 Agent 的私有记忆中；
- **来源可追溯**：重要结论能够回到 Source、Evidence、时间、责任主体与历史版本；
- **长期可演化**：允许修订、补充、时间变化、冲突、不确定和失效，而不是只保存一个被覆盖后的“最终答案”；
- **跨模型复用**：同一套受治理 Context 可以被 Codex、GPT、Claude、本地模型或未来 Agent 使用；
- **减少重复解释**：用户不需要在每一次新会话中重新讲述长期背景；
- **提高判断质量**：系统最终价值不止是“记住”，而是让 Human + Agent 在更完整、更可靠、更可检查的 Context 上思考和行动；
- **保留控制权**：Agent 可以整理、建议和执行获授权的操作，但不能把自己的推断静默升级为长期事实、正式 Goal 或 consequential Decision。

---

## 4. 长期产品结构

ArcheOS 优先保持三层边界：

```text
ArcheOS Core
信息、Evidence、World Model、Context、治理与审计
        ↓
External Agent
理解、推理、建议与受授权执行
        ↓
Domain Product
围绕一个明确用户问题提供可直接使用的产品体验
```

### ArcheOS Core

Core 提供长期认知基础设施，不绑定销售、品牌、项目管理、Founder 等具体业务场景，也不要求普通业务用户理解内部信息模型。

### External Agent

ArcheOS 不以开发自己的通用 Agent 为长期前提。推理模型和 Agent runtime 可以替换；ArcheOS 保存的是用户长期拥有、可追溯、可治理的认知资产与使用边界。

### Domain Product

销售助手、Founder 决策助手、项目助手、Research、Operations 等都可以在同一 Core 上形成领域产品。Domain Product 解决明确的用户问题，但不得重新建立自己的长期 Information / World Model truth。

哪个 Domain Product 最先形成商业产品，必须由真实使用与市场 Evidence 决定，不在 Product Spec 中预先锁定。

---

## 5. 产品边界

ArcheOS 不是：

- 一个随意堆放文件的总目录；
- 某个模型私有、不可迁移的黑盒记忆；
- 只依靠 embedding 相似度工作的普通 RAG 知识库；
- 把 Agent inference 自动当成用户事实或 World Model truth 的自动知识库；
- 为每个业务场景分别维护一套 Person / Company / Project / Note / Decision 数据模型的平台；
- 为了“完整”而提前建设所有 SaaS、企业协作、billing、marketplace 和 orchestration 能力的通用框架；
- 一个必须自己拥有模型或 Agent runtime 才能成立的封闭产品。

大型文件、频繁变化的数据或必须由外部业务系统维护的记录，可以继续由适合的系统作为权威；ArcheOS 只在明确边界内保存 Source、Evidence、治理状态、必要认知和访问方式。

---

## 6. 长期产品原则

1. **用户长期资产优先于模型生命周期**：模型、SDK、Agent 和 UI 可以替换，长期认知不能因此丢失或重建一遍。
2. **来源与历史优先于方便覆盖**：重要认知必须能够回到 Evidence；修改不应抹掉关键历史。
3. **事实、Claim、Judgment、Decision 分层**：系统知道“谁说了什么”不等于系统认定“现实就是这样”。
4. **Human 保留 consequential authority**：重要 Goal、Decision、删除、权限和高风险变化保留人工裁决边界。
5. **一个长期认知 Core**：Domain Product、输入格式、Provider 和 Agent 不建立平行 truth。
6. **开放、可迁移、storage-independent**：优先采用开放、可读、可替换的 contract，避免被单个存储、模型或软件锁定。
7. **真实世界 Evidence 驱动演化**：产品与架构可以被真实数据、用户行为和失败结果修正，而不是因为早期设计写进文档就永远不变。
8. **先证明价值，再建设规模化基础设施**：技术完整度不是产品成熟度；未来能力必须由当前 Product Stage 的真实 Evidence Gap 触发。

---

## 7. 产品路线与开发关系

产品设计遵循：

```text
长期产品方向
→ 当前 Product Stage
→ Stage Gate / Evidence Gap
→ Development Roadmap
→ Experiment / Issue / PR
→ Real-world Evidence
→ Roadmap Feedback
```

因此，开发默认不从“还缺哪个功能”开始，而从以下问题开始：

> 当前 Product Stage 正在试图证明什么？还有什么 Evidence 不足？

Product Roadmap 当前状态、长期商业化阶段与阶段进入条件以 `docs/product/PRODUCT_ROADMAP.md` 为唯一产品路线权威。