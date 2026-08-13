"""Managed Source identity validation shared by storage and Processing."""

from __future__ import annotations

import re


SOURCE_ID_PATTERN = re.compile(r"^src_[0-9a-f]{32}$")


def require_managed_source_id(value: object, *, field: str = "source_id") -> str:
    """Return one current Managed Source ID or fail before any path use."""

    if not isinstance(value, str) or not SOURCE_ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field} must be src_ followed by 32 lowercase hex characters"
        )
    return value
