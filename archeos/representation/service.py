"""Application service for building safe, replaceable Normalized Representations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Mapping

from ..source.contracts import ManagedSourceAccess
from ..source.identity import require_managed_source_id
from ..source.models import ManagedSource
from .contracts import RepresentationAdapter, RepresentationRepository
from .identity import canonical_configuration_fingerprint, require_content_hash, representation_id
from .local_repository import (
    RepresentationConflictError,
    RepresentationError,
    RepresentationNotFoundError,
    RepresentationValidationError,
)
from .models import (
    AdapterArtifact,
    AdapterBuildResult,
    NormalizedRepresentation,
    RepresentationArtifact,
    RepresentationBuildResult,
)


class RepresentationService:
    """Coordinate verified Source materialization with an isolated Adapter."""

    def __init__(
        self,
        source_access: ManagedSourceAccess,
        repository: RepresentationRepository,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.source_access = source_access
        self.repository = repository
        self.clock = clock

    def build(
        self,
        source_id: str,
        adapter: RepresentationAdapter,
        configuration: Mapping[str, object] | None = None,
    ) -> RepresentationBuildResult:
        requested_source_id = self._require_source_id(source_id)
        configuration = {} if configuration is None else configuration
        fingerprint = canonical_configuration_fingerprint(configuration)
        source = self._get_source(requested_source_id)
        self._verify_source(requested_source_id, source)
        self._validate_adapter(adapter, source)
        representation_id_value = representation_id(
            source_id=requested_source_id,
            source_content_hash=source.content_hash,
            kind=adapter.kind,
            adapter_name=adapter.name,
            adapter_version=adapter.version,
            configuration_fingerprint=fingerprint,
        )
        existing = self._existing(representation_id_value)
        if existing is not None:
            return RepresentationBuildResult(existing, "existing")

        with self.source_access.materialize(requested_source_id) as materialized_path:
            self._verify_source(requested_source_id, source)
            self._verify_materialized(Path(materialized_path), source)
            with self.repository.staging(requested_source_id, representation_id_value) as staging_dir:
                try:
                    built = adapter.build(source, Path(materialized_path), staging_dir, configuration)
                except Exception as exc:
                    raise RepresentationError("Representation Adapter failed") from exc
                self._verify_materialized(Path(materialized_path), source)
                representation = self._representation_from_build(
                    representation_id_value,
                    source,
                    adapter,
                    fingerprint,
                    staging_dir,
                    built,
                )
                self.repository.write_manifest(staging_dir, representation)
                self.repository.validate_staged(staging_dir, representation)
                self._verify_source(requested_source_id, source)
                try:
                    published = self.repository.publish(staging_dir, representation)
                except RepresentationConflictError:
                    existing = self._existing(representation_id_value)
                    if existing is None:
                        raise
                    return RepresentationBuildResult(existing, "existing")
                if published != representation:
                    raise RepresentationError("published Representation did not match the staged result")
                return RepresentationBuildResult(published, "built")

    def show(self, representation_id_value: str) -> NormalizedRepresentation:
        return self.repository.get(representation_id_value)

    def list(self, source_id: str) -> tuple[NormalizedRepresentation, ...]:
        return self.repository.list_for_source(self._require_source_id(source_id))

    def verify(self, representation_id_value: str):
        return self.repository.verify(representation_id_value)

    def _existing(self, representation_id_value: str) -> NormalizedRepresentation | None:
        try:
            representation = self.repository.get(representation_id_value)
        except RepresentationNotFoundError:
            return None
        verification = self.repository.verify(representation_id_value)
        if not verification.verified:
            raise RepresentationError("existing Representation failed verification and will not be overwritten")
        return representation

    @staticmethod
    def _require_source_id(value: object) -> str:
        try:
            return require_managed_source_id(value)
        except ValueError as exc:
            raise RepresentationValidationError("Managed Source ID is invalid") from exc

    def _get_source(self, requested_source_id: str) -> ManagedSource:
        try:
            source = self.source_access.get(requested_source_id)
        except Exception as exc:
            raise RepresentationError("Managed Source could not be read") from exc
        self._require_returned_source_id(source.source_id, requested_source_id)
        try:
            require_content_hash(source.content_hash, field="Managed Source content_hash")
        except ValueError as exc:
            raise RepresentationError("Managed Source content hash was invalid") from exc
        if isinstance(source.size_bytes, bool) or not isinstance(source.size_bytes, int) or source.size_bytes < 0:
            raise RepresentationError("Managed Source size was invalid")
        if source.availability != "available":
            raise RepresentationError("Managed Source is unavailable")
        return source

    def _verify_source(self, requested_source_id: str, source: ManagedSource) -> None:
        try:
            verification = self.source_access.verify(requested_source_id)
        except Exception as exc:
            raise RepresentationError("Managed Source could not be verified") from exc
        self._require_returned_source_id(verification.source_id, requested_source_id)
        if not verification.verified:
            raise RepresentationError("Managed Source failed verification")
        if (
            verification.expected_content_hash != source.content_hash
            or verification.observed_content_hash != source.content_hash
            or verification.expected_size_bytes != source.size_bytes
            or verification.observed_size_bytes != source.size_bytes
        ):
            raise RepresentationError("Managed Source verification did not match the immutable snapshot")

    @staticmethod
    def _require_returned_source_id(value: object, requested_source_id: str) -> None:
        try:
            returned = require_managed_source_id(value)
        except ValueError as exc:
            raise RepresentationError("Managed Source identity was invalid") from exc
        if returned != requested_source_id:
            raise RepresentationError("Managed Source identity did not match the request")

    @staticmethod
    def _validate_adapter(adapter: RepresentationAdapter, source: ManagedSource) -> None:
        for field in (adapter.name, adapter.version, adapter.kind):
            if not isinstance(field, str) or not field.strip():
                raise RepresentationValidationError("Adapter identity fields must be non-empty strings")
        if source.media_type not in adapter.supported_media_types:
            raise RepresentationValidationError("Adapter does not support the Managed Source media type")

    @staticmethod
    def _verify_materialized(path: Path, source: ManagedSource) -> None:
        if path.is_symlink() or not path.is_file():
            raise RepresentationError("materialized Source was not a regular file")
        observed_hash, observed_size = RepresentationService._hash_file(path)
        if observed_hash != source.content_hash or observed_size != source.size_bytes:
            raise RepresentationError("materialized Source did not match the immutable snapshot")

    def _representation_from_build(
        self,
        representation_id_value: str,
        source: ManagedSource,
        adapter: RepresentationAdapter,
        fingerprint: str,
        staging_dir: Path,
        built: AdapterBuildResult,
    ) -> NormalizedRepresentation:
        if not isinstance(built, AdapterBuildResult) or built.kind != adapter.kind:
            raise RepresentationError("Adapter returned an invalid Representation result")
        status = "complete" if built.completeness == 1.0 else "partial"
        artifacts = tuple(
            self._artifact_from_staging(representation_id_value, staging_dir, artifact)
            for artifact in built.artifacts
        )
        generated_at = self.clock() if self.clock is not None else self._utc_now()
        return NormalizedRepresentation(
            representation_id=representation_id_value,
            source_id=source.source_id,
            source_content_hash=source.content_hash,
            kind=adapter.kind,
            adapter_name=adapter.name,
            adapter_version=adapter.version,
            configuration_fingerprint=fingerprint,
            generated_at=generated_at,
            status=status,
            completeness=built.completeness,
            warnings=tuple(built.warnings),
            artifacts=artifacts,
        )

    @staticmethod
    def _artifact_from_staging(
        representation_id_value: str, staging_dir: Path, artifact: AdapterArtifact
    ) -> RepresentationArtifact:
        if not isinstance(artifact, AdapterArtifact):
            raise RepresentationError("Adapter returned an invalid artifact")
        if not all(isinstance(value, str) and value.strip() for value in (artifact.kind, artifact.locator, artifact.media_type)):
            raise RepresentationError("Adapter artifact fields must be non-empty strings")
        locator_path = Path(artifact.locator)
        if (
            locator_path.is_absolute()
            or ".." in locator_path.parts
            or locator_path.parts[:1] != ("artifacts",)
            or len(locator_path.parts) < 2
        ):
            raise RepresentationError("Adapter artifact locator escaped staging")
        path = staging_dir / locator_path
        try:
            artifacts_root = (staging_dir / "artifacts").resolve(strict=True)
            path.parent.resolve(strict=True).relative_to(artifacts_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepresentationError("Adapter artifact locator escaped staging") from exc
        if path.is_symlink() or not path.is_file():
            raise RepresentationError("Adapter artifact was not a regular file")
        content_hash, size_bytes = RepresentationService._hash_file(path)
        artifact_id = hashlib.sha256(
            f"{representation_id_value}\n{artifact.locator}\n{artifact.kind}\n{artifact.media_type}".encode("utf-8")
        ).hexdigest()
        return RepresentationArtifact(
            artifact_id=f"artifact_{artifact_id}",
            kind=artifact.kind,
            locator=locator_path.as_posix(),
            media_type=artifact.media_type,
            size_bytes=size_bytes,
            content_hash=content_hash,
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return f"sha256:{digest.hexdigest()}", size

    @staticmethod
    def _utc_now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
