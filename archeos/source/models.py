"""Immutable read models for the local Managed Source runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MANIFEST_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class IngestedFrom:
    """A historical hint about where a Source was admitted from."""

    location: str
    observed_at: str
    record_status: str = "historical_hint"
    may_be_missing: bool = True

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]

    def get(self, key: str, default: object = None) -> object:
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict[str, object]:
        return {
            "location": self.location,
            "observed_at": self.observed_at,
            "record_status": self.record_status,
            "may_be_missing": self.may_be_missing,
        }


@dataclass(frozen=True)
class VerificationRecord:
    verified_at: str
    observed_content_hash: str
    managed_content_hash: str
    observed_size_bytes: int
    managed_size_bytes: int
    hash_matches: bool
    size_matches: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "verified_at": self.verified_at,
            "observed_content_hash": self.observed_content_hash,
            "managed_content_hash": self.managed_content_hash,
            "observed_size_bytes": self.observed_size_bytes,
            "managed_size_bytes": self.managed_size_bytes,
            "hash_matches": self.hash_matches,
            "size_matches": self.size_matches,
        }


@dataclass(frozen=True)
class StorageReplica:
    kind: str
    location: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "location": self.location,
            "status": self.status,
        }


@dataclass(frozen=True)
class ManagedSource:
    """The immutable read model exposed by the repository contract."""

    source_id: str
    content_hash: str
    size_bytes: int
    media_type: str
    filename_hint: str
    managed_locator: str
    archived_at: str
    availability: str
    ingested_from: IngestedFrom | None = None
    verification_records: tuple[VerificationRecord, ...] = ()
    storage_replicas: tuple[StorageReplica, ...] = ()
    # This is an admission response hint and is intentionally not persisted.
    content_equivalent_source_ids: tuple[str, ...] = ()

    @property
    def source(self) -> ManagedSource:
        """Compatibility convenience for callers expecting an admission result."""

        return self

    def to_manifest_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "source_id": self.source_id,
            "managed_source": {
                "managed_locator": self.managed_locator,
                "content_hash": self.content_hash,
                "size_bytes": self.size_bytes,
                "media_type": self.media_type,
                "filename_hint": self.filename_hint,
                "archived_at": self.archived_at,
                "availability": self.availability,
            },
            "verification_records": [
                record.to_dict() for record in self.verification_records
            ],
            "storage_replicas": [
                replica.to_dict() for replica in self.storage_replicas
            ],
        }
        if self.ingested_from is not None:
            payload["ingested_from"] = self.ingested_from.to_dict()
        return payload

    def to_dict(self) -> dict[str, object]:
        payload = self.to_manifest_dict()
        payload["content_equivalent_source_ids"] = list(
            self.content_equivalent_source_ids
        )
        return payload


@dataclass(frozen=True)
class VerificationResult:
    source_id: str
    verified: bool
    expected_content_hash: str | None
    observed_content_hash: str | None
    expected_size_bytes: int | None
    observed_size_bytes: int | None
    checked_at: str
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.verified

    @property
    def success(self) -> bool:
        return self.verified

    @property
    def hash_matches(self) -> bool:
        return (
            self.expected_content_hash is not None
            and self.expected_content_hash == self.observed_content_hash
        )

    @property
    def size_matches(self) -> bool:
        return (
            self.expected_size_bytes is not None
            and self.expected_size_bytes == self.observed_size_bytes
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "verified": self.verified,
            "expected_content_hash": self.expected_content_hash,
            "observed_content_hash": self.observed_content_hash,
            "expected_size_bytes": self.expected_size_bytes,
            "observed_size_bytes": self.observed_size_bytes,
            "checked_at": self.checked_at,
            "reason": self.reason,
            "hash_matches": self.hash_matches,
            "size_matches": self.size_matches,
        }


@dataclass(frozen=True)
class RestoreResult:
    source_id: str
    target_path: object
    restored: bool
    content_hash: str
    size_bytes: int
    verified: bool

    @property
    def success(self) -> bool:
        return self.restored and self.verified

    def to_dict(self) -> dict[str, Any]:
        target = self.target_path
        return {
            "source_id": self.source_id,
            "target_path": str(target),
            "restored": self.restored,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "verified": self.verified,
        }
