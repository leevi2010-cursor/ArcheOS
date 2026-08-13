"""Filesystem adapter for the local Managed Source repository.

The adapter owns only byte-level admission, verification, and restore.  It does
not know about Processing, Normalized Representation, Information, or the
World Model.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from errno import EEXIST, ENOTEMPTY
from ctypes import CDLL, c_char_p, c_int, get_errno
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .models import (
    MANIFEST_SCHEMA_VERSION,
    AdmissionResult,
    IngestedFrom,
    ManagedSource,
    RestoreResult,
    StorageReplica,
    VerificationRecord,
    VerificationResult,
)


CHUNK_SIZE = 1024 * 1024
SOURCE_ID_PATTERN = re.compile(r"^src_[0-9a-f]{32}$")
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_DIR_NAME = "sources"
STAGING_DIR_NAME = ".staging"
AT_FDCWD_DARWIN = -2
AT_FDCWD_LINUX = -100
RENAME_EXCL_DARWIN = 0x00000004
RENAME_NOREPLACE_LINUX = 1


class SourceError(RuntimeError):
    """Base error for safe Managed Source operations."""


class SourceValidationError(SourceError):
    """The requested input or persisted record is invalid."""


class AdmissionError(SourceError):
    """Admission could not complete and no Source was published."""


class SourceConflictError(AdmissionError):
    """The provisional Source ID would overwrite an existing Source."""


class SourceIntegrityError(SourceError):
    """A Managed Source failed byte-level verification."""


class SourceNotFoundError(SourceError):
    """The requested Source is unknown."""


class ManifestError(SourceError):
    """A persisted Manifest is not a supported strict Manifest v1."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _default_id_factory() -> str:
    return f"src_{uuid.uuid4().hex}"


def _validate_source_id(source_id: object) -> str:
    if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise SourceValidationError(
            "source_id must be src_ followed by 32 lowercase hex characters"
        )
    return source_id


def _validate_hash(value: object, field: str = "content_hash") -> str:
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        raise ManifestError(f"{field} must be a full sha256 hash")
    return value


def _validate_non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    return value.strip()


def _validate_timestamp(value: object, field: str) -> str:
    timestamp = _validate_non_empty_string(value, field)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ManifestError(f"{field} must include a timezone")
    return timestamp


def _validate_size(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(f"{field} must be a non-negative integer")
    return value


def _validate_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{field} must be a boolean")
    return value


def _hash_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _copy_stream(
    source_path: Path,
    destination_path: Path,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with source_path.open("rb") as source, destination_path.open("xb") as destination:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            destination.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        destination.flush()
        os.fsync(destination.fileno())
    return f"sha256:{digest.hexdigest()}", size


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix
    if not suffix or "/" in suffix or "\\" in suffix:
        return ""
    return suffix[:32]


def _is_lexically_safe_locator(locator: object, source_id: str) -> bool:
    if not isinstance(locator, str) or not locator:
        return False
    path = Path(locator)
    expected_parts = (SOURCE_DIR_NAME, source_id)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) != 3
        or path.parts[:2] != expected_parts
    ):
        return False
    return path.name.startswith("original")


def _publish_directory_no_replace(staging_path: Path, final_path: Path) -> None:
    """Publish a fully written Source directory without replacing a rival path."""

    libc = CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename_no_replace = libc.renameatx_np
        directory_fd = AT_FDCWD_DARWIN
        flag = RENAME_EXCL_DARWIN
    elif sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename_no_replace = libc.renameat2
        directory_fd = AT_FDCWD_LINUX
        flag = RENAME_NOREPLACE_LINUX
    else:
        raise OSError("atomic no-replace directory publication is unavailable")
    rename_no_replace.argtypes = (c_int, c_char_p, c_int, c_char_p, c_int)
    rename_no_replace.restype = c_int
    result = rename_no_replace(
        directory_fd,
        os.fsencode(staging_path),
        directory_fd,
        os.fsencode(final_path),
        flag,
    )
    if result == 0:
        return
    error_number = get_errno()
    if error_number in {EEXIST, ENOTEMPTY}:
        raise FileExistsError(error_number, "Managed Source ID already exists", final_path)
    raise OSError(error_number, "atomic Managed Source publication failed", final_path)


def _publish_file_no_replace(staging_path: Path, target_path: Path) -> None:
    """Atomically publish a regular file without ever replacing target_path."""

    try:
        os.link(staging_path, target_path, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError:
        raise
    staging_path.unlink()


class LocalManagedSourceRepository:
    """A local, storage-independent implementation of the repository contract."""

    def __init__(
        self,
        managed_root: Path | str = Path("01_inbox"),
        *,
        id_factory: Callable[[], str] = _default_id_factory,
        source_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], str] = _utc_now,
        chunk_size: int = CHUNK_SIZE,
        media_type_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.managed_root = Path(managed_root)
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        self.id_factory = source_id_factory or id_factory
        self.clock = clock
        self.chunk_size = chunk_size
        self.media_type_resolver = media_type_resolver or mimetypes.guess_type
        self.sources_root = self.managed_root / SOURCE_DIR_NAME
        self.staging_root = self.managed_root / STAGING_DIR_NAME

    def admit(
        self,
        external_path: Path | str,
        source_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> AdmissionResult:
        """Copy and publish one immutable Source, or cleanly fail closed."""

        source_path = Path(external_path)
        initial_stat = self._validate_external_file(source_path)
        provisional_id = _validate_source_id(
            self.id_factory() if source_id is None else source_id
        )
        metadata = metadata or {}
        filename_hint = self._filename_hint(metadata, source_path)
        media_type = self._media_type(metadata, filename_hint)
        observed_at = self.clock()

        self._ensure_controlled_layout(create=True)
        staging_dir = Path(
            tempfile.mkdtemp(prefix="admit-", dir=str(self.staging_root))
        )
        published = False
        try:
            original_path = staging_dir / f"original{_safe_suffix(filename_hint)}"
            streamed_hash, streamed_size = _copy_stream(
                source_path,
                original_path,
                chunk_size=self.chunk_size,
            )
            final_stat = self._validate_external_file(source_path)
            if not self._same_input_snapshot(initial_stat, final_stat):
                raise AdmissionError(
                    "external file changed during admission; no Managed Source was published"
                )
            if streamed_size != initial_stat.st_size:
                raise AdmissionError("external file size changed during admission")

            source_hash, source_size = _hash_file(
                source_path,
                chunk_size=self.chunk_size,
            )
            if source_hash != streamed_hash or source_size != streamed_size:
                raise AdmissionError("external file changed during admission")

            managed_hash, managed_size = _hash_file(
                original_path,
                chunk_size=self.chunk_size,
            )
            if streamed_hash != managed_hash or streamed_size != managed_size:
                raise AdmissionError("managed copy failed independent byte verification")

            equivalent_ids = self._content_equivalents(managed_hash)
            archived_at = self.clock()
            ingested_from = self._ingested_from(
                metadata,
                source_path,
                observed_at,
            )
            managed_locator = f"{SOURCE_DIR_NAME}/{provisional_id}/{original_path.name}"
            verification = VerificationRecord(
                verified_at=archived_at,
                observed_content_hash=streamed_hash,
                managed_content_hash=managed_hash,
                observed_size_bytes=streamed_size,
                managed_size_bytes=managed_size,
                hash_matches=streamed_hash == managed_hash,
                size_matches=streamed_size == managed_size,
            )
            source = ManagedSource(
                source_id=provisional_id,
                content_hash=managed_hash,
                size_bytes=managed_size,
                media_type=media_type,
                filename_hint=filename_hint,
                managed_locator=managed_locator,
                archived_at=archived_at,
                availability="available",
                ingested_from=ingested_from,
                verification_records=(verification,),
                storage_replicas=(
                    StorageReplica(
                        kind="local",
                        location=managed_locator,
                        status="verified",
                    ),
                ),
            )
            self._write_manifest(staging_dir / "manifest.json", source)

            final_dir = self.sources_root / provisional_id
            try:
                _publish_directory_no_replace(staging_dir, final_dir)
            except FileExistsError as exc:
                raise SourceConflictError("Managed Source ID already exists") from exc
            published = True
            return AdmissionResult(
                source=source,
                content_equivalent_source_ids=equivalent_ids,
            )
        except SourceError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise AdmissionError("Managed Source admission failed") from exc
        finally:
            if not published:
                shutil.rmtree(staging_dir, ignore_errors=True)

    def get(self, source_id: str) -> ManagedSource:
        if not self.sources_root.exists():
            raise SourceNotFoundError("Managed Source was not found")
        self._ensure_controlled_layout()
        source_id = _validate_source_id(source_id)
        source_dir = self.sources_root / source_id
        manifest_path = source_dir / "manifest.json"
        if (
            source_dir.is_symlink()
            or not source_dir.is_dir()
            or not manifest_path.is_file()
            or manifest_path.is_symlink()
        ):
            raise SourceNotFoundError("Managed Source was not found")
        return self._read_manifest(manifest_path)

    def list_sources(self) -> tuple[ManagedSource, ...]:
        if not self.sources_root.is_dir():
            return ()
        self._ensure_controlled_layout()
        sources: list[ManagedSource] = []
        for source_dir in sorted(self.sources_root.iterdir(), key=lambda path: path.name):
            if not source_dir.is_dir() or source_dir.is_symlink():
                continue
            sources.append(self.get(source_dir.name))
        return tuple(sources)

    def verify(self, source_id: str) -> VerificationResult:
        source = self.get(source_id)
        checked_at = self.clock()
        managed_path = self._managed_path(source)
        if not managed_path.is_file() or managed_path.is_symlink():
            return VerificationResult(
                source_id=source.source_id,
                verified=False,
                expected_content_hash=source.content_hash,
                observed_content_hash=None,
                expected_size_bytes=source.size_bytes,
                observed_size_bytes=None,
                checked_at=checked_at,
                reason="managed bytes are missing or not a regular file",
            )
        try:
            observed_hash, observed_size = _hash_file(
                managed_path,
                chunk_size=self.chunk_size,
            )
        except OSError:
            return VerificationResult(
                source_id=source.source_id,
                verified=False,
                expected_content_hash=source.content_hash,
                observed_content_hash=None,
                expected_size_bytes=source.size_bytes,
                observed_size_bytes=None,
                checked_at=checked_at,
                reason="managed bytes could not be read",
            )
        hash_matches = observed_hash == source.content_hash
        size_matches = observed_size == source.size_bytes
        return VerificationResult(
            source_id=source.source_id,
            verified=hash_matches and size_matches and source.availability == "available",
            expected_content_hash=source.content_hash,
            observed_content_hash=observed_hash,
            expected_size_bytes=source.size_bytes,
            observed_size_bytes=observed_size,
            checked_at=checked_at,
            reason=(
                None
                if hash_matches and size_matches and source.availability == "available"
                else "managed bytes do not match the immutable Manifest"
            ),
        )

    @contextmanager
    def materialize(self, source_id: str) -> Iterator[Path]:
        """Expose verified local bytes for one bounded Processing operation."""

        source = self.get(source_id)
        managed_path = self._managed_path(source)
        if not managed_path.is_file() or managed_path.is_symlink():
            raise SourceIntegrityError("managed bytes are missing or not a regular file")
        yield managed_path

    def restore(self, source_id: str, target_path: Path | str) -> RestoreResult:
        source = self.get(source_id)
        target = Path(target_path)
        self._validate_restore_target(target)
        if os.path.lexists(target):
            raise SourceConflictError("restore target already exists; overwrite is disabled")
        if not target.parent.is_dir():
            raise SourceValidationError("restore target parent directory does not exist")

        verification = self.verify(source.source_id)
        if not verification.verified:
            raise SourceIntegrityError("Managed Source failed verification and cannot be restored")

        fd, staging_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".staging",
            dir=str(target.parent),
        )
        os.close(fd)
        staging_path = Path(staging_name)
        published = False
        try:
            # mkstemp gives us a race-free name. _copy_stream creates its target
            # exclusively, so remove only this empty staging file before copying.
            staging_path.unlink()
            managed_path = self._managed_path(source)
            restored_hash, restored_size = _copy_stream(
                managed_path,
                staging_path,
                chunk_size=self.chunk_size,
            )
            if restored_hash != source.content_hash or restored_size != source.size_bytes:
                raise SourceIntegrityError("restored bytes failed hash or size verification")
            try:
                _publish_file_no_replace(staging_path, target)
            except FileExistsError as exc:
                raise SourceConflictError(
                    "restore target appeared during restore; overwrite is disabled"
                ) from exc
            published = True
            return RestoreResult(
                source_id=source.source_id,
                target_path=target,
                restored=True,
                content_hash=restored_hash,
                size_bytes=restored_size,
                verified=True,
            )
        except SourceError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise SourceError("Managed Source restore failed") from exc
        finally:
            if not published:
                try:
                    staging_path.unlink()
                except FileNotFoundError:
                    pass

    def _validate_external_file(self, source_path: Path) -> os.stat_result:
        try:
            stat = source_path.lstat()
        except OSError as exc:
            raise AdmissionError("external file does not exist or is not readable") from exc
        if source_path.is_symlink() or not source_path.is_file():
            raise SourceValidationError("external input must be a regular file, not a directory or symlink")
        if not os.access(source_path, os.R_OK):
            raise AdmissionError("external file is not readable")
        return stat

    @staticmethod
    def _same_input_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
        return (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and before.st_ino == after.st_ino
            and before.st_dev == after.st_dev
        )

    @staticmethod
    def _filename_hint(
        metadata: Mapping[str, object], source_path: Path
    ) -> str:
        value = metadata.get("filename_hint", source_path.name)
        if not isinstance(value, str) or not value.strip():
            raise SourceValidationError("filename_hint must be a non-empty string")
        filename_hint = value.strip()
        if (
            filename_hint in {".", ".."}
            or Path(filename_hint).name != filename_hint
        ):
            raise SourceValidationError("filename_hint must be a filename, not a path")
        return filename_hint

    def _media_type(self, metadata: Mapping[str, object], filename_hint: str) -> str:
        value = metadata.get("media_type")
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise SourceValidationError("media_type must be a non-empty string")
            return value.strip()
        guessed = self.media_type_resolver(filename_hint)
        if isinstance(guessed, tuple):
            guessed = guessed[0]
        return guessed or "application/octet-stream"

    @staticmethod
    def _ingested_from(
        metadata: Mapping[str, object], source_path: Path, observed_at: str
    ) -> IngestedFrom:
        raw = metadata.get("ingested_from")
        if isinstance(raw, IngestedFrom):
            return raw
        if isinstance(raw, Mapping):
            location = raw.get("location")
            timestamp = raw.get("observed_at", observed_at)
            if not isinstance(location, str) or not location.strip():
                raise SourceValidationError("ingested_from.location must be non-empty")
            if not isinstance(timestamp, str) or not timestamp.strip():
                raise SourceValidationError("ingested_from.observed_at must be non-empty")
            if not isinstance(raw.get("may_be_missing", True), bool):
                raise SourceValidationError("ingested_from.may_be_missing must be a boolean")
            return IngestedFrom(
                location=location.strip(),
                observed_at=_validate_timestamp(
                    timestamp, "ingested_from.observed_at"
                ),
                record_status=_validate_non_empty_string(
                    raw.get("record_status", "historical_hint"),
                    "ingested_from.record_status",
                ),
                may_be_missing=raw.get("may_be_missing", True),
            )
        try:
            location = source_path.absolute().as_uri()
        except (OSError, ValueError):
            location = str(source_path)
        return IngestedFrom(location=location, observed_at=observed_at)

    def _content_equivalents(self, content_hash: str) -> tuple[str, ...]:
        return tuple(
            source.source_id
            for source in self.list_sources()
            if source.content_hash == content_hash
        )

    @staticmethod
    def _write_manifest(path: Path, source: ManagedSource) -> None:
        payload = source.to_manifest_dict()
        round_tripped = LocalManagedSourceRepository._parse_manifest(
            json.loads(json.dumps(payload))
        )
        if round_tripped != source:
            raise ManifestError("Manifest round-trip does not preserve Managed Source")
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _managed_path(self, source: ManagedSource) -> Path:
        if not _is_lexically_safe_locator(source.managed_locator, source.source_id):
            raise ManifestError("managed_locator is outside the controlled Source root")
        managed_path = self.managed_root / source.managed_locator
        source_dir = self.sources_root / source.source_id
        try:
            source_dir_real = source_dir.resolve(strict=True)
            sources_root_real = self.sources_root.resolve(strict=True)
            managed_parent_real = managed_path.parent.resolve(strict=True)
            source_dir_real.relative_to(sources_root_real)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ManifestError("managed_locator is outside its Source directory") from exc
        if managed_parent_real != source_dir_real:
            raise ManifestError("managed_locator parent is not the controlled Source directory")
        if managed_path.is_symlink():
            raise ManifestError("managed_locator must not be a symlink")
        return managed_path

    def _ensure_controlled_layout(self, *, create: bool = False) -> None:
        if create:
            self.sources_root.mkdir(parents=True, exist_ok=True)
            self.staging_root.mkdir(parents=True, exist_ok=True)
        if not self.sources_root.exists():
            return
        if self.sources_root.is_symlink() or self.staging_root.is_symlink():
            raise SourceValidationError("Managed Source layout must not contain symlink roots")
        try:
            managed_root_real = self.managed_root.resolve(strict=True)
            sources_root_real = self.sources_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SourceValidationError("Managed Source layout cannot be resolved") from exc
        if sources_root_real != managed_root_real / SOURCE_DIR_NAME:
            raise SourceValidationError("Managed Source layout escaped its controlled root")
        if self.staging_root.exists():
            try:
                staging_root_real = self.staging_root.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise SourceValidationError(
                    "Managed Source staging root cannot be resolved"
                ) from exc
            if staging_root_real != managed_root_real / STAGING_DIR_NAME:
                raise SourceValidationError(
                    "Managed Source staging root escaped its controlled root"
                )

    def _validate_restore_target(self, target: Path) -> None:
        if target.name in {"", ".", ".."}:
            raise SourceValidationError("restore target must be a file path")
        try:
            target_parent_real = target.parent.resolve(strict=True)
            managed_root_real = self.managed_root.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SourceValidationError("restore target parent cannot be resolved") from exc
        resolved_target = target_parent_real / target.name
        try:
            resolved_target.relative_to(managed_root_real)
        except ValueError:
            return
        raise SourceValidationError(
            "restore target must be outside the Managed Source root"
        )

    def _read_manifest(self, path: Path) -> ManagedSource:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError("Managed Source Manifest could not be read") from exc
        source = self._parse_manifest(payload)
        if path.parent.name != source.source_id:
            raise ManifestError("Manifest source_id does not match its controlled directory")
        return source

    @staticmethod
    def _parse_manifest(payload: object) -> ManagedSource:
        if not isinstance(payload, dict):
            raise ManifestError("Manifest must be a JSON object")
        expected_top = {
            "schema_version",
            "source_id",
            "managed_source",
            "verification_records",
            "storage_replicas",
        }
        if "ingested_from" in payload:
            expected_top.add("ingested_from")
        if set(payload) != expected_top:
            raise ManifestError("Manifest fields do not match the strict v1 schema")
        if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ManifestError("Unsupported Managed Source Manifest schema")
        try:
            source_id = _validate_source_id(payload.get("source_id"))
        except SourceValidationError as exc:
            raise ManifestError("Manifest source_id is invalid") from exc

        managed_payload = payload.get("managed_source")
        if not isinstance(managed_payload, dict):
            raise ManifestError("managed_source must be an object")
        if set(managed_payload) != {
            "managed_locator",
            "content_hash",
            "size_bytes",
            "media_type",
            "filename_hint",
            "archived_at",
            "availability",
        }:
            raise ManifestError("managed_source fields do not match the strict v1 schema")
        managed_locator = managed_payload.get("managed_locator")
        if not _is_lexically_safe_locator(managed_locator, source_id):
            raise ManifestError("managed_locator is invalid")
        content_hash = _validate_hash(managed_payload.get("content_hash"))
        size_bytes = _validate_size(managed_payload.get("size_bytes"), "size_bytes")
        media_type = _validate_non_empty_string(managed_payload.get("media_type"), "media_type")
        filename_hint = _validate_non_empty_string(
            managed_payload.get("filename_hint"), "filename_hint"
        )
        if filename_hint in {".", ".."} or Path(filename_hint).name != filename_hint:
            raise ManifestError("filename_hint must be a basename")
        archived_at = _validate_timestamp(
            managed_payload.get("archived_at"), "archived_at"
        )
        availability = _validate_non_empty_string(
            managed_payload.get("availability"), "availability"
        )
        if availability not in {"available", "unavailable"}:
            raise ManifestError("availability is not supported")

        ingested_from = None
        if "ingested_from" in payload:
            raw_ingested = payload["ingested_from"]
            if not isinstance(raw_ingested, dict) or set(raw_ingested) != {
                "location",
                "observed_at",
                "record_status",
                "may_be_missing",
            }:
                raise ManifestError("ingested_from fields do not match the strict v1 schema")
            ingested_from = IngestedFrom(
                location=_validate_non_empty_string(raw_ingested.get("location"), "ingested_from.location"),
                observed_at=_validate_timestamp(raw_ingested.get("observed_at"), "ingested_from.observed_at"),
                record_status=_validate_non_empty_string(raw_ingested.get("record_status"), "ingested_from.record_status"),
                may_be_missing=_validate_bool(raw_ingested.get("may_be_missing"), "ingested_from.may_be_missing"),
            )

        verification_records = LocalManagedSourceRepository._parse_verification_records(
            payload.get("verification_records")
        )
        storage_replicas = LocalManagedSourceRepository._parse_storage_replicas(
            payload.get("storage_replicas")
        )
        return ManagedSource(
            source_id=source_id,
            content_hash=content_hash,
            size_bytes=size_bytes,
            media_type=media_type,
            filename_hint=filename_hint,
            managed_locator=managed_locator,
            archived_at=archived_at,
            availability=availability,
            ingested_from=ingested_from,
            verification_records=verification_records,
            storage_replicas=storage_replicas,
        )

    @staticmethod
    def _parse_verification_records(value: object) -> tuple[VerificationRecord, ...]:
        if not isinstance(value, list):
            raise ManifestError("verification_records must be an array")
        records: list[VerificationRecord] = []
        expected = {
            "verified_at",
            "observed_content_hash",
            "managed_content_hash",
            "observed_size_bytes",
            "managed_size_bytes",
            "hash_matches",
            "size_matches",
        }
        for index, raw in enumerate(value):
            if not isinstance(raw, dict) or set(raw) != expected:
                raise ManifestError(f"verification_records[{index}] is invalid")
            records.append(
                VerificationRecord(
                    verified_at=_validate_timestamp(raw.get("verified_at"), "verified_at"),
                    observed_content_hash=_validate_hash(raw.get("observed_content_hash"), "observed_content_hash"),
                    managed_content_hash=_validate_hash(raw.get("managed_content_hash"), "managed_content_hash"),
                    observed_size_bytes=_validate_size(raw.get("observed_size_bytes"), "observed_size_bytes"),
                    managed_size_bytes=_validate_size(raw.get("managed_size_bytes"), "managed_size_bytes"),
                    hash_matches=_validate_bool(raw.get("hash_matches"), "hash_matches"),
                    size_matches=_validate_bool(raw.get("size_matches"), "size_matches"),
                )
            )
        return tuple(records)

    @staticmethod
    def _parse_storage_replicas(value: object) -> tuple[StorageReplica, ...]:
        if not isinstance(value, list):
            raise ManifestError("storage_replicas must be an array")
        replicas: list[StorageReplica] = []
        expected = {"kind", "location", "status"}
        for index, raw in enumerate(value):
            if not isinstance(raw, dict) or set(raw) != expected:
                raise ManifestError(f"storage_replicas[{index}] is invalid")
            replicas.append(
                StorageReplica(
                    kind=_validate_non_empty_string(raw.get("kind"), "replica.kind"),
                    location=_validate_non_empty_string(raw.get("location"), "replica.location"),
                    status=_validate_non_empty_string(raw.get("status"), "replica.status"),
                )
            )
        return tuple(replicas)
