# Recommendation

## 当前决定

保持 External Agent Handoff route 为：

> **not production viable**

理由不是 structured output 失败，而是 `codex-cli 0.147.0` 的受控后代进程 argv 在两次公开 synthetic run 中都命中全部 5 类 sensitive canary。Issue #66 明确要求 argv/process metadata 0 命中；该 Gate 不能降级。

## 下一步 Gate

1. 不启动新的真实 Semantic Handoff Gate，不重新消耗真实样本授权；
2. 不把本实验 harness 迁入 `archeos/`，不实现 #61 production runtime；
3. 由 Architecture + Product Alignment Review 判断：
   - 是否停止当前 Codex CLI External Agent route；或
   - 是否为另一个明确 runtime 建立新的 synthetic-only transport Gate；
4. 任何候选 route 都必须重新满足相同的 argv/environment 0 命中、strict result binding、failure audit、permissions、cleanup 与 no-ingestion 条件；
5. 只有新 route 的 synthetic Gate PASS 且 Architecture Review 接受观测覆盖后，才可由新的授权 Issue 决定是否运行真实样本。

## Roadmap Feedback

Observation:

External Agent route 可以生成 strict-valid result，但当前 Codex CLI runtime 会把 stdin payload 展开到后代进程 argv。

Evidence:

两次独立 synthetic Processing Run 均为 `argv_sensitive_hits=5`、`environment_sensitive_hits=0`、`privacy_boundary_violation`；成功/失败审计、cleanup 与 no-ingestion 均可 Readback。

Affected Stage / Assumption:

Product Stage 1 中“semantic execution 稳定、可审计、隐私可控”这一 Development Gap；External Agent Handoff 当前不是可进入 production 的 transport。

Suggested Change:

停止当前 Codex CLI route 的真实样本推进；如 Architect 认为仍需比较其他 runtime，继续使用同一 synthetic Gate，不降低 privacy 标准。

Decision:

`review`（等待 Architecture + Product Alignment Review）。
