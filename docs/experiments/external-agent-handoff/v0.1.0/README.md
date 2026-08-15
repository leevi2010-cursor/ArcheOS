# External Agent Handoff Privacy / Audit Synthetic Gate v0.1.0

这是 Issue #66 的公开合成实验。结论是：**当前 `codex-cli 0.147.0` External Agent Handoff transport 未通过 privacy Gate，不能进入 production，也不能据此启动新的真实样本 Gate。**

## 边界

- 只接受 byte-for-byte 等价于本目录已提交 fixture 的公开 synthetic package；任何替换正文的输入在启动外部进程前被拒绝；
- 不读取 Managed Source、Normalized Representation、#60 样本、真实日志或任何业务目录；
- 不导入 `archeos/` production runtime，不创建 Atomic Information / Residue package，不写 World Model；
- 不自动 retry / fallback，不把 timeout、runtime failure、invalid result 或 privacy failure 转成 Residue；
- 不新增 Core concept。运行身份使用 canonical `Processing Run`，审计输出属于 `Derived Artifact`。

## 受控交接

```text
committed public synthetic package
→ canonical input fingerprint
→ stdin-only request transport
→ External Agent controlled process tree
→ strict result + fingerprint binding
→ local validation
→ protected result Readback（仅 Gate PASS 时）
→ anonymous Processing Run audit Readback（成功/失败都写）
```

父进程 argv 只包含 CLI 选项和权限为 `0700/0600` 的随机临时路径。Analysis Unit body、synthetic Source / Representation identifier、synthetic business path 与 synthetic credential canary 只进入 stdin；不会由 harness 写入 child argv 或环境变量。

## Process-tree privacy observation

observer 从 External Agent 根 PID 开始，对整个当前后代树采样：

- Linux 使用 `/proc/<pid>/cmdline` 与 `/proc/<pid>/environ`；
- macOS 使用 `ps` 的 argv 与扩展 environment 视图；
- 只在内存中比较 5 类 synthetic sensitive value；
- 持久审计只记录 backend、root 是否可见、snapshot/process 数和命中数，不保存原始 argv、环境、正文、路径或 credential；
- observer 不可用、根进程未观察到、argv/environment 任一命中都 fail closed。

该实现是 10ms 采样 observer，不是内核级 exec audit。它足以在本次真实 Codex CLI synthetic run 中稳定捕获泄漏；即使未来出现 0 命中，Architecture Review 仍需判断观测覆盖是否足以恢复真实样本授权。

## Strict result / Readback

External result 必须同时满足：

- `protocol_version` 与 `input_fingerprint` 精确绑定当前 request；
- strict root/entry fields，不允许 additional properties；
- unit reference 只能来自当前 eligible set；
- unknown、跨 item duplicate、单 item duplicate、incomplete coverage 全部拒绝；
- confidence / semantic type /必填文本遵守最小 contract；
- 只有 privacy、permission、cleanup 与 strict validation 全部通过时才发布 synthetic validated result；
- 发布后从磁盘重新读取、重跑 strict validator 并核对 result fingerprint。

失败不会留下 result 或 Candidate/Residue package，但始终留下不含原文的 `processing-run-audit.json`，并完成一次本地 Readback。

## Synthetic matrix

`tests/test_external_agent_handoff_gate.py` 覆盖：

1. normal valid result；
2. timeout；
3. non-zero runtime failure；
4. no result file；
5. empty result；
6. invalid JSON；
7. unknown unit ref；
8. duplicate unit ref；
9. incomplete coverage；
10. stale / wrong input fingerprint；
11. child argv canary scan；
12. environment canary scan；
13. temporary / durable permission；
14. cleanup after success / failure；
15. success / failure audit Readback；
16. audit artifact privacy scan；
17. no package / Atomic Information / World Model write；
18. non-committed input rejection。

## 重跑

Focused matrix：

```bash
python3 -m unittest tests.test_external_agent_handoff_gate -v
```

真实 Codex CLI synthetic Gate（输出目录必须位于 Git 仓库之外）：

```bash
python3 docs/experiments/external-agent-handoff/v0.1.0/run_synthetic_gate.py \
  --output-root /tmp/archeos-issue66-audit \
  --codex-bin codex \
  --timeout 120
```

Gate FAIL 返回非零退出码；打印内容只包含匿名 audit，不打印 raw argv/environment 或模型输出。

## 文件

- `fixtures/synthetic-handoff-package.json`：唯一获准输入的公开合成 package；
- `fixtures/fake_external_agent.py`：failure matrix 的受控子进程；
- `schemas/external-agent-result.schema.json`：External Agent strict result schema；
- `schemas/processing-run-audit.schema.json`：匿名 Processing Run audit schema；
- `run_synthetic_gate.py`：transport、process observer、validator、cleanup 与 Readback harness；
- `RESULTS.md`：实际 Gate 证据；
- `RECOMMENDATION.md`：route 判断与下一 Gate。
