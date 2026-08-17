# Clean-cut 建议

本文件是后续迁移建议，不表示已经执行 cutover。

## 原则

1. Source / Evidence 先行，结构化数据后置；
2. 按 source family 建立少量、有验收条件的 Import Issues；
3. 不长期 dual-read / dual-write；
4. 旧 schema、ID、状态与关系不能直接成为 ArcheOS Core truth；
5. identity、Role、Relationship、Lifecycle 的 consequential change 保持 Human Judgment；
6. cutover 前后都保留 Raw Source、Evidence、Processing Run 与可逆 readback。

## 建议阶段

### 0. 冻结迁移范围

- 只选择明确 owner、privacy route、Source family 与验收人的资料；
- 保存只读 inventory 与 Source hash/readback；
- 未授权目录和高隐私候选保持 out of scope。

### 1. 小批 Import

- 每个 Import Issue 只处理一个 source family / contract；
- 先 Managed Source 与 Representation，再 Semantic Handoff；
- unsupported / partial 明确进入 report，不用临时 parser 绕过。

### 2. Information 验收

- `unaccounted = 0`；
- Evidence locator 可回放；
- replay / re-ingestion 幂等；
- runtime failure 不进入 Residue；
- relation truth 不充分时保留 `uncertain`，不强行 consolidation。

### 3. Identity 与结构化变更

- 从 read-only assessment / apply plan 开始；
- cap 从 durable receipts / journal / bindings / Objects 计算；
- automatic apply 只在批准 envelope 内；
- Role、Relationship、Lifecycle 与 identity correction 由 Human Judgment 决定。

### 4. Context 对照

- Product Owner 对比新 Context 与旧资料；
- 记录遗漏、错误结构化、pending judgments 与 Evidence 可展开性；
- P0 / P1 未清零前不得 cutover。

### 5. Authority cutover

- 一次性声明当前 ArcheOS Store 为结构化读取权威；
- 旧系统降为只读 archive / migration source；
- 停止旧 structured writes，再停止兼容读取；
- UI / Projection 只从当前 World Model / View Model 重建。

## 回退边界

回退不是恢复旧 dual-write。若验证失败：

- 停止对应 Import Issue；
- 保留已登记 Source、Processing Run、Evidence、Residue 与 audit；
- 隔离未通过的派生产物；
- 不删除原始资料；
- 修正 contract 或由 Lead 决定是否继续。

## 建议后续工作

- 一个带人工 truth 的 Consolidation 验证 Issue，定向覆盖 equivalent / derived / temporal / conflict；
- 一个至少覆盖多个真实 Object 的 Context 对照 Issue；
- 只有在上述证据通过后，才为高价值 legacy family 创建小型 Import Issues。
