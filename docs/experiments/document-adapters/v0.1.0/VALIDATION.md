# Issue #30 本地 Adapter 验证记录

## 范围

本记录对应 Issue #30 的收窄范围：Markdown、文本型 PDF、XLSX、PPTX 与图片结构预检。
OCR、复杂 PDF、视觉语义、云服务和模型下载均未实现，也未参与本次验证。

## 运行边界

- 真实样本只在本机短暂读取；原文件未修改。
- 处理过程使用临时的 Managed Source 与 Representation 目录，进程结束后清理。
- smoke 进程拦截网络连接入口；未观察到网络访问。
- 本文件只保留聚合统计；不包含真实路径、文件名、正文、图片、哈希或业务实体。

## 合成验证

`tests/test_document_adapters.py` 覆盖 9 个场景：五个 Adapter 的结构、locator、warning、registry/CLI、受控图片 privacy route、格式化空白 XLSX 单元格、PPTX GroupShape 递归保留，以及 PDF runtime error 的无发布/清理行为。

## 匿名本地 smoke

执行日期：2026-08-13。每种可用格式最多选择 1 个代表样本。

| 格式 | 样本数 | 状态 | verify | warning | artifact | 耗时（毫秒） |
| --- | ---: | --- | --- | --- | ---: | ---: |
| 文本型 PDF | 1 | partial | true | `READING_ORDER_NOT_VERIFIED` | 1 | 160 |
| XLSX | 1 | partial | true | `EMBEDDED_MEDIA_UNSUPPORTED` | 1 | 1236 |
| PPTX | 1 | complete | true | 无 | 1 | 207 |
| 图片 | 1 | partial | true | `PRIVACY_ROUTE_UNKNOWN` | 1 | 4 |
| Markdown | 0 | not_available | 不适用 | 不适用 | 0 | 不适用 |

其中 XLSX warning 表示 Reader 报告存在不支持的嵌入媒体；Adapter 未保留该内容且没有把该运行时提示吞掉。图片样本在没有显式隐私路由时保持 `unknown`，不进行 OCR、视觉理解或路由降级。

## 未验证项与风险

- 扫描或 image-heavy PDF 仅返回结构化 `partial` warning；OCR 后置到独立 Issue。
- 未以真实 Markdown 样本验证，因为授权样本中没有可用 Markdown。
- 本次只验证轻量本地库的当前受控版本；复杂布局、宏、外部链接、动画、嵌入对象和视觉语义均不在本轮承诺内。
