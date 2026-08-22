# 远程分支只读审计（2026-08-17）

## 范围与方法

本报告对应 Issue #74，仅审计 `origin/main` 之外的远程分支，不执行分支删除、worktree 删除、工作区切换、重置或清理。

审计依据：

- GitHub Pull Request 的 open / merged / closed 状态；
- `origin/main...origin/<branch>` 的 branch-side commits；
- `git cherry origin/main origin/<branch>` 的补丁等价结果；
- merged branch tip 与对应 PR merge commit 的完整 tree 比较；
- 本机 `git worktree list --porcelain` 是否仍引用该分支。

`unique_commit_count` 表示分支侧、且没有以相同 commit identity 进入当前 `main` 的 commit 数。对于 squash / rebase 合并，只有对应 merge commit 与 branch tip 的完整 tree 相同，才把这些 commit 认定为已等价进入 `main`。

## 结论摘要

| classification | 分支数 | 处理建议 |
| --- | ---: | --- |
| `safe_to_delete` | 8 | 可由后续明确授权的 maintenance 删除远程分支 |
| `keep` | 12 | 当前存在 open PR 或本地 worktree 引用，保持不动 |
| `needs_human_check` | 3 | 存在未合并提交、关闭但未合并的 PR，或缺少 PR provenance |

本报告不授权任何删除动作。

## 分支明细

| branch | related_pr | related_issue | unique_commit_count | classification | reason |
| --- | --- | --- | ---: | --- | --- |
| `codex/issue-100-anchor-coverage` | #103 merged | #100 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-106-wechat-digest` | #109 open Draft | #106 | 4 | `keep` | open PR，属于当前活动交付 |
| `codex/issue-107-semantic-profile` | #110 open Draft | #107 | 7 | `keep` | open PR，属于当前活动交付 |
| `codex/issue-17-legacy-data-pilot` | #105 merged | #17 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-32-information-consolidation` | #99 merged | #32 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-33-information-consolidation` | #101 merged | #33 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-34-identity-gate` | #102 merged | #34 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-55-product-roadmap-governance` | #56 merged | #55 | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |
| `codex/issue-64-development-roadmap` | #68 closed, unmerged | #64 | 1 | `needs_human_check` | 关闭但未合并的 PR 仍有独立提交，且本地 worktree 仍引用该分支 |
| `codex/issue-64-roadmap-sync` | #92 closed, unmerged | #64 | 1 | `needs_human_check` | 关闭但未合并的 PR 仍有独立提交，且本地 worktree 仍引用该分支 |
| `codex/issue-64-roadmap-sync-v2` | #93 merged | #64 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-75-role-governance` | #91 merged | #75 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-95-workspace-state` | #96 merged | #95 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/issue-97-contract-diagnostics` | #98 merged | #97 | 0 | `keep` | 内容已合并，但仍被本地 worktree 引用 |
| `codex/m2-c1-source-archive-experiment` | #20 merged | none | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |
| `docs/concept-convergence-governance` | #52 merged | #42 | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |
| `docs/converge-m4-roadmap` | #53 merged | none | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |
| `docs/issue-58-document-convergence` | #59 merged | #58 | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |
| `docs/issue-69-object-identity-gate` | #71 merged | #69 | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |
| `docs/long-term-vision` | none | none | 1 | `needs_human_check` | 无 PR provenance，包含一个未等价进入 `main` 的独立文档提交 |
| `docs/roadmap-convergence-20260817` | #104 open Draft | #17 / #108 | 4 | `keep` | open PR，属于当前活动交付 |
| `docs/roadmap-human-view-retirement` | #49 merged | #31 / #48 / #32 / #33 / #34 / #17 | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |
| `docs/thinking-protocol-governance-roadmap` | #51 merged | none | 0 | `safe_to_delete` | branch tip 与 PR merge commit 的完整 tree 相同，且无本地 worktree 引用 |

## 人工判断项

### `codex/issue-64-development-roadmap`

PR #68 已关闭但未合并。该分支仍有一个 branch-side 独立提交，并被本地 worktree 引用。即使其目标可能已由后续 PR #93 取代，也不在本次只读审计中推断 worktree 是否可废弃。

### `codex/issue-64-roadmap-sync`

PR #92 已关闭但未合并。该分支仍有一个 branch-side 独立提交，并被本地 worktree 引用。是否由 PR #93 完整取代，需要拥有该 worktree 的负责人确认。

### `docs/long-term-vision`

该分支没有关联 PR，包含一个未等价进入 `main` 的独立文档提交。删除前必须由 Architect 判断该长期愿景文档应保留、整合还是废弃。

## 安全边界读回

- 远程分支删除：0；
- 本地分支删除：0；
- worktree 删除：0；
- 用户主工作区修改：0；
- 未跟踪文件读取或修改：0；
- 真实业务数据写入 GitHub：0。
