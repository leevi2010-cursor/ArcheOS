"""Fail-closed local storage for Normalized Representation manifests and artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from ..filesystem import publish_directory_no_replace
from ..source.identity import require_managed_source_id
from .identity import representation_id, require_content_hash, require_representation_id
from .models import (
    REPRESENTATION_SCHEMA_VERSION,
    NormalizedRepresentation,
    RepresentationArtifact,
    RepresentationVerificationResult,
    RepresentationWarning,
)


class RepresentationError(RuntimeError):
    """Base error for safe Normalized Representation operations."""


class RepresentationValidationError(RepresentationError):
    """An input or controlled layout was unsafe."""


class RepresentationManifestError(RepresentationError):
    """A persisted manifest did not satisfy strict v1."""


class RepresentationNotFoundError(RepresentationError):
    """A Representation was not present as a controlled regular directory."""


class RepresentationConflictError(RepresentationError):
    """A no-replace publication found an existing Representation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _hash_file(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _require_non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepresentationManifestError(f"{field} must be a non-empty string")
    return value.strip()


def _require_timestamp(value: object) -> str:
    timestamp = _require_non_empty(value, "generated_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RepresentationManifestError("generated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RepresentationManifestError("generated_at must include a timezone")
    return timestamp


def _require_completeness(value: object, status: str, warnings: tuple[RepresentationWarning, ...]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RepresentationManifestError("completeness must be a number")
    completeness = float(value)
    if not 0.0 <= completeness <= 1.0:
        raise RepresentationManifestError("completeness must be between 0.0 and 1.0")
    if status == "complete" and completeness != 1.0:
        raise RepresentationManifestError("complete Representation must have completeness 1.0")
    if status == "partial" and (completeness >= 1.0 or not warnings):
        raise RepresentationManifestError(
            "partial Representation must have completeness below 1.0 and warnings"
        )
    return completeness


def _require_locator(value: object, field: str = "locator") -> str:
    if not isinstance(value, str) or not value:
        raise RepresentationManifestError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("artifacts",):
        raise RepresentationManifestError(f"{field} escaped the controlled artifacts directory")
    if len(path.parts) < 2 or path.name in {"", ".", ".."}:
        raise RepresentationManifestError(f"{field} must identify an artifact file")
    return path.as_posix()


def _require_exact_keys(payload: object, expected: set[str], field: str) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RepresentationManifestError(f"{field} has unknown or missing fields")
    return payload


class LocalRepresentationRepository:
    """Local repository with strict manifests and atomic no-replace publication."""

    def __init__(
        self,
        representation_root: Path | str = Path("02_processing/representations"),
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.representation_root = Path(representation_root)
        self.staging_root = self.representation_root / ".staging"

        self.clock = clock

    @contextmanager
    def staging(self, source_id: str, representation_id: str) -> Iterator[Path]:
        source_id = self._source_id(source_id)
        representation_id = self._representation_id(representation_id)
        self._ensure_layout(create=True)
        path = Path(
            tempfile.mkdtemp(
                prefix=f"{source_id}-{representation_id[:12]}-", dir=self.staging_root
            )
        )
        try:
            (path / "artifacts").mkdir()
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def write_manifest(
        self, staging_dir: Path, representation: NormalizedRepresentation
    ) -> None:
        self._validate_staging_dir(staging_dir)
        payload = representation.to_manifest_dict()
        parsed = self._parse_manifest(json.loads(json.dumps(payload)))
        if parsed != representation:
            raise RepresentationManifestError("manifest round-trip did not preserve Representation")
        path = staging_dir / "manifest.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def publish(
        self, staging_dir: Path, representation: NormalizedRepresentation
    ) -> NormalizedRepresentation:
        self._validate_staging_dir(staging_dir)
        manifest_path = staging_dir / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RepresentationManifestError("staged Representation must contain a regular manifest")
        if self._read_manifest(manifest_path) != representation:
            raise RepresentationManifestError("staged manifest did not match the Representation")
        self._verify_artifacts_in(staging_dir, representation)
        source_root = self.representation_root / representation.source_id
        source_root.mkdir(parents=True, exist_ok=True)
        self._ensure_layout()
        if source_root.is_symlink() or not source_root.is_dir():
            raise RepresentationValidationError("Representation Source directory was unsafe")
        final_dir = source_root / representation.representation_id
        try:
            publish_directory_no_replace(staging_dir, final_dir)
        except FileExistsError as exc:
            raise RepresentationConflictError("Representation ID already exists") from exc
        return self.get(representation.representation_id)

    def get(self, representation_id: str) -> NormalizedRepresentation:
        representation_id = self._representation_id(representation_id)
        if not self.representation_root.exists():
            raise RepresentationNotFoundError("Representation was not found")
        self._ensure_layout()
        matches = list(self.representation_root.glob(f"src_*/{representation_id}"))
        if len(matches) != 1:
            raise RepresentationNotFoundError("Representation was not found")
        directory = matches[0]
        if directory.is_symlink() or not directory.is_dir():
            raise RepresentationNotFoundError("Representation was not found")
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RepresentationNotFoundError("Representation was not found")
        representation = self._read_manifest(manifest_path)
        if representation.representation_id != representation_id or directory.name != representation_id:
            raise RepresentationManifestError("Representation directory did not match its manifest ID")
        if directory.parent.name != representation.source_id:
            raise RepresentationManifestError("Representation directory did not match its Source ID")
        return representation

    def list_for_source(self, source_id: str) -> tuple[NormalizedRepresentation, ...]:
        source_id = self._source_id(source_id)
        source_root = self.representation_root / source_id
        if not source_root.exists():
            return ()
        self._ensure_layout()
        if source_root.is_symlink() or not source_root.is_dir():
            raise RepresentationValidationError("Representation Source directory was unsafe")
        return tuple(self.get(path.name) for path in sorted(source_root.iterdir(), key=lambda item: item.name))

    def verify(self, representation_id: str) -> RepresentationVerificationResult:
        try:
            representation = self.get(representation_id)
            directory = self._directory(representation)
            self._verify_artifacts_in(directory, representation)
        except RepresentationError as exc:
            return RepresentationVerificationResult(
                representation_id=str(representation_id), verified=False, reason=str(exc)
            )
        return RepresentationVerificationResult(
            representation_id=representation.representation_id, verified=True
        )

    def _directory(self, representation: NormalizedRepresentation) -> Path:
        directory = self.representation_root / representation.source_id / representation.representation_id
        try:
            root_real = self.representation_root.resolve(strict=True)
            directory_real = directory.resolve(strict=True)
            directory_real.relative_to(root_real)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepresentationValidationError("Representation directory escaped its root") from exc
        if directory.is_symlink():
            raise RepresentationValidationError("Representation directory must not be a symlink")
        return directory

    def _verify_artifacts_in(
        self, directory: Path, representation: NormalizedRepresentation
    ) -> None:
        seen: set[str] = set()
        for artifact in representation.artifacts:
            if artifact.locator in seen:
                raise RepresentationManifestError("artifact locators must be unique")
            seen.add(artifact.locator)
            path = self._artifact_path(directory, artifact.locator)
            if path.is_symlink() or not path.is_file():
                raise RepresentationManifestError("artifact is missing, unsafe, or not a regular file")
            observed_hash, observed_size = _hash_file(path)
            if observed_hash != artifact.content_hash or observed_size != artifact.size_bytes:
                raise RepresentationManifestError("artifact bytes did not match the manifest")

    def _artifact_path(self, directory: Path, locator: str) -> Path:
        locator = _require_locator(locator)
        path = directory / locator
        try:
            directory_real = directory.resolve(strict=True)
            artifacts_real = (directory / "artifacts").resolve(strict=True)
            parent_real = path.parent.resolve(strict=True)
            parent_real.relative_to(artifacts_real)
            directory_real.relative_to(self.representation_root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepresentationManifestError("artifact locator escaped its Representation") from exc
        if path.is_symlink():
            raise RepresentationManifestError("artifact must not be a symlink")
        return path

    def _validate_staging_dir(self, staging_dir: Path) -> None:
        try:
            staging_real = staging_dir.resolve(strict=True)
            root_real = self.staging_root.resolve(strict=True)
            staging_real.relative_to(root_real)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RepresentationValidationError("staging directory escaped the controlled root") from exc
        if staging_dir.is_symlink() or not staging_dir.is_dir():
            raise RepresentationValidationError("staging directory must be a regular directory")

    def _ensure_layout(self, *, create: bool = False) -> None:
        if create:
            self.representation_root.mkdir(parents=True, exist_ok=True)
            self.staging_root.mkdir(exist_ok=True)
        if not self.representation_root.exists():
            return
        if self.representation_root.is_symlink() or self.staging_root.is_symlink():
            raise RepresentationValidationError("Representation layout must not contain symlink roots")
        try:
            root_real = self.representation_root.resolve(strict=True)
            staging_real = self.staging_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RepresentationValidationError("Representation layout could not be resolved") from exc
        if staging_real != root_real / ".staging":
            raise RepresentationValidationError("Representation staging root escaped its controlled root")

    @staticmethod
    def _source_id(value: object) -> str:
        try:
            return require_managed_source_id(value)
        except ValueError as exc:
            raise RepresentationValidationError("source_id is invalid") from exc

    @staticmethod
    def _representation_id(value: object) -> str:
        try:
            return require_representation_id(value)
        except ValueError as exc:
            raise RepresentationValidationError("representation_id is invalid") from exc

    @classmethod
    def _read_manifest(cls, path: Path) -> NormalizedRepresentation:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepresentationManifestError("Representation manifest could not be read") from exc
        return cls._parse_manifest(payload)

    @classmethod
    def _parse_manifest(cls, payload: object) -> NormalizedRepresentation:
        data = _require_exact_keys(
            payload,
            {"schema_version", "representation_id", "source", "representation", "warnings", "artifacts"},
            "manifest",
        )
        if data["schema_version"] != REPRESENTATION_SCHEMA_VERSION:
            raise RepresentationManifestError("Representation schema version is unsupported")
        try:
            representation_id_value = require_representation_id(data["representation_id"])
            source = _require_exact_keys(data["source"], {"source_id", "content_hash"}, "source")
            source_id = require_managed_source_id(source["source_id"])
            source_hash = require_content_hash(source["content_hash"], field="source.content_hash")
            details = _require_exact_keys(
                data["representation"],
                {"kind", "adapter_name", "adapter_version", "configuration_fingerprint", "generated_at", "status", "completeness"},
                "representation",
            )
            kind = _require_non_empty(details["kind"], "representation.kind")
            adapter_name = _require_non_empty(details["adapter_name"], "representation.adapter_name")
            adapter_version = _require_non_empty(details["adapter_version"], "representation.adapter_version")
            fingerprint = require_content_hash(
                details["configuration_fingerprint"], field="configuration_fingerprint"
            )
        except ValueError as exc:
            raise RepresentationManifestError("Representation manifest identity was invalid") from exc
        generated_at = _require_timestamp(details["generated_at"])
        status = details["status"]
        if status not in {"complete", "partial"}:
            raise RepresentationManifestError("Representation status must be complete or partial")
        warnings = cls._parse_warnings(data["warnings"])
        completeness = _require_completeness(details["completeness"], status, warnings)
        artifacts = cls._parse_artifacts(data["artifacts"])
        if representation_id_value != representation_id(
            source_id=source_id,
            source_content_hash=source_hash,
            kind=kind,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            configuration_fingerprint=fingerprint,
        ):
            raise RepresentationManifestError(
                "Representation ID did not match its deterministic identity inputs"
            )
        return NormalizedRepresentation(
            representation_id=representation_id_value,
            source_id=source_id,
            source_content_hash=source_hash,
            kind=kind,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            configuration_fingerprint=fingerprint,
            generated_at=generated_at,
            status=status,
            completeness=completeness,
            warnings=warnings,
            artifacts=artifacts,
        )

    @staticmethod
    def _parse_warnings(payload: object) -> tuple[RepresentationWarning, ...]:
        if not isinstance(payload, list):
            raise RepresentationManifestError("warnings must be a list")
        warnings: list[RepresentationWarning] = []
        for item in payload:
            if not isinstance(item, dict) or set(item) not in (
                {"code", "message", "severity"},
                {"code", "message", "severity", "affected_locator"},
            ):
                raise RepresentationManifestError("warning has unknown or missing fields")
            severity = _require_non_empty(item["severity"], "warning.severity")
            if severity not in {"info", "warning", "error"}:
                raise RepresentationManifestError("warning severity is invalid")
            affected = item.get("affected_locator")
            if affected is not None:
                affected = _require_locator(affected, "warning.affected_locator")
            warnings.append(
                RepresentationWarning(
                    code=_require_non_empty(item["code"], "warning.code"),
                    message=_require_non_empty(item["message"], "warning.message"),
                    severity=severity,
                    affected_locator=affected,
                )
            )
        return tuple(warnings)

    @staticmethod
    def _parse_artifacts(payload: object) -> tuple[RepresentationArtifact, ...]:
        if not isinstance(payload, list) or not payload:
            raise RepresentationManifestError("artifacts must be a non-empty list")
        artifacts: list[RepresentationArtifact] = []
        ids: set[str] = set()
        locators: set[str] = set()
        for item in payload:
            data = _require_exact_keys(
                item,
                {"artifact_id", "kind", "locator", "media_type", "size_bytes", "content_hash"},
                "artifact",
            )
            artifact_id = _require_non_empty(data["artifact_id"], "artifact.artifact_id")
            locator = _require_locator(data["locator"])
            if artifact_id in ids or locator in locators:
                raise RepresentationManifestError("artifact IDs and locators must be unique")
            size = data["size_bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise RepresentationManifestError("artifact.size_bytes must be a non-negative integer")
            try:
                content_hash = require_content_hash(data["content_hash"], field="artifact.content_hash")
            except ValueError as exc:
                raise RepresentationManifestError("artifact hash is invalid") from exc
            ids.add(artifact_id)
            locators.add(locator)
            artifacts.append(
                RepresentationArtifact(
                    artifact_id=artifact_id,
                    kind=_require_non_empty(data["kind"], "artifact.kind"),
                    locator=locator,
                    media_type=_require_non_empty(data["media_type"], "artifact.media_type"),
                    size_bytes=size,
                    content_hash=content_hash,
                )
            )
        return tuple(artifacts)
