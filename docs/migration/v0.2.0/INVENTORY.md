# 匿名 Inventory 与迁移分类

## Pilot 边界

| 项目 | 数量 / 状态 |
| --- | --- |
| 明确选择的 Sources | 20 |
| Source families | 3 |
| input shapes | Markdown 13、text PDF 3、PPTX 1、image structural preflight 3 |
| privacy-approved semantic Sources | 17 |
| `local_only` Sources | 3 |
| Representation | complete 17、partial 3、failed 0 |

本表只覆盖 Issue #17 已授权候选池中的固定 20 个 Source，不代表任何旧系统或本地资料库的全量盘点。

## KEEP / IMPORT / REBUILD / RETIRE

| 匿名资料类别 | 分类 | 当前处理 | 后续条件 |
| --- | --- | --- | --- |
| 20 个原始业务 Source | KEEP | 原件只读，Managed Source 保留不可变身份 | 不删除、不移动、不重命名；未来导入仍从 Source / Evidence 开始 |
| 17 个 privacy-approved Source 的 Representation 与 Processing evidence | KEEP + IMPORT | 已进入当前 Source → Representation → Atomic Information 链 | 只在 Evidence / package readback 通过时继续复用 |
| 3 个视觉隐私无法确定的 Source | KEEP | `local_only`，Provider 调用为 0 | 需要本地视觉能力或新的显式 privacy Decision 才能继续 |
| 旧资料中的历史规范、Decision 与说明 | KEEP | 作为来源或设计参考，不成为运行时第二权威 | 通过 Source / Evidence 引用；不得复制成平行 roadmap |
| 旧系统结构化记录 | IMPORT（候选） | 本 Pilot 未全量导入 | 逐 family 建立小型 Import Issue，先完成 canonical mapping 与 identity review |
| 旧系统 schema、状态机与 API noun | RETIRE（目标） | 未执行 cutover | 当前 ArcheOS 能表达真实语义且 readback 通过后，才停止旧权威 |
| 仍有业务价值的旧 UI / Projection | REBUILD（按需） | 本 Pilot 未实现 | 只从当前 World Model / View Model 读取，不保留旧 schema truth |
| 旧 runtime 与 migration glue | RETIRE | 本 Pilot 不引入 | clean-cut 后删除依赖；不建设长期 dual-read / dual-write |

## 明确未做

- 未扫描授权范围之外的目录；
- 未全量盘点旧资料；
- 未移动或删除任何原始资料；
- 未执行 destructive cutover；
- 未把旧 schema 导入 ArcheOS Core；
- 未因额度剩余而扩样。
