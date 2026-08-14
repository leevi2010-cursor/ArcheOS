# Recommendation

## v1 结论

**no production provider yet**。

当前不存在足以作为 ArcheOS 默认 semantic provider 的路线。三条路线已证明：多个独立 package → strict structured result → 跨调用 Candidate/Residue coverage 的执行边界可行；但它们没有证明真实 document / conversation 的稳定语义质量，也没有解释此前真实 text-PDF 的两次未完成。

## 对必须回答问题的答复

1. **当前失败主因**：不可确定。合成长输入成功排除了“所有长输入必然失败”，但不能区分 SDK/runtime、模型执行、schema 或 document input shape。
2. **pinned SDK 是否适合作为正式 provider**：尚不适合。合成门禁通过不等于真实可用。
3. **latest SDK 是否显著改善**：没有可比较升级；运行时可安装 latest 仍为 `0.144.4`。
4. **external Agent handoff 是否更符合产品边界**：是，结构上更一致；但 `n=5` synthetic 仍不能证明足够可靠。
5. **是否需要 direct model API**：当前不需要。若以后比较，只承担 Semantic Provider Adapter 执行层，不接管 Source、Representation、Evidence、Candidate/Residue 或 World Model。
6. **v1 正式路线**：尚未选择。
7. **fallback / timeout / retry**：单次尝试、120 秒 deadline、立即 interrupt、fail closed；不自动 retry/fallback，不把 runtime failure 转 Residue。
8. **仅实验路线**：本目录所有路线，包括 direct API/local model 调研，均仅实验。
9. **#48 微信 semantic digestion gate**：#48 可先完成真实 Source → Conversation Representation → stable Analysis Units；在 Architect 明确选择并授权真实 semantic route 前，禁止运行真实微信 semantic digestion 或吸收 Atomic Information。
10. **后续 production Issue 最小 contract**：versioned input package、明确 provider route/version、strict result schema、unit coverage/no-overlap validator、Evidence unit locator 保持、120 秒 interrupt/fail-closed、Processing Run failure record、无自动 Object/Relationship/World Model 写入、synthetic/authorized-real 分级验证。

## 下一步建议

1. 不恢复 #31 的 Codex semantic production default；先合并其通用 contract 收敛工作。
2. 推进 #48 的 Representation 与 stable units，不使其依赖本实验的任何 Provider。
3. Architect 只在需要重新评估生产路线时，选择一条候选并对 1–3 个已选真实样本授予一次性验证授权；每个样本只运行一次，结果仅保留匿名聚合与问题类型。
4. 若真实验证仍失败，保持 `no production provider yet`，而不是继续对 ArcheOS contract 做 SDK 特定修补。
