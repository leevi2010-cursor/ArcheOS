# Canonical Mapping

本映射只用于识别既有 legacy material。新实现直接使用 ArcheOS canonical concepts，不维持平行模型。

| 旧资料 / legacy 表达 | Canonical mapping | 迁移规则 |
| --- | --- | --- |
| 原文件、录音、导出包 | Source | 先登记不可变 Source identity，再产生派生表示 |
| 解析文本、转写、预览、摘要中间件 | Normalized Representation / Derived Artifact | 不覆盖 Source；必须保留 Processing Run 与 Evidence locator |
| Note / Atomic Note / semantic record | Atomic Information | 逐条保留 statement、Evidence、context、uncertainty 与 Revision；不建 Note Store |
| assertion / speaker statement | Claim on Atomic Information | 保留 claimant / stance / attribution；Claim 不直接等于 World Model truth |
| legacy entity | Object | 只有需要长期稳定身份时才创建；普通 topic / action 使用 `no_object` |
| person / company / project / business-line base table | Object + approved Role | 不恢复互斥 base table；Role 使用当前 vocabulary 并独立治理，满足安全自动更新条件时可自动执行 |
| display label / historical name | Name assignment | 稳定身份使用 `object_id`；Name 变化保留历史 |
| legacy status / phase | Lifecycle（仅明确语义） | 不把任意状态字段直接升级为 Lifecycle；目标唯一、Evidence 足够且无冲突 / 歧义时可安全自动更新，否则 human review |
| legacy relation / foreign key | Relationship（仅有 Evidence 时） | 不按旧外键自动造关系；方向、语义、端点与 Evidence 明确且满足安全条件时可自动执行，否则 human review |
| task / action item | Issue 或 Todo | 需要独立 owner / 状态 / Evidence 时为 Issue，否则为 Todo |
| report / dashboard / tree | View / Projection / Presentation | 重新从当前权威读取；不得成为第二套 Core truth |
| legacy stable ID | reviewed external identity mapping | 只有可验证、无冲突时建立；不得发生 silent remap |
| 无法安全吸收的内容 | Residue | 保留原因与 Evidence，不静默丢弃，不把 runtime failure 伪装成 Residue |

## Identity 原则

- 同一真实 identity 的 Information 先分组，再顺序 create / bind；
- `create_minimal` 只建立 Object + evidence-backed Name；
- Role、Relationship、Lifecycle 不随 minimal create 自动产生；
- Role、Relationship、Lifecycle 分别按 `INFORMATION_GOVERNANCE.md` 治理，满足安全自动更新条件时可以自动执行；
- ambiguity、conflict、high-impact structure、identity correction、merge、delete、split 保持 Human Judgment；
- exact replay 不制造新 Object、Revision、Receipt 或 Journal。
