# ArcheOS v0.2.0 真实旧资料 Pilot

本目录记录 Issue #17 的匿名迁移 readiness 与 Stage 1 真实压力验证结果。

Pilot 只验证当前 ArcheOS 链路能否在边界明确的真实旧资料上保持 Source、Evidence、Atomic Information、Object identity 与 Context；它不是全量迁移，也不建立长期 dual-read / dual-write。

## 结果摘要

- 20 个 Source 完成只读准入与 Representation；
- 17 个 privacy-approved Source 完成 25 个真实 Semantic batches，全部 strict PASS；
- 3 个无法确定视觉内容隐私边界的 Source 保持 `local_only`，Provider 调用为 0；
- 形成 275 条 Durable Atomic Information 与 24 条 Residue，eligible Units 的 `unaccounted = 0`；
- bounded Identity Gate 与 Product Owner Context Review 完成；
- 没有未解决 P0 / P1；
- Stage 1 Evidence 结论为 `PARTIAL / review`，主要缺口是本 corpus 未安全确认 equivalent / derived / temporal / conflict 关系，且最终 Context 只覆盖一个真实 Object。

## 文件

- [INVENTORY.md](INVENTORY.md)：匿名 inventory 与 KEEP / IMPORT / REBUILD / RETIRE 分类；
- [MAPPING.md](MAPPING.md)：旧资料到 canonical concepts 的最小映射；
- [PILOT_RESULTS.md](PILOT_RESULTS.md)：执行证据、指标、偏差恢复与 Roadmap Feedback；
- [CUTOVER_PLAN.md](CUTOVER_PLAN.md)：后续 clean-cut 建议；
- [manifest.json](manifest.json)：机器可读的匿名结果清单。

## 隐私边界

本目录不包含真实正文、文件名、路径、hash、人员、公司、项目、客户信息、Object ID、Source ID、Evidence locator 或 Provider diagnostic。所有真实数据与中间产物只保留在本地 Git-ignored Pilot workspace。
