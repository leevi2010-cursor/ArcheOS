# Results

## 运行条件

- 日期：2026-08-15；
- 系统：macOS 27.0 / Apple Silicon arm64；
- External Agent：`codex-cli 0.147.0`；
- 输入：hard-pinned 的公开 synthetic package，包含 2 个 Analysis Units 与 5 类 synthetic sensitive value；
- transport：request 经 stdin；harness root argv 只有 CLI 参数和随机 protected temp path；
- observer：macOS PID/PPID topology sampling + selected-PID combined process metadata；
- 单次 deadline：120 秒；无自动 retry / fallback；
- 未读取真实 Source / Representation / #60 样本或日志。

## Failure matrix

23 个 focused tests 全部通过，覆盖：sampling 零命中 unavailable、10 次短命 descendant leak、descendant combined metadata leak、selected-PID 最小权限、lingering descendant cleanup、missing executable / observer / timeout / non-zero failure audit、no/empty/invalid/strict schema result、binding、permissions、atomic publish、Readback 时序、provider metadata injection、audit privacy、hardcoded fixture pin 与 no-ingestion。

## Review 修复后的 External Agent synthetic Gate

修复后只执行了 1 次公开 synthetic Processing Run，且只有一次 External Agent 尝试。

| 指标 | 结果 |
| --- | ---: |
| External structured result strict validation | passed |
| eligible units | 2 |
| observed process count | 6 |
| process-tree snapshots | 1151 |
| combined metadata sensitive hits | 9 |
| metadata read failures | 9 |
| observation complete | false |
| temp permission | verified |
| process group / observed PID / temp cleanup | verified |
| audit Readback | verified |
| result published | false |
| package published | false |
| information ingested | false |
| execution status | failed |
| failure category | `privacy_boundary_violation` |

早期 observer 曾用两次独立 `ps` snapshot 推断 argv/environment channel；该分类会因进程生命周期竞争而变化，相关 `argv=5 / environment=0` 记录已被本次 conservative combined-channel 结果取代，不再作为 Evidence。

## Gate 判断

External Agent 返回了 strict-valid structured result，但受控 process tree 的 combined metadata 命中 synthetic sensitive values；任一命中都必须 route FAIL。即使未来某次 sampling 为零命中，polling 也不能证明覆盖全部短命 exec，因此只能是 `privacy_observation_status=unavailable`，不能 PASS。

因此当前结论是：

> **External Agent Handoff transport not production viable.**

这不是语义质量结论，也不能通过保留 structured result、转写 Residue、自动重试或降低 process metadata Gate 来规避。

## 观测边界

- global discovery 只读取非敏感 PID/PPID topology；argv/environment metadata 仅读取已选 root/descendant PID；
- raw process metadata 未持久化，公共结果只保存匿名 combined 计数；
- sampling observer 不是完整 exec audit，零命中固定 fail closed 为 unavailable；
- 没有验证其他 External Agent runtime、direct model API 或 production integration；
- 没有运行真实资料，因此没有真实 semantic quality Evidence。
