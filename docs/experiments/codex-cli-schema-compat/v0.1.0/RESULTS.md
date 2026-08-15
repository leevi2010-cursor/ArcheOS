# Issue #80 结果

## 结论

**PASS：Codex CLI strict structured-output execution baseline 已恢复。**

当前 `codex-cli 0.147.0` 接受 `type + const` 兼容形态。#76 的既有严格语义合同没有放宽：
`protocol_version` 和 `input_fingerprint` 仍受绑定，root 与 Candidate / Residue 仍保持
`additionalProperties: false`，而 19 个 anchor 的 coverage 与 context Evidence 都经 #31 validator 验证。

这只证明公开 synthetic 上的执行基线，**不代表真实微信语义质量通过，也不放行 #61。**

## 正式 public synthetic runs

| Run | 正式调用 | 结果 | 可验证结果 |
| --- | ---: | --- | --- |
| 1 `type + const` minimal smoke | 1 | PASS，25.387s | provider completed、result file、strict JSON 均通过。 |
| 2 corrected #76 small contract | 1 | PASS，42.730s | protocol / input fingerprint binding、strict Candidate / Residue 与 #31 validator 均通过；2/2 anchor accounting。 |
| 3 19 anchors + context support | 1 | PASS，44.609s | provider completed、strict schema / bindings、19/19 anchor accounting、unaccounted=0、#31 context Evidence validator 均通过。 |

正式 `codex exec` 调用合计：**3 / 3**。无 retry、fallback 或 Provider / route 变更。

## 运行边界与诊断

- 运行前两个 tracked schema 都通过递归 `const → explicit matching type` preflight；任何不匹配会在启动
  Codex 前失败；
- Run 2/3 中存在 bounded context support；#31 validator 对结果的 Evidence references / anchor coverage 执行
  严格验证。正向 context-reference 与 invalid / unknown context-reference fail-closed 分支同时有本地 synthetic
  regression coverage；本次模型输出本身未主动把 context support 引为 Evidence；
- CLI version preflight 成功。每次调用均有 exit code 0、result file 存在且 JSON 有效；
- 本机 local-only diagnostic 保留了经过 credential-like 值脱敏、长度限制的 stdout/stderr 供审阅；本文件
  不记录 session、环境、临时目录或原始输出；
- 全程只使用公开 synthetic 文本；未读取任何真实微信、Managed Source、Representation、#76 marker 或其他
  业务资料；未写 package、Atomic Information、World Model 或 production runtime。

## 已知前提

#78 已把 #76 的请求阶段失败定位为：`protocol_version` 的 JSON Schema 使用 `const` 但未显式声明
`type`。本实验只验证修正后的 `{"type":"string","const":"semantic-quality-wechat/1.0"}` 序列化形态；
不降低任何语义、Evidence 或 coverage 要求。
