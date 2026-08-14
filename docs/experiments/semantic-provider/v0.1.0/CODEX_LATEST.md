# B. Latest Codex Python SDK（隔离环境）

## 设置

按未固定版本安装到新的 Python 3.13 环境，并在运行时查询 package index。可获得的最新 `openai-codex` 仍为 `0.144.4`，所以它与 pinned 路线没有 SDK 版本差异。

其他安全与输入条件与 A 相同：只发送公开合成 package，`deny_all`、read-only、ephemeral、120 秒 deadline，不访问 Managed Source 或 World Model。

## 结果

一次正式合成运行在 16.942 秒完成，严格输出和完整 coverage 均通过：13/13 单元已覆盖，`unaccounted_eligible_units=0`，没有 timeout 或 runtime failure。fixture oracle 中 3 个刻意不确定/冲突单元被归入不符合预期的一侧。

## 结论

本次没有“latest SDK”可与 pinned SDK 形成有效版本对照，因此不能以该结果建议升级依赖，也不能把输出差异归因于 SDK 版本。保留为版本观察基线；只有出现新的官方 stable package 后，才有意义重做隔离比较。
