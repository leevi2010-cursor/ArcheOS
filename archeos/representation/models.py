"""Immutable public models for replaceable Normalized Representations."""

from __future__ import annotations

from dataclasses import dataclass


REPRESENTATION_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class RepresentationArtifact:
    artifact_id: str
    kind: str
    locator: str
    media_type: str
    size_bytes: int
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "locator": self.locator,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class RepresentationWarning:
    code: str
    message: str
    severity: str
    affected_locator: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.affected_locator is not None:
            payload["affected_locator"] = self.affected_locator
        return payload


@dataclass(frozen=True)
class NormalizedRepresentation:
    representation_id: str
    source_id: str
    source_content_hash: str
    kind: str
    adapter_name: str
    adapter_version: str
    configuration_fingerprint: str
    generated_at: str
    status: str
    completeness: float
    warnings: tuple[RepresentationWarning, ...]
    artifacts: tuple[RepresentationArtifact, ...]

    def to_manifest_dict(self) -> dict[str, object]:
        return {
            "schema_version": REPRESENTATION_SCHEMA_VERSION,
            "representation_id": self.representation_id,
            "source": {
                "source_id": self.source_id,
                "content_hash": self.source_content_hash,
            },
            "representation": {
                "kind": self.kind,
                "adapter_name": self.adapter_name,
                "adapter_version": self.adapter_version,
                "configuration_fingerprint": self.configuration_fingerprint,
                "generated_at": self.generated_at,
                "status": self.status,
                "completeness": self.completeness,
            },
            "warnings": [warning.to_dict() for warning in self.warnings],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    def to_dict(self) -> dict[str, object]:
        return self.to_manifest_dict()


@dataclass(frozen=True)
class AdapterArtifact:
    """One file the Adapter created below its assigned staging directory."""

    kind: str
    locator: str
    media_type: str


@dataclass(frozen=True)
class AdapterBuildResult:
    kind: str
    artifacts: tuple[AdapterArtifact, ...]
    completeness: float
    warnings: tuple[RepresentationWarning, ...] = ()


@dataclass(frozen=True)
class RepresentationBuildResult:
    representation: NormalizedRepresentation
    status: str

    def to_dict(self) -> dict[str, object]:
        return {"representation": self.representation.to_dict(), "status": self.status}


@dataclass(frozen=True)
class RepresentationVerificationResult:
    representation_id: str
    verified: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "representation_id": self.representation_id,
            "verified": self.verified,
            "reason": self.reason,
        }
