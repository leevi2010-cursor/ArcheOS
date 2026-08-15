# Codex CLI Schema Compatibility v0.1.0

这是 Issue #80 的公开 synthetic-only 执行实验。它只验证 `type + const` 的 Codex structured-output
兼容形态，不修改 #76 的历史实验、#31/#48 的语义合同或 production runtime。

## 固定边界

- 仅使用脚本内公开 synthetic anchor / context support；不打开真实 Source、Representation、微信资料或 #76 marker；
- 最多 3 次正式 `codex exec` 调用，严格顺序为最小 `type + const`、2-anchor contract、19-anchor contract；
- 任一前置 run 失败即停止，不 retry、不 fallback、不换 Provider；
- 每次调用前先以 declared core dependency 的 Draft 2020-12 validator 检查完整 schema 合法性，再递归检查每个含
  `const` 的 schema node 都有匹配的显式 JSON Schema `type`；任一失败均在启动 Codex 前 fail closed，且不重写 schema；
- `protocol_version`、`input_fingerprint`、`additionalProperties: false`、Candidate / Residue、#31 coverage 与 context Evidence validator 保持严格不变；
- 不写 package、Atomic Information 或 World Model。

## 执行

```bash
python3 docs/experiments/codex-cli-schema-compat/v0.1.0/run_schema_compat.py \
  --report /tmp/archeos-issue80-report.json \
  --timeout 120
```

报告必须在仓库外的 local-only 路径。提交前只把公开 synthetic、已脱敏的结论写入 `RESULTS.md`；不得提交
环境变量、token、本机路径或任何真实数据。

`codex --version` 和每次执行都记录 startup、timeout、exit code 与受控 process-group cleanup 结果。timeout 时按
`SIGTERM → bounded drain/reap → absence verify → SIGKILL（仅在仍存在时）→ bounded drain/reap → final verify`
收口，避免把尚未回收的 zombie 误报为残留进程，pipe drain 也有固定上限。
