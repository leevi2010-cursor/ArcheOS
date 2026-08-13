# 许可证、安全与本地隐私

## 许可证门禁

- 可优先验证的宽松许可候选包括 MIT（`markdown-it-py`、MarkItDown、Docling、`pdfplumber`、`openpyxl`、`python-pptx`）、Apache-2.0（Tika、Tesseract、PaddleOCR、POI、Unstructured）和 MPL-2.0（LibreOffice）。
- PyMuPDF 为 AGPL-3.0 或商业许可，不能默认引入。
- MinerU 使用带额外商业门槛或在线服务署名要求的许可，必须逐版本法务复核。
- Exiv2 为 GPL-2.0-or-later；若未来使用，优先隔离 CLI / service 并单独评估链接与分发边界。
- ImageMagick 使用其自有宽松许可，但安全 policy 不是默认安全配置。

许可证调查不是最终法务意见；新增依赖前仍须检查传递依赖、模型权重许可和分发方式。

## 网络与模型门禁

| 风险 | 默认行为 |
| --- | --- |
| 首次下载模型 | 禁止；仅允许在非敏感环境预取、锁定版本与 artifact 后运行 |
| remote API / hosted service | 禁止；必须另行授权和独立隐私路线 |
| 第三方插件 | 禁止；显式 allowlist 后才可启用 |
| URL / network parser / delegate | 禁止；Adapter 只接收受管本地 Source bytes 或 locator |
| cloud fallback | 禁止；模型缺失或断网必须 fail closed |

Docling、PaddleOCR、MinerU、Unstructured 的模型或高级路径均需在真实样本前做离线和网络连接验证。MarkItDown 的 Azure、LLM image/OCR 等能力同样不得默认启用。Tika server、ImageMagick delegate、Exiv2 URL 输入以及视觉模型下载参数必须由 Adapter 阻断。

## 图片与 restricted 处理

`scene`、`document`、`restricted` 是未来 Safety / Processing policy 的处理等级，不是新的 Core 概念。结构元数据不能可靠判定图片语义；restricted 必须由 intake 明示、来源策略或本地安全规则触发，未知或低置信度时进入人工路径，绝不自动降级。

- scene：只做离线结构检查；视觉理解后置，输出仅为 preview / candidate；
- document：可在锁定本地模型后进行 OCR，保留 bbox、置信度、坐标系和 engine/version；
- restricted：默认无网络、无远程 URL、无云 fallback；不进行人脸识别、身份推断、声纹或 Person 匹配。

原始 EXIF/IPTC/XMP、缩略图、GPS、设备与时间信息都按敏感数据对待。对外报告仅使用 allowlist 结构字段，临时数据要最小权限、受限资源并在结束后清理。
