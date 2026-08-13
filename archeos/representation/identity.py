"""Deterministic identity helpers for Normalized Representations."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from ..source.identity import require_managed_source_id


REPRESENTATION_ID_PATTERN = re.compile(r"^repr_[0-9a-f]{64}$")
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def require_representation_id(value: object, *, field: str = "representation_id") -> str:
    if not isinstance(value, str) or not REPRESENTATION_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be repr_ followed by 64 lowercase hex characters")
    return value


def require_content_hash(value: object, *, field: str = "content_hash") -> str:
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a full sha256 hash")
    return value


def canonical_configuration_fingerprint(configuration: Mapping[str, object]) -> str:
    """Hash a canonical JSON configuration without paths or runtime timestamps."""
    if not isinstance(configuration, Mapping):
        raise ValueError("configuration must be a mapping")
    try:
        canonical = json.dumps(
            configuration,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("configuration must be canonical JSON data") from exc
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def representation_id(
    *,
    source_id: object,
    source_content_hash: object,
    kind: object,
    adapter_name: object,
    adapter_version: object,
    configuration_fingerprint: object,
) -> str:
    """Return an opaque, deterministic representation identity."""
    source_id = require_managed_source_id(source_id)
    source_content_hash = require_content_hash(source_content_hash, field="source_content_hash")
    configuration_fingerprint = require_content_hash(
        configuration_fingerprint, field="configuration_fingerprint"
    )
    fields = (kind, adapter_name, adapter_version)
    if any(not isinstance(field, str) or not field.strip() for field in fields):
        raise ValueError("kind, adapter_name, and adapter_version must be non-empty strings")
    payload = "\n".join(
        (
            source_id,
            source_content_hash,
            str(kind).strip(),
            str(adapter_name).strip(),
            str(adapter_version).strip(),
            configuration_fingerprint,
        )
    )
    return f"repr_{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
