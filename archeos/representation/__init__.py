"""Canonical contracts and local runtime for Normalized Representations."""

from .contracts import RepresentationAdapter, RepresentationRepository
from .local_repository import (
    LocalRepresentationRepository,
    RepresentationConflictError,
    RepresentationError,
    RepresentationManifestError,
    RepresentationNotFoundError,
    RepresentationValidationError,
)
from .models import (
    AdapterArtifact,
    AdapterBuildResult,
    NormalizedRepresentation,
    RepresentationArtifact,
    RepresentationBuildResult,
    RepresentationVerificationResult,
    RepresentationWarning,
)
from .service import RepresentationService
from .wechat import (
    WechatConversationError,
    WechatConversationRepresentationAdapter,
    WechatConversationV2RepresentationAdapter,
    validate_wechat_conversation_artifact,
    validate_wechat_conversation_v2_artifact,
    wechat_conversation_analysis_rows,
    wechat_conversation_metrics,
)

__all__ = [
    "AdapterArtifact",
    "AdapterBuildResult",
    "LocalRepresentationRepository",
    "NormalizedRepresentation",
    "RepresentationAdapter",
    "RepresentationArtifact",
    "RepresentationBuildResult",
    "RepresentationConflictError",
    "RepresentationError",
    "RepresentationManifestError",
    "RepresentationNotFoundError",
    "RepresentationRepository",
    "RepresentationService",
    "RepresentationValidationError",
    "RepresentationVerificationResult",
    "RepresentationWarning",
    "WechatConversationError",
    "WechatConversationRepresentationAdapter",
    "WechatConversationV2RepresentationAdapter",
    "validate_wechat_conversation_artifact",
    "validate_wechat_conversation_v2_artifact",
    "wechat_conversation_analysis_rows",
    "wechat_conversation_metrics",
]
