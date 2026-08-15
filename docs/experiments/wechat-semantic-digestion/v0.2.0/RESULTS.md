# 微信 Semantic Digestion Real Gate v2（匿名结果）

## 结论

**PASS。** 已授权的真实微信 Conversation Representation 完成一次受限的
Codex CLI 语义交接。结果经过严格合同、信息包、Durable Atomic Information
和精确重放验证；没有写入 World Model。

本报告只保留匿名聚合指标。真实聊天正文、参与者身份、路径、哈希、prompt、
模型原始输出、stdout/stderr 和凭证均未写入 Git。

## 调用边界

| 项目 | 结果 |
| --- | --- |
| 已验证 Conversation Representation | 1 份；50 条消息可重放 |
| Analysis Units | 19 个 eligible anchors、36 个 context-support references、31 个 excluded units |
| 正式 Provider 调用 | 1 次 |
| route | `codex-cli` External Agent route |
| deadline | 300 秒（生产默认值仍为 120 秒） |
| elapsed | 58,015 ms |
| retry / fallback / provider switch / 拆批 | 均未发生 |

## 严格合同与信息结果

| 检查 | 结果 |
| --- | --- |
| provider completed / strict structured output | 通过 |
| protocol 与 input fingerprint binding | 通过 |
| anchor accounting | 19 / 19 covered；0 unaccounted |
| context → Evidence contract | 通过；仅 canonical Analysis Unit / locator 被接受为 Evidence |
| package strict verify / Readback | 通过 |
| Atomic Information durable ingestion | created=12、existing=0、failed=0 |
| exact replay | 不调用 Provider；created=0、existing=12 |
| Processing Run audit Readback | 通过 |
| Object / Relationship / Lifecycle / World Model writes | 0 / 0 / 0 / 0 |

## 诊断与清理

本次调用正常完成，未产生 failure raw diagnostic bundle。调用结束后仍执行
显式诊断清理，结果通过。受控临时产物未保留。

## 验证

- 微信 Source 与最终 Conversation Representation 重新 verify 通过。
- `python -m unittest discover -s tests`：443 项通过。
- 与语义交接相关的 focused tests：111 项通过。
- `compileall`：通过。
- 已执行 Ruff 检查；当前主线存在 99 项既有静态规则问题，涉及未改动文件，
  本 Gate 不在 Issue #62 范围内修改它们。

## 非目标

本 Gate 不评估或宣称模型语义准确率，也不进行 Provider benchmark。Durable
Atomic Information 仍处于 Information Layer，不能被当作 World Model truth。
