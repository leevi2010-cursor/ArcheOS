# Provider Matrix

| 路线 | 实际版本 / runtime | 执行边界 | 合成结果 | 隐私与认证 | 生产判断 |
| --- | --- | --- | --- | --- | --- |
| A. pinned Codex SDK | `openai-codex==0.144.4`，其 bundled runtime | 官方 Python SDK → app-server；`deny_all`、`read_only`、ephemeral | 5/5 structured output 与跨调用完整 coverage 通过；不确定分类 3/13 不符合 fixture oracle | 本机既有登录态；ArcheOS 不读 token；仅合成输入 | 不能作为默认：未解释真实 document 未完成 |
| B. latest SDK（隔离） | Python index 在运行时只提供 `0.144.4`，与 A 相同 | 独立虚拟环境，其他条件与 A 相同 | 5/5 structured output 与跨调用完整 coverage 通过；不确定分类 3/13 不符合 fixture oracle | 同 A | 没有可比较的 SDK 升级；不作升级建议 |
| C. external Agent handoff | `codex-cli 0.147.0` | local analysis package → `codex exec` → strict schema validation；read-only、ephemeral | 5/5 structured output 与跨调用完整 coverage 通过；不确定分类 2/13 不符合 fixture oracle | 本机既有登录态；外部 Agent 只接收 package | 结构上最符合产品边界；仍只有合成样本，不足以设默认 |
| D. direct model API | 未执行 | 未来仅可置于 Provider Adapter 后 | 无基准 | 需要用户已有合法凭据与单独授权 | 不进入本轮候选 |
| 本地开源模型 | 未执行 | 未来仅可置于 Provider Adapter 后 | 无基准 | 需另做许可、离线、资源与质量门禁 | 不进入本轮候选 |

## 可归因与不可归因

两套 SDK 环境安装到的版本相同，因此两次输出差异不能归因于 SDK 升级；它更可能反映模型输出的随机性或运行时状态。本实验也不包含真实文档，不能在 SDK/runtime、模型执行、schema 或 document input size 之间判定此前真实失败的主因。

初始 preflight 发现 app-server structured-output schema 拒绝 `uniqueItems`。实验随后移除了该 keyword，并把 unit ID 唯一性保留在 harness 的严格验证中。此发现说明 SDK/output-schema 方言是兼容性约束；它不是 Provider 质量结论，也不使用真实输入。
