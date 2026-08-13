# ArcheOS 长期愿景

> 本文记录长期方向，用于检查当前架构是否把未来堵死。
>
> **它不是当前 Roadmap、不是 Architecture Contract、不是 Executor 工作清单，也不会自动产生 GitHub Issue。**
>
> 当前开发仍以真实业务数据验证、信息消化质量和个人长期使用体验为优先。

## 1. 当前阶段：个人认知与决策增强系统

ArcheOS 首先服务单个用户和其日常 Agent。

核心目标不是收集尽可能多的信息，而是把真实世界中杂乱的资料、对话、录音、文件和业务记录：

```text
真实输入
→ 可追溯 Source
→ 可替换 Representation
→ Atomic Information / Claim / Evidence
→ Consolidation
→ Object / Relationship / World Model
→ Context
→ 成为人和 Agent 做判断的可靠依据
```

这一阶段以真实业务数据持续压力测试：是否丢失信息、错误合并、错误结构化；Object / Relationship 是否合理；Context 是否比原始资料更清楚、更适合判断。

只有当用户自己长期使用顺畅、认知质量稳定之后，才进入下一层扩张。

## 2. 下一阶段：主动认知与主动决策增强

当系统已经能比较可靠地认识当前世界后，下一阶段探索从被动消化升级为主动认知：

```text
Context
→ 发现问题 / 机会
→ 主动探索
→ 主动学习
→ 更新认知
→ 主动提出 Goal / Decision Proposal
→ 人类反馈
→ 行动结果重新进入 Information lifecycle
```

关键研究问题是：**系统为什么要主动做某件事？**

可能的驱动力包括但不限于：

- 生存 / 风险压力；
- 增长倾向；
- Vision / Goal；
- Red Line / Constraint；
- 当前状态与目标之间的偏差；
- 长期未解决的问题或高价值未知项。

这些概念目前仅是研究方向。不得提前建立 `MotivationEngine`、`Drive`、`ValueSystem`、`CausalGraph` 等 Core 模型。

应先在真实经营场景中观察：什么时候系统应该主动调查、主动提醒、主动提出决策；什么时候应该保持安静。再由真实证据决定最小 contract。

## 3. 多用户与社区：成熟后的扩展

只有在第一位用户已经长期使用顺畅、系统形成稳定的个人认知和决策增强闭环之后，再考虑第二个、第三个用户。

推荐演化顺序：

```text
单个用户稳定使用
↓
第二 / 第三个用户验证可迁移性
↓
识别哪些经验可以抽象为通识
↓
Community Knowledge
↓
多租户与社区治理
```

长期社区方向：

1. 每个租户通过长期对话和业务数据逐层形成自己的认知 / 世界观；
2. 租户可以加入共享社区，并读取经过治理的行业知识和通识；
3. 租户的具体私有信息不会直接成为社区知识；
4. 私有经验只有经过抽象、脱敏、治理和批准后，才可能形成 Community Knowledge；
5. 社区知识允许被挑战、修订、降权和淘汰；
6. 加入社区代表获得读取共享知识的能力，不代表把社区知识自动复制为租户自己的事实；
7. 多租户 SaaS、社区存储和共享治理均属于成熟后的基础设施阶段，不是当前技术前置。

## 4. 长期存储方向（参考，不作为当前选型授权）

未来若进入 SaaS / Multi-tenant 阶段，当前倾向是保持现有 storage-independent contracts，通过 Adapter 演化：

```text
Service / API
+ transactional structured store（如 PostgreSQL）
+ immutable blob/object storage（如 TOS / S3-compatible storage）
+ rebuildable retrieval indexes
```

原则：

- Blob storage 不替代结构化数据库；
- 向量索引不是业务 truth；
- storage 技术不得改变 Source / Atomic Information / Object identity；
- 当前本地 Repository / Store 应保持可替换，避免为了未来 SaaS 提前重写 Core；
- 在真正出现第二、第三个用户之前，不因为长期愿景提前建设完整 multi-tenant infrastructure。

## 5. 当前架构的未来兼容原则

长期愿景只对当前架构提出以下“不要堵死未来”的约束：

- Identity 不依赖本机路径；
- Domain contract 与 storage mechanism 分离；
- 原始 Source、结构化认知、派生索引分层；
- 私有信息与可共享通识保持边界；
- Agent 推断不能自动升级为事实、Goal 或 consequential Decision；
- 所有重要认知尽可能可回到 Evidence；
- 未来可以替换 storage adapter，而不重新定义领域语义。

## 6. 使用本文的规则

本文只用于两个目的：

1. 检查当前架构是否无意中把长期方向堵死；
2. 在未来阶段到来时，与当时的真实使用结果做对比，判断哪些愿景成立、哪些应被修改或放弃。

**不得因为本文存在，就要求当前 Executor 提前实现多租户、社区、主动 Agent、复杂动力模型、云数据库或远程存储。**
