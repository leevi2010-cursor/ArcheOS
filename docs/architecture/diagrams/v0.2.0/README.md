# 向阳经营系统架构图 v0.2.0

- 日期：2026-08-11
- 状态：M2 Target Architecture
- 产品名称：向阳经营系统（Sunward Operating System）
- 工程代号：ArcheOS

## 本版本表达什么

本版本表达**按当前规划完成 M2-B1 / B2 / B3 后的目标架构**，并同时标注当前已实现和规划中的能力。

状态颜色：

- 绿色实线：已实现 / 已合并，可作为当前基础。
- 蓝色实线：正在实现。
- 蓝色虚线：已规划但尚未实现。
- 紫色：长期权威数据 / 持久化边界。
- 灰色：外部 Agent 或外部消费者。
- 橙色：Tolaria / 旧向阳经营系统等迁移来源。

## 两张图

### 系统架构图

`system-architecture-v0.2.0.svg`

回答：系统有哪些主要层次、每层负责什么、谁可以读写长期数据、Context Builder 在哪里。

### 数据流图

`data-flow-v0.2.0.svg`

回答：一段原始信息如何经过 Processing，成为 Atomic Information，再受治理地影响 World Model，最后被 Context Builder 提供给 Agent / 人类，并进入未来 Decision → Feedback 闭环。

## 关键架构结论

1. **Atomic Information** 是长期最小信息单元；`Note` 仅作为历史旧称识别，不建立平行模型。
2. **Object** 是长期稳定身份；Name、Role、Lifecycle 与 Relationship 围绕 Object 演化。
3. **Atomic Information 与 World Model 分层**：信息可以自动进入长期 Information；改变世界模型时遵守治理规则。
4. **Context Builder 是统一读取能力**；Object-scoped context 只是第一种 scope，不新增 Object Context 平行概念。
5. **存储可替换**：JSONL / SQLite 是 Adapter，不决定领域语义。
6. **人类交互使用业务语言**；内部 ID、schema、repository 等技术细节不直接暴露给普通业务用户。
7. **迁移后产品仍叫“向阳经营系统”**；ArcheOS 是当前重构工程代号。

## 与实现阶段的对应

- M1 Processing：已实现。
- M2-A World Model Foundation：已实现。
- M2-B1 Atomic Information Store：正在实现（Issue #9 / PR #12）。
- M2-B2 Information Digestion & Governance：规划（Issue #10）。
- M2-B3 Context Builder Object-scoped v1：规划（Issue #11）。
- M3 Domain Agents：后续。
- M4 Decision / Action / Feedback：后续。
- Tolaria / 旧向阳迁移：B3 完成后进入 Migration Readiness / shadow migration 阶段。
