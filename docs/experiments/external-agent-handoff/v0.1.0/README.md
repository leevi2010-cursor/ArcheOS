# External Agent Handoff Privacy / Audit Synthetic Gate v0.1.0

这是 Issue #66 的公开合成实验。结论保持：**当前 `codex-cli 0.147.0` External Agent Handoff transport 未通过 privacy Gate，不能进入 production，也不能据此启动新的真实样本 Gate。**

## 边界

- 只接受与代码内 hardcoded committed fingerprint 一致的公开 synthetic package；不依赖可变 fixture 或 manifest 自证；
- 不读取 Managed Source、Normalized Representation、#60 样本、真实日志或任何业务目录；
- 不导入 `archeos/` production runtime，不创建 Atomic Information / Residue package，不写 World Model；
- 不自动 retry / provider fallback，不把 timeout、runtime failure、invalid result 或 privacy failure 转成 Residue；
- 不新增 Core concept。运行身份使用 canonical `Processing Run`，审计输出属于 `Derived Artifact`。

## 受控交接

```text
hard-pinned public synthetic package
→ canonical input fingerprint
→ stdin-only request transport
→ External Agent controlled process group/tree
→ strict result + fingerprint binding
→ local validation
→ staged result Readback（仅全部 Gate 满足时）
→ pending audit write + Readback
→ verified audit write + Readback
→ result/audit directory atomic publish
```

父进程 argv 只包含 CLI 选项和权限为 `0700/0600` 的随机临时路径。Analysis Unit body、synthetic Source / Representation identifier、synthetic business path 与 synthetic credential canary 只进入 stdin；不会由 harness 主动写入 child argv 或环境变量。

## Process-tree privacy observation

默认 observer 是明确 fail-closed 的 sampling detector，而不是“零命中证明器”：

- 全局只读取 PID/PPID topology；
- Linux 只对已选 root/descendant PID 读取 `/proc/<pid>/cmdline + environ`；
- macOS 只对已选 PID 调用 `ps eww -p <pid>`；
- argv 与 environment 统一作为 conservative `combined process metadata`，不做两次 snapshot 的不稳定 channel 归因；
- 只在内存中比较全部 unit body、unit ID、synthetic Source/Representation ID、business path 与 credential canary；
- 持久审计只记录 backend、root、snapshot/process 数、combined 命中数、metadata read failure 与 completeness；不保存 raw metadata；
- 发现任一 combined metadata 命中即 `failed`；0 命中仍因 polling 无法覆盖所有短命 exec 而固定为 `unavailable`，绝不 PASS。

测试中的 `synthetic_complete_test_double` 只验证成功 artifact/Readback 分支，不用于 live CLI，也不构成 route privacy Evidence。

## Process cleanup

所有正常、timeout、non-zero、observer failure 与异常路径都会：

1. 终止独立 process group；
2. 对 observer 已见 descendant 做补充终止；
3. 必要时从 `SIGTERM` 升级到 `SIGKILL`；
4. 核验 process group 与已见 PID 均不存在；
5. 删除 protected temporary directory；
6. 在 audit 的 `cleanup_observation` 中记录匿名结果。

30 秒 background descendant regression 会在根进程退出后继续存活；harness 必须终止它，才能记录 `cleanup_status=verified`。

## Strict result / audit durability

External result 必须绑定同一 `protocol_version + input_fingerprint`，通过 strict root/entry fields、type、unknown/duplicate ref 与 full coverage validator。

result 与 audit 在隐藏 staging directory 中组成一个 bundle。result 先完成 Readback；audit 先写 `pending` 并实际 Readback，再写 `verified` 并再次 Readback，最后才原子发布整个目录。若 audit/result write 或 Readback 失败，staging 被删除并只尝试发布匿名 failure audit，不留下孤立 result。

provider route/version 只接受明确安全格式；非法正文、路径或 credential-like metadata 会在启动 External Agent 前归一为 hash label，并产生 `unsafe_provider_metadata` failure audit。audit privacy scan 覆盖全部输入正文、标识、路径与 credential canary。

## Reviewer regression matrix

`tests/test_external_agent_handoff_gate.py` 当前 23 tests，覆盖：

- normal strict result 与 atomic Readback success test-double；
- sampling 0-hit unavailable；
- 10 次短命 descendant argv leak 不 false PASS；
- descendant argv/env combined metadata detection；
- topology-only global discovery + selected-PID metadata；
- 30 秒 lingering child termination/readback；
- timeout、non-zero、missing executable/version probe、observer failure；
- no/empty result、invalid JSON、valid JSON extra field/type error；
- unknown/duplicate/incomplete/wrong fingerprint；
- permissions、audit write failure 无孤立 result、pending→verified Readback 时序；
- provider metadata injection、全部输入 audit privacy；
- hardcoded fixture fingerprint、non-committed package rejection；
- no package / Atomic Information / World Model write 与 strict committed schemas。

## 重跑

```bash
python3 -m unittest tests.test_external_agent_handoff_gate -v
```

真实 Codex CLI synthetic Gate（输出目录位于 Git 仓库之外）：

```bash
python3 docs/experiments/external-agent-handoff/v0.1.0/run_synthetic_gate.py \
  --output-root /tmp/archeos-issue66-audit \
  --codex-bin codex \
  --timeout 120
```

sampling route 无论检测到泄漏还是无法证明完整覆盖都返回非零；stdout 只包含匿名 audit。
