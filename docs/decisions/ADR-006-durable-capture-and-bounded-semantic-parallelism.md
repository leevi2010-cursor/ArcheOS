# ADR-006：冻结 Capture 与有界语义并行

## 状态

Accepted-candidate — 2026-08-25

## 背景

微信增量消化原先在同一冻结窗口的分段恢复、Semantic authority 绑定与历史验证中重复执行 connector full capture。窗口和历史数量增长后，重复解压、读取、排序与附件定位成为主要耗时；不同完整会话的语义分析又被完全串行执行。真实 Stage 1 验证因此长期停留在技术等待，无法以合理 ROI 形成 Object、Event 与 Timeline 业务证据。

## Decision

### 1. 一个冻结窗口只 full capture 一次

新运行将完整 canonical capture 持久化为私有 Processing Derived Artifact：`snapshot.json`、`index.json`、`summary.json`，最后发布 `receipt.json`。运行计划绑定 capture receipt 与原 canonical capture fingerprint。所有 segment、resume 和 completed-window 验证只读这些 durable artifacts，不再调用 connector；Conversation Source 由 index 直接定位有序消息。

Legacy active v3 运行允许一次 fixed-range capture 升级。升级完成后，authority、maintenance、Gate C 与 Governance recovery 等所有 active-run 安装和恢复入口必须同时读回 durable snapshot 与 index，禁止再次调用 connector。只有能证明为 Provider 前 `ValueError` 的状态才恢复为 processing；已有结果继续 exact replay，unknown attempt 继续拒绝。

### 2. 并行只产生隔离的语义结果

串行 planner 每轮选择最多 2 个完整、不同的 Representation；配置可在受控基准通过后调整为 1–4。每个 lane 使用独立 Provider adapter、诊断目录和 mutable state。在全局 authority lock 内按计划顺序预留连续 ordinal 后，lane 只读取 Source / Representation 并把严格验证结果写入各自 recovery bundle。

同一会话、文档或 Representation 的有序 Analysis Units 不跨 lane 拆分；一个完整 Representation 在一次有界模型调用中得到完整结果，不把上下文拆成多个 ephemeral 调用。Provider、model、timeout、cap、retry 与 fallback contract 不变。

### 3. 长期状态严格串行

结果收敛后，系统仍按 plan / ordinal 顺序执行 package publish、Atomic Information ingestion、Governance、Identity Gate、Proposal / Journal / World Model apply、item terminal state 与 checkpoint。预留顺序按 Representation 整包连续，durable global commit cursor 绑定已提交 ordinal；后续 lane 的成功结果可以先保存，但不得越过更早项提交。

## Recovery 与回滚

- capture artifact 采用 atomic write、fsync、readback、receipt-last；任一 raw/canonical/index/summary/receipt/plan drift 在 Provider 或长期写入前拒绝；
- Provider 前 reservation 以单个 durable attempt marker 原子表达；reserved-not-started 可安全启动，result bundle 已完整时 resume 的 Provider calls 为 0，started 且没有 terminal result 仍视为 unknown；
- production-shaped service 基准以 max3 短段跑完整窗口，分别记录一次 full-capture 路径和多次 durable readback 的观测耗时中位数，并直接断言机器指标；
- 并行度设为 1 可回退语义调度，但不会恢复重复 capture；
- snapshot 是可替换的 Processing artifact，不是第二个 Source 或 Information authority；Managed Source 与 Evidence 规则不变。

## 影响模块

- `archeos/wechat_digest.py`：capture artifacts、legacy upgrade、index slicing、wave planner 与串行提交；
- `archeos/semantic_handoff.py`：有界 result-only wave、独立 Provider 与全局 ordinal；
- `archeos/cli.py`：`--semantic-parallelism`；
- 对应 recovery、性能与隐私测试。

## Consequences

该方案减少 connector 工作并缩短独立会话的语义等待，但不会提高 Governance 并发度，也不会改变 canonical concepts。若 2 路并行仍不能满足 Stage 1 业务验收，应调整验证集或窗口组织，不应继续无界增加并行。
