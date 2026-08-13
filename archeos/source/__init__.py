"""Local Managed Source admission, verification, and restore."""

from .contracts import ManagedSourceAccess, ManagedSourceRepository
from .handoff import (
    HandoffMarker,
    HandoffMarkerConflictError,
    HandoffMarkerError,
    HandoffMarkerService,
    HandoffShowResult,
    HandoffWriteResult,
)
from .local_repository import (
    AdmissionError,
    LocalManagedSourceRepository,
    ManifestError,
    SourceConflictError,
    SourceError,
    SourceIntegrityError,
    SourceNotFoundError,
    SourceValidationError,
)
from .models import (
    AdmissionResult,
    IngestedFrom,
    ManagedSource,
    RestoreResult,
    StorageReplica,
    VerificationRecord,
    VerificationResult,
)
from .service import ManagedSourceService

__all__ = [
    "AdmissionError",
    "AdmissionResult",
    "HandoffMarker",
    "HandoffMarkerConflictError",
    "HandoffMarkerError",
    "HandoffMarkerService",
    "HandoffShowResult",
    "HandoffWriteResult",
    "IngestedFrom",
    "LocalManagedSourceRepository",
    "ManagedSource",
    "ManagedSourceAccess",
    "ManagedSourceRepository",
    "ManagedSourceService",
    "ManifestError",
    "RestoreResult",
    "SourceConflictError",
    "SourceError",
    "SourceIntegrityError",
    "SourceNotFoundError",
    "SourceValidationError",
    "StorageReplica",
    "VerificationRecord",
    "VerificationResult",
]
