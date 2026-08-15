"""The approved, local-only production Representation Adapter registry."""

from __future__ import annotations

from .adapters import (
    ImagePreflightRepresentationAdapter,
    MarkdownRepresentationAdapter,
    PdfTextRepresentationAdapter,
    PptxRepresentationAdapter,
    XlsxRepresentationAdapter,
)
from .contracts import RepresentationAdapter
from .local_repository import RepresentationValidationError
from .wechat import WechatConversationRepresentationAdapter


def production_adapter(name: str) -> RepresentationAdapter:
    adapters: dict[str, RepresentationAdapter] = {
        "markdown": MarkdownRepresentationAdapter(),
        "pdf-text": PdfTextRepresentationAdapter(),
        "xlsx": XlsxRepresentationAdapter(),
        "pptx": PptxRepresentationAdapter(),
        "image-preflight": ImagePreflightRepresentationAdapter(),
        "wechat-conversation": WechatConversationRepresentationAdapter(),
    }
    try:
        return adapters[name]
    except KeyError as exc:
        raise RepresentationValidationError("Representation Adapter is not approved") from exc
