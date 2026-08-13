# ArcheOS 信息吸收与长期认知治理规则

## 1. 文档职责

本文件定义 ArcheOS 在运行过程中如何处理新信息、如何更新长期认知、什么时候自动执行、什么时候需要人类判断。

它是**产品行为规则**，不是概念词典，也不是数据库实现说明。

文档分工：

- `docs/architecture/CONCEPTS.md`：定义概念是什么；
- `docs/product/INFORMATION_GOVERNANCE.md`：定义系统遇到新信息时应该怎么处理；
- `docs/architecture/ARCHITECTURE.md`：定义这些能力由哪些系统边界承载；
- `AGENTS.md`：约束 Architect / Executor 如何遵守上述权威文档；
- GitHub Issue：定义一次具体开发要实现哪些规则。

---

## 2. Source 接入与 Managed Source 权威

Source 的接入分为临时发现和正式准入两个阶段：

```text
外部文件
  → 只读 intake candidate
  → 用户明确准入
  → 完整字节复制
  → size / content_hash 校验
  → Managed Source + 稳定 source_id
```

运行规则：

- 扫描目录默认只读，不把扫描记录升级为正式 Source；
- 只有复制到系统受控位置并通过字节校验后，才创建 Managed Source 和 `source_id`；
- 第一版本地 Managed Source 根目录是 `01_inbox/` 的受控 Source 区，实际字节和 Manifest 保持本地、Git-ignored；
- `source_id` 是一份不可变受管字节快照的稳定身份，不由外部路径、文件名或 `content_hash` 单独定义；
- `content_hash` 用于完整性校验和存储去重，不自动合并不同接入语境；
- 后续 Processing、Evidence 和 Normalized Representation 只引用 Managed Source，不依赖外部绝对路径；
- 外部原文件和 Managed Source 可以物理共存，但不计算为两份独立 Evidence；
- 归档完成后系统不自动跟踪、同步或重新处理外部文件变化；
- 用户需要更新时必须回到系统显式重新接入，创建新的 Source；
- 已被 Evidence 引用的 Source 字节不可由同一 `source_id` 原地覆盖；
- `ingested_from` 只保留可失效的历史接入提示，不参与 Evidence 定位；
- Handoff Marker 只能在 Managed Source 已复制、校验且 Manifest 持久化后，经用户另行授权写入；它不是 Source、Evidence 或同步机制。

当前 `process_audio()` 的外部 path、文件名 stem 与 SHA-256 前缀派生 ID、以及 processing manifest 中的绝对路径，属于 M1 legacy provenance。它们保留为后续 Source 身份迁移对象，本规则不授权当前 Issue 修改代码。

---

## 3. Atomic Information Candidate → Atomic Information

符合信息契约的 Atomic Information Candidate 可以自动进入长期 Atomic Information，不要求逐条人工审核。

要求：

- 保留来源和 Evidence；
- 保留 context 与 confidence / uncertainty；
- 能够明确识别声明主体 / 立场时，保留 Claim；
- 重复处理同一信息时应避免无意义重复；
- Atomic Information 后续修订不得静默覆盖历史，应保留版本或等价的可追溯历史。

不满足信息契约、Evidence 不完整或处理失败的内容，不应通过人工“确认一下”来掩盖质量问题；应回到 Processing / Residue / failure 边界处理。

---

## 4. Claim 治理

Claim 表示“谁以什么立场表达了什么”，**不是系统已经确认的事实**。

运行时遵循：

- Claim 可以自动进入长期 Information Layer；
- 相互矛盾的 Claim 可以同时长期保存，不为了得到唯一答案而覆盖或删除其中一条；
- `Atomic Information.confidence` 默认表示信息提取 / 语义理解置信度，不是真实性概率；
- Claim 的 attribution confidence 表示归因正确性的置信度，不是真实性概率；
- 第一版不建立 claimant reputation / source authority 打分体系，不根据“这个人通常更可信”自动计算事实概率；
- 如果 World Model 更新是否成立取决于“应该相信哪个 Claim”，必须进入冲突 / 人类判断，而不是模型自行选边；
- Claimant 尚未解析为 Object 时，可以继续保留 Source / speaker 归因，不得为了归因方便自动创建 Person Object。

Claim 可以成为 World Model 变化的依据之一，但是否写入 World Model 仍由后续消化、Evidence、冲突检查和安全更新规则决定。

---

## 5. Atomic Information 与已有 Object 的消化

新的 Atomic Information 涉及已有 Object 时，系统先判断业务影响：

### 4.1 补充

新信息与当前认知相容，只是增加更多事实、背景、Claim 或细节。

处理：**自动吸收 Information；不需要为了“保存信息”强行修改 World Model。**

### 4.2 更新

新信息说明已有长期认知需要调整，例如名称、Role、Relationship 或 Lifecycle 的某项信息需要变化。

处理：满足“安全自动更新”条件时可以自动执行；否则交给人类判断。

### 4.3 冲突

新信息、Claim 或 Evidence 与已有可信认知无法安全同时成立。

处理：**不得静默覆盖，保留所有来源并交给人类判断。**

---

## 6. 安全自动更新

已有 Object 的更新在同时满足以下条件时可以自动执行：

- 目标 Object 唯一且明确；
- Evidence 足够；
- 新信息与已有可信信息 / Claim 不冲突；
- 业务含义清楚，没有明显歧义；
- 使用的是已经批准的 Role / Relationship / Lifecycle 语义；
- 不需要创建新 Object；
- 不涉及删除 Object；
- 不会使仍需保留的 Object 变成孤立对象；
- 不需要模型猜测一条不确定的 Relationship；
- 如果信息带有 Claim，其结构变化不依赖尚未解决的“该相信谁”判断。

例如：系统已经明确知道“展厅经营”是哪一个长期对象，新的可靠信息说明“9 月 1 日正式启动”，且与已有信息不冲突，可以直接补充其开始时间，不要求人类再次确认。

自动更新仍必须保存来源、Evidence、Atomic Information / Claim 和历史。

---

## 7. 必须交给人类判断的情况

以下情况停止自动修改，并请求人类判断：

- 新建 Object；
- 删除 Object；
- 新旧可信信息或不同 Claim 发生冲突，且 World Model 更新依赖选择其中一方；
- 无法确定新的 Atomic Information 对应哪个已有 Object；
- Relationship 的对象或业务含义不确定；
- 新增或调整 Role 时，无法清楚说明它与对象当前业务上下文、现有关系的联系；
- 变更可能使仍需保留的 Object 变成孤立对象；
- 涉及真正的业务取舍，而不是证据明确的信息更新；
- 需要比较不同 claimant / source 的可信度才能得出结论。

人类判断可以直接通过 AI 对话或 prompt 完成，不要求专门审核前端。

---

## 8. Relationship 治理

当前允许写入 World Model 的通用 Relationship 语义以 `CONCEPTS.md` 为唯一词汇权威。

第一版只使用：

```text
part_of
member_of
responsible_for
depends_on
related_to
```

运行时要求：

- 两端必须先解析到已有 Object；
- 不为了建立关系而自动把普通名词升级成 Object；
- 关系方向必须明确；
- 不同时持久化一条关系及其纯查询意义上的反向副本；
- 能用更具体已批准关系表达时，不应为了省事全部写成 `related_to`；
- 现有词汇不足时保留 Atomic Information / Claim，并交由 Architect 判断是否需要扩展，不允许 Agent 临时发明新的 relation 值。

后续是否增加 `supports`、`participates_in`、`serves` 等关系，以真实 B2 / B4 数据反复出现的需求为依据，不提前扩展。

---

## 9. 新建 Object 与孤立对象

ArcheOS 应尽量避免创建没有业务联系的孤立 Object。

新建 Object 时：

- 优先同时说明它与已有 Object 的业务关系；
- 如果暂时无法建立关系，应向人类说明为什么这个对象仍值得单独长期保留；
- 人类确认后才进入长期 World Model。

系统不能因为录音或文档里出现了一个名词，就自动把它升级成 Object。

---

## 10. 删除 Object 与关系安全

删除 Object 必须由人类确认。

删除前检查：

- 是否仍有重要 Atomic Information / Claim / Evidence / 历史依赖这个 Object；
- 删除后是否会让其他仍需保留的 Object 因失去唯一有效联系而变成孤立对象；
- 是否需要先建立新的业务关系，或一起处理相关 Object。

如果删除会意外制造孤立对象，则不得直接删除。

具体采用逻辑删除还是物理删除属于后续实现决策，但历史与可追溯性不能被无意破坏。

---

## 11. 面向人类的表达规则

所有面向人类的内容必须使用**通俗业务语言**，而不是内部技术语言。

适用于：

- AI 对人提出的问题；
- 人工审核请求；
- 冲突和风险说明；
- 前端页面；
- 报告、摘要和建议；
- 面向非开发者的错误与警告。

默认读者是一名普通大学本科毕业生。用户不需要理解 `object_id`、schema、foreign key、repository、graph edge、mutation、adapter 等内部技术概念。

任何需要人类判断的内容，都应说明：

1. 系统发现了什么；
2. 为什么重要；
3. 依据是什么；
4. 有哪些选择；
5. 每个选择会有什么业务后果。

当冲突来自不同 Claim 时，面向人类应说明“谁表达了什么、依据来自哪里、为什么目前不能安全合并”，而不是只显示内部冲突代码。

例如内部动作可能是：

```text
end_role(project)
add_role(business_line)
```

面向人类应表达为：

> 系统发现“展厅经营”更像一项持续经营的业务，而不是一个有明确结束时间的项目。建议把它调整为“业务线”，以前的历史记录会继续保留。是否调整？

技术 ID 和实现细节只在开发、调试、审计或用户明确要求时展示。

---

## 12. 存储无关性

上述业务规则不得写死在某一种数据库 Adapter 中。

无论底层使用 JSONL、SQLite 或未来其他数据库：

- 自动更新边界一致；
- 人工判断边界一致；
- Claim / Evidence 与历史要求一致；
- 孤立对象保护规则一致；
- 人类表达规则一致。

存储方式可以替换，业务规则不能随存储实现而漂移。
