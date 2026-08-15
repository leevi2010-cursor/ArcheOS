# External Agent Failure Diagnostics v0.2.0

本说明记录 Issue #85 的失败诊断边界。它只服务 `Processing Run` 的技术排障，不改变 External Agent 的语义合同、Evidence、信息包或长期 Information 生命周期。

## Durable audit

长期审计只记录版本、route、时间、耗时与 deadline、退出/终止状态、有限失败类别、结果文件存在性与大小、stdout/stderr 字节数、清理状态，以及既有 protocol、fingerprint 和覆盖计数。

审计绝不记录 stdout/stderr 正文、prompt、request、raw result、路径、环境、完整命令、credential、业务正文或 Provider 会话/账号标识。`provider_error_category` 只能是以下有限值：

- `auth_or_permission`
- `rate_limited`
- `network_or_transport`
- `service_unavailable`
- `structured_output_rejected`
- `provider_internal_error`
- `cancelled`
- `unknown`

无法由明确 stderr 事实归类时固定为 `unknown`，不从业务内容猜测。

## Failure-only local bundle

只有失败调用会在本机临时目录下创建一个 bundle，内容固定为：

- `stdout.tail`
- `stderr.tail`
- `metadata.json`

目录权限为 `0700`，文件权限为 `0600`。每个 stream 仅保存最多 64 KiB tail；credential-like 字符串写盘前替换为 `[REDACTED]`。这不是业务文本脱敏承诺：bundle 仍按敏感本机材料处理，不能复制到 GitHub、Issue、PR 或长期业务数据。

`metadata.json` 仅保存允许的技术摘要与 `created_at` / `expires_at`，不保存环境、完整 argv、prompt 或 request。最大保留期为 24 小时；每次调用前会尽力清理过期 bundle。

## Reviewer cleanup

Reviewer 完成 allowlist 结论提取后，应调用 Provider 的 `cleanup_failure_diagnostics()` 立即删除本机 bundle，而不等待 TTL。返回失败表示本机清理没有完成，必须作为本地审计问题处理；不得静默宣称已清理。

成功调用不生成 raw diagnostic bundle。无论诊断是否可用，失败继续 fail closed：不发布 Processing package，不写入 Durable Atomic Information，也不写入 World Model。
