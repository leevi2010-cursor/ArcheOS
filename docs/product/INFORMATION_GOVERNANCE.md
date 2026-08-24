# ArcheOS 信息吸收与长期认知治理规则

## 1. 文档职责

本文件定义 ArcheOS 在运行过程中如何处理新信息、如何更新长期认知、什么时候自动执行、什么时候需要人类判断。

它是**产品行为规则**，不是概念词典，也不是数据库实现说明。

文档分工：

- `docs/architecture/CONCEPTS.md`：定义概念是什么；
- `docs/product/INFORMATION_GOVERNANCE.md`：定义系统遇到新信息时应该怎么处理；
- `docs/architecture/ARCHITECTURE.md`：定义这些能力由哪些系统边界承载；
- `AGENTS.md`：约束 ChatGPT Product / Technical Lead 与 Codex Executor / Developer 如何遵守上述权威文档；
- GitHub Issue：定义一次具体开发要实现哪些规则。

本文件不维护当前 Issue 顺序或 runtime 实现状态；这些分别以 `docs/development/ROADMAP.md`、GitHub Issue 和代码为准。

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

历史 package / schema 若仍保留早期 path/hash provenance，只作为已实现 legacy compatibility 读取，不改变上述当前 Source 权威，也不得成为新设计继续使用旧身份语义的理由。

---

## 3. Atomic Information Candidate → Atomic Information

符合信息契约的 Atomic Information Candidate 可以自动进入长期 Atomic Information，不要求逐条人工审核。

要求：

- 保留来源和 Evidence；
- 保留 context 与 confidence / uncertainty；
- 能够明确识别声明主体 / 立场时，保留 Claim；
- 能够明确识别为可检验、尚未成为稳定知识的命题时，可以按 canonical Hypothesis 语义保存；
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

## 5. Hypothesis 治理

Hypothesis 是 Atomic Information 的 canonical 语义形态，用于保存**尚未成为稳定知识、但可以被后续 Evidence / Feedback 支持、反对、修订或淘汰的可检验命题**。

运行时遵循：

- 影响 Judgment / Decision 的关键 Hypothesis 必须显式保存，不能只藏在 Prompt、自由文本或模型私有 chain-of-thought 中；
- Hypothesis 应尽可能记录 supporting Evidence、challenging / counter Evidence、适用 scope、预期可观察结果和后续 Feedback；
- 新 Evidence / Feedback 对 Hypothesis 的结果可以是 `supports / challenges / inconclusive` 或等价语义，不能强迫每次反馈都给出二元真/假；
- Hypothesis 被现实支持到什么程度**不得复用 `Atomic Information.confidence`**；后者仍只表示抽取 / 语义理解置信度；
- 不能因为模型认为 Hypothesis 很可能正确，就自动把它升级成 World Model fact、Policy、Pattern、Protocol 或 Principle；
- Hypothesis 被反对、修订或淘汰时仍保留历史与 Evidence，不删除旧版本来制造“从未判断错过”的假象；
- 当某个 Hypothesis 经过多个独立场景反复支持，需要沉淀为 Pattern / Protocol / Policy / Principle 时，必须通过相应治理创建或修订目标结构的新版本，并保留 Hypothesis / Evidence / Feedback provenance；不得原地改类型。

一次 Decision 成功不自动证明它依赖的所有 Hypothesis；一次 Decision 失败也不自动否定所有 Hypothesis。复盘必须按可观察结果与 Evidence 分别判断。

---

## 6. Atomic Information 与已有 Object 的消化

新的 Atomic Information 涉及已有 Object 时，系统先判断业务影响：

### 6.0 默认按相关批次理解

当同一文档、会话或其他边界清楚的输入形成多条 Atomic Information 时，默认把这些相关信息作为一个有序批次交给同一个 Interpretation Provider 理解，而不是每条信息单独调用一次模型。

同一相关批次仍由一个 Agent 在完整有界上下文中理解，不按消息或片段拆给多个 Agent。不同、完整且彼此独立的会话或 Representation 可以有限并行进行只读语义分析；并行只生成隔离、可恢复的结果。Information 保存、Identity 判断、Governance、Object / Relationship 更新、Proposal / Journal 与 checkpoint 始终按来源计划顺序串行执行。该执行边界不得降低 Evidence、冲突、不确定性或 Human Judgment 要求。

批量理解必须同时满足：

- 一次 Provider 调用返回与输入 `atomic_information_id` 顺序严格一致的一组判断，不得缺失、重复、乱序或混入未知 ID；
- 整批结果完成结构校验并持久保存后，系统才按输入顺序写入长期状态；
- 批量结果保存前的准备必须保持只读；旧收据恢复、身份归位和其他可能改变长期状态的动作统一延后到顺序应用阶段；
- 每次顺序写入都保存恢复进度；中断后复用已经保存的批次结果继续，不得再次调用模型；
- Provider 在完整结果保存前失败时，本批次不得产生由该结果驱动的长期写入；
- 冲突、不确定、Evidence 不足或高影响结构变化进入现有 Human Judgment / Change Proposal 边界，不得让正常项退化为逐条模型调用；
- 单条 API 可以继续兼容，但多条业务入口不得静默回退为 N 次单条 Provider 调用。

批次、收据与恢复游标只是 Processing / Audit 技术状态，不是新的 Core concept 或第二份业务 truth。批量理解仍由单一 Agent 在同一有界上下文中完成，不以多 Agent 并行替代上下文一致性。

### 6.1 补充

新信息与当前认知相容，只是增加更多事实、背景、Claim、Hypothesis 或细节。

处理：**自动吸收 Information；不需要为了“保存信息”强行修改 World Model。**

### 6.2 更新

新信息说明已有长期认知需要调整，例如名称、Role、Relationship 或 Lifecycle 的某项信息需要变化。

处理：满足“安全自动更新”条件时可以自动执行；否则交给人类判断。

### 6.3 冲突

新信息、Claim 或 Evidence 与已有可信认知无法安全同时成立。

处理：**不得静默覆盖，保留所有来源并交给人类判断。**

Hypothesis 与当前 World Model 不一致时，首先把它当作待验证命题，不把“提出不同解释”本身当成需要立即重写 World Model 的冲突。

### 6.4 Atomic Information → Event / Timeline 业务归并

Atomic Information 是可追溯的证据层；Timeline 中的 Event 回答“业务上发生了什么”。两者不得默认一一对应。

- 同一参与者、业务事项 / 交易 / 协作动作与相容时间上下文中的提问、答复、确认、补充和状态变化，应优先归并为一个 Event，并共同引用相关 Atomic Information / Evidence；
- 纯确认语、地址补充、追问、范围细节或同一安排的后续说明，不单独成为 Event，除非它本身造成独立业务状态变化；
- 业务事项、参与者组合、时间窗口或状态变化确实独立时才拆分 Event；不得为了减少数量而合并互相冲突、不同交易、不同人物或不可兼容时间的内容；
- 不猜测时间、身份、地点、因果或状态；无法证明的内容继续显示为冲突、不确定或未知；
- 重复活动不得自动升级为 Project / Business Line / Pattern / Protocol。

业务归并是 Processing / Projection 行为，不创建 EventGroup、Cluster、新 Store 或平行的业务 truth。

---

## 7. 安全自动 World Model 变更

World Model 的自动变更不再以“是否涉及新建 Object”作为一刀切门槛，而按**身份风险**与**结构变更风险**分别治理。

### 7.1 已有 Object 的安全自动更新

已有 Object 的更新在同时满足以下条件时可以自动执行：

- 目标 Object 唯一且明确；
- Evidence 足够；
- 新信息与已有可信信息 / Claim 不冲突；
- 业务含义清楚，没有明显歧义；
- 使用的是已经批准的 Role / Relationship / Lifecycle 语义；
- 不涉及删除、合并 Object；
- 不需要模型猜测一条不确定的 Relationship；
- 如果信息带有 Claim，其结构变化不依赖尚未解决的“该相信谁”判断；
- 如果结构变化依赖某个尚未验证的 Hypothesis，则不能仅凭该 Hypothesis 自动执行。

例如：系统已经明确知道“展厅经营”是哪一个长期对象，新的可靠信息说明“9 月 1 日正式启动”，且与已有信息不冲突，可以直接补充其开始时间，不要求人类再次确认。

### 7.2 新 Object 的安全自动创建

新 Object 可以在通过第 10 节的 **Identity Gate** 后自动创建，但自动创建只确认“存在一个值得长期稳定引用的身份”，不同时自动确认不必要的 Role、Relationship、Lifecycle 或其他业务事实。

自动创建和后续结构更新必须分别留痕；不能因为 Object 已自动创建，就把同一轮模型推断的其他结构全部视为已确认。

所有自动 World Model 变更仍必须保存来源、Evidence、Atomic Information / Claim / Hypothesis 和历史，并写入 Change Journal 或等价审计链。

---

## 8. 必须交给人类判断的情况

以下情况停止自动修改，并请求人类判断：

- 删除或合并 Object；
- 新 Object 身份与一个或多个已有 Object 存在合理重复候选；
- 无法唯一判断新的 Atomic Information 对应哪个已有 Object；
- 新 Object 的身份只能依赖模糊名称、代词、弱上下文或模型猜测；
- 冲突 Evidence 直接影响“这是同一个对象还是不同对象”的身份判断；
- 创建 Object 的同时必须决定高影响 Role、Relationship、Lifecycle 或其他 consequential structure；
- Relationship 的对象或业务含义不确定；
- 新增或调整 Role 时，无法清楚说明它与对象当前业务上下文、现有关系的联系；
- 涉及真正的业务取舍，而不是证据明确的信息更新；
- 需要比较不同 claimant / source 的可信度才能得出结论；
- 需要把尚未验证的 Hypothesis 当作既定事实才能完成 consequential change；
- 错误身份会直接影响 consequential Decision / Action，且当前 Evidence 不能把风险降到可接受范围。

人类判断可以直接通过 AI 对话或 prompt 完成，不要求专门审核前端。

如果 Human 在正常工作中已经明确引用、命名或纠正一个长期对象，并且当前上下文能唯一确定该身份，这个明确表达本身可以作为人类判断；系统不应再追加一次“是否创建这个 Object”的重复确认。

---

## 9. Relationship 治理

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
- 现有词汇不足时保留 Atomic Information / Claim / Hypothesis，并交由 ChatGPT Product / Technical Lead 判断是否需要扩展；在 `CONCEPTS.md` 修改通过前，不允许 Agent 临时发明新的 relation 值。

Object 通过 Identity Gate 自动创建，不等于任何 Relationship 自动成立。Relationship 仍按本节独立判断。

是否增加新的通用 Relationship，只以真实数据反复证明的缺口为依据，不提前扩展。

---

## 10. 新建 Object：Identity Gate

ArcheOS 不要求每一个新 Object 都经过人工审批。新建 Object 的核心问题是：**是否已经有足够 Evidence 证明这里存在一个值得长期保持稳定身份、并且与已有 Object 不重复的对象。**

`Identity Gate` 是治理规则名称，不是新的 Core concept，不创建独立 ID、Store 或生命周期。

### 10.1 五种处理结果

系统遇到可能的长期对象时，优先按以下顺序处理：

```text
明确已有身份
→ 自动绑定已有 Object

明确新身份 + 值得长期保持 + 低风险
→ 自动创建最小 Object

证据暂时不足
→ 继续积累未匹配 Atomic Information / 技术性 emergence candidate

身份歧义 / 重复风险 / consequential
→ 人类判断

不值得长期保持身份
→ 只保留 Atomic Information
```

Object 不是所有 Information 最终必须挂载的容器。信息无法安全绑定 Object 时，可以长期保持未绑定状态。

### 10.2 自动创建只建立“最小 Object”

自动创建首先只确认稳定身份。第一步最多建立：

```text
stable object_id
+ 最小 Name（仅当 Evidence 明确支持）
+ supporting Atomic Information / Evidence / provenance
```

创建 Object **不等于**确认：

- 它一定属于某个 Role；
- 它与某个 Object 一定存在某条 Relationship；
- 某个 Lifecycle 状态已经成立；
- 关于它的所有 Claim / Hypothesis 都是真的。

Role、Relationship、Lifecycle 与其他结构事实继续分别走各自的 Evidence 和 Governance。

### 10.3 自动创建最低条件

只有同时满足以下条件，才可以自动创建最小 Object：

1. **长期身份价值明确**：它预计会被后续 Information、Context、Decision 或 Action 再次引用，而不是一次性动作、普通主题、属性值或模糊代词；
2. **身份 Evidence 明确**：Evidence 能支持“这是哪个对象”，不能只依赖模型 confidence；
3. **先查重**：已执行 current Name、historical Name / alias、稳定 external ID、已知关系与安全 normalization 等 existing-object resolution，没有合理的已有 Object 候选；
4. **不要求猜结构**：创建最小 Object 不需要同时猜测关键 Role、Relationship 或 Lifecycle；
5. **低业务后果**：创建这个身份本身不会直接触发 consequential Decision / Action 或外部写入；
6. **provenance 完整**：能够说明为什么认为它是长期对象、依据哪些 Atomic Information / Evidence；
7. **幂等**：相同身份 Evidence 的精确重试不能制造第二个 Object。

强身份信号可以包括稳定 external ID、项目 / 合同 / 客户编号、明确系统 identity 等。没有强 ID 时，可以由多次一致出现、稳定名称、相同上下文、相容关系与多条 Evidence 逐步积累到 Identity Gate。

不得把 `Atomic Information.confidence > 某阈值` 直接等价为“可以建 Object”。confidence 主要表达抽取 / 语义理解正确性，不是身份真实性概率。

### 10.4 Evidence 不足时先积累，不急着审核

潜在 Object 第一次出现时，如果身份价值或查重证据还不足，不要求立即创建，也不要求立即把问题推给 Human。

系统可以继续保留未匹配 Atomic Information，并使用技术性的 emergence candidate / grouping 积累：

- supporting Information；
- 不同 Source / 时间的重复出现；
- 稳定名称 / external ID；
- possible existing matches；
- unresolved identity questions。

这些技术性 candidate 不成为新的业务 Core，也不要求每条进入人工审核队列。

当后续 Evidence 足够时：

- 明确已有 → 自动绑定；
- 明确新身份且低风险 → 自动创建最小 Object；
- 仍有真正歧义 / 高风险 → 再升级给 Human。

### 10.5 Relationship 与孤立对象

ArcheOS 仍应尽量避免无业务意义的孤立 Object，但**“暂时没有 Relationship”不再自动成为禁止创建的理由**。

如果一个新身份本身已经明确、具有长期引用价值且 provenance 完整，可以先创建最小 Object，Relationship 等后续 Evidence 足够时再建立。

这类 Object 至少必须有 supporting Atomic Information / Evidence，不能因为模型觉得“以后可能有用”就凭空创建。

### 10.6 Human 明确表达避免重复确认

如果 Human 在正常业务对话、纠正或命名中已经清楚表达某个长期对象，并且系统能够唯一解析该身份，则该 Human 表达可以直接满足人工判断要求。

例如 Human 已明确说“海丝金融中心这个项目……”，且上下文不存在第二个合理身份候选，系统不应再问一次“是否创建海丝金融中心 Object？”才能继续。

如果 Human 的表达本身仍有多个合理候选，则继续请求澄清，而不是把“来自 Human”误当作身份必然唯一。

### 10.7 自动创建后的纠错

自动创建不意味着 Object 永远正确。后续 Evidence 若显示身份重复、拆分错误或错误绑定：

- 保留原 supporting Information / Evidence 与 Change Journal；
- 停止继续扩大有问题的结构；
- merge / delete / 需要改变稳定身份边界的操作交给 Human 判断；
- 不通过静默删除或原地改 `object_id` 抹掉历史。

---

## 11. 删除 Object 与关系安全

删除 Object 必须由人类确认。

删除前检查：

- 是否仍有重要 Atomic Information / Claim / Hypothesis / Evidence / 历史依赖这个 Object；
- 删除后是否会让其他仍需保留的 Object 因失去唯一有效联系而变成孤立对象；
- 是否需要先建立新的业务关系，或一起处理相关 Object。

如果删除会意外制造孤立对象，则不得直接删除。

具体采用逻辑删除还是物理删除属于后续实现决策，但历史与可追溯性不能被无意破坏。

---

## 12. 面向人类的表达规则

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

当建议依赖关键 Hypothesis 时，应明确告诉用户：

- 当前假设是什么；
- 哪些 Evidence 支持 / 反对；
- 哪个结果尚未验证；
- 什么新 Evidence 可能改变当前判断。

例如内部动作可能是：

```text
end_role(project)
add_role(business_line)
```

面向人类应表达为：

> 系统发现“展厅经营”更像一项持续经营的业务，而不是一个有明确结束时间的项目。建议把它调整为“业务线”，以前的历史记录会继续保留。是否调整？

技术 ID 和实现细节只在开发、调试、审计或用户明确要求时展示。

---

## 13. 存储无关性

上述业务规则不得写死在某一种数据库 Adapter 中。

无论底层使用 JSONL、SQLite 或未来其他数据库：

- 自动更新边界一致；
- 自动创建 Object 的 Identity Gate 一致；
- 人工判断边界一致；
- Claim / Hypothesis / Evidence 与历史要求一致；
- 孤立对象保护规则一致；
- 人类表达规则一致。

存储方式可以替换，业务规则不能随存储实现而漂移。
