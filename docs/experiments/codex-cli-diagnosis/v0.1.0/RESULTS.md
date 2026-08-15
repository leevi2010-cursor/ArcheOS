# Issue #78 结果

## 结论

**FAIL：尚未恢复 #76 所需的 strict structured-output baseline。** 根因已收敛为
`structured_output_schema_failure`，而不是 auth、Codex CLI 基础执行或 #76 的双目录布局。

当前 `codex-cli 0.147.0` 的 response-format endpoint 拒绝 schema 中只有 `const` 而没有显式
`type` 的 property。#76 的 `protocol_version` 使用该形态，因此在 Provider 开始生成结果前即被
`invalid_json_schema` 拒绝；#76 最终只留下 `provider_nonzero_exit`，但实际可行动错误是 schema
兼容性问题。

## 已执行的 public synthetic matrix

| Case | 是否调用 | 结果 | 关键诊断 |
| --- | ---: | --- | --- |
| A runtime/auth smoke | 是 | PASS，15.028s | `codex exec`、已登录 route 与基础模型执行正常。 |
| B minimal strict schema | 是 | FAIL，20.141s | `invalid_json_schema`：`answer` property 的 `const` 缺少 `type`。 |
| C #66 same-directory | 是 | PASS，28.678s | result file 存在、JSON 有效、strict binding/coverage 均通过。 |
| D #76 split-directory contrast | 是 | PASS，33.568s | 与 C 同样 strict-valid；双目录布局未复现失败。 |
| E corrected #76 small contract | 是 | FAIL，21.877s | `invalid_json_schema`：`protocol_version` property 的 `const` 缺少 `type`。 |
| F corrected #76 19-anchor | 否 | 未调用 | E 已在请求 schema 提交阶段失败；扩大 anchor/context 不会提供新证据。 |

正式 `codex exec` 调用：**5 / 6**。没有 retry、fallback 或第 6 次补跑。

## Observability

- CLI 启动、version 与 auth 基础执行：A 成功；
- result file / JSON / strict validator：C、D 均成功；B、E 均未产生 result file，原因是 Provider
  在 schema 校验阶段拒绝请求；
- stderr 保留了经 credential-like 值脱敏、长度受限的本机完整诊断。日志还出现一个 Cloudflare MCP
  `AuthRequired` sidecar warning，但 C、D 同时成功，所以它不是本次 non-zero 的决定性原因；
- 不记录 token、环境变量、私人路径或会话标识。

没有读取真实微信、真实 Source、Representation、#76 request/raw output/marker 或其他业务数据；没有
写入 Atomic Information、World Model 或 production runtime。
