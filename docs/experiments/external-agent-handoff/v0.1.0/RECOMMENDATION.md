# Recommendation

## 当前决定

保持 External Agent Handoff route 为：

> **not production viable**

修复后的公开 synthetic run 在受控 process tree 的 conservative combined metadata 中记录 9 次 sensitive canary 命中，因此明确 route FAIL。harness 不再声称能够稳定归因到 argv 或 environment；同时，sampling 零命中也只能报告 `unavailable`，不能满足 Issue #66 的零泄漏证明 Gate。

## 下一步 Gate

1. 不启动新的真实 Semantic Handoff Gate，不重新消耗真实样本授权；
2. 不把本实验 harness 迁入 `archeos/`，不实现 #61 production runtime；
3. 由 Architecture + Product Alignment Review 判断：
   - 是否停止当前 Codex CLI External Agent route；或
   - 是否为另一个明确 runtime 建立新的 synthetic-only transport Gate；
4. 候选 route 必须重新满足完整 process-tree metadata 观测、strict result binding、failure audit、permissions、cleanup 与 no-ingestion条件；
5. 只有新 route 的 synthetic Gate PASS 且 Architecture Review 接受观测覆盖后，才可由新的授权 Issue 决定是否运行真实样本。

## Roadmap Feedback

Observation:

External Agent route 可以生成 strict-valid result，但当前 Codex CLI runtime 的受控 process metadata 命中 synthetic sensitive values；sampling observer 又无法凭零命中证明完整覆盖。

Evidence:

修复后一次公开 synthetic Processing Run 为 `metadata_sensitive_hits=9`、`observation_complete=false`、`privacy_boundary_violation`；cleanup、failure audit 与 no-ingestion 均可 Readback。早期 argv/environment 独立分类已废弃，不作为因果 Evidence。

Affected Stage / Assumption:

Product Stage 1 中“semantic execution 稳定、可审计、隐私可控”这一 Development Gap；External Agent Handoff 当前不是可进入 production 的 transport。

Suggested Change:

停止当前 Codex CLI route 的真实样本推进；如 Architect 认为仍需比较其他 runtime，继续使用相同或更严格的 synthetic Gate，不降低 privacy 标准。

Decision:

`review`（等待 Architecture + Product Alignment Review）。
