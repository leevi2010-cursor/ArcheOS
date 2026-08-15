# Issue #78 Recommendation

## 诊断 Recommendation

本实验不选择或切换 Provider。

向 Product / Technical Lead 上报：#76 的 `provider_nonzero_exit` 已在公开 synthetic 输入上定位为
当前 Codex response-format 对 JSON Schema 的兼容性失败。下一张真实 Semantic Gate 之前，需要由 Lead
决定是否批准一个仅修正 #76 experiment schema 兼容形态（例如为每个 `const` property 补上明确 `type`）的
后续 Issue/合同；不能由本实验或 Reviewer 静默修改已合并 #76 harness。

证据同时表明：Codex CLI auth/基础执行正常，#66 同目录形态正常，#76 的 split-directory 形态也正常。#61
仍保持 Blocked，直至新的受审查 synthetic baseline 成功，并取得新的真实样本授权。
