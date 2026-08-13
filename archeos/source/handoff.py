"""Explicit file-level Handoff Markers for verified local Managed Sources."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from ..filesystem import publish_file_no_replace
from .contracts import ManagedSourceRepository
from .identity import require_managed_source_id
from .local_repository import SourceError


MARKER_SCHEMA_VERSION = "1.0"


class HandoffMarkerError(SourceError):
    """A Handoff Marker could not be safely written or read."""


class HandoffMarkerConflictError(HandoffMarkerError):
    """A marker target is already owned or contains user content."""


@dataclass(frozen=True)
class HandoffMarker:
    source_id: str
    marker_path: Path
    written_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "marker_path": str(self.marker_path),
            "written_at": self.written_at,
            "schema_version": MARKER_SCHEMA_VERSION,
        }


@dataclass(frozen=True)
class HandoffWriteResult:
    marker: HandoffMarker
    status: str

    def to_dict(self) -> dict[str, object]:
        return {"marker": self.marker.to_dict(), "status": self.status}


@dataclass(frozen=True)
class HandoffShowResult:
    marker: HandoffMarker
    source_exists: bool
    source_verified: bool | None

    def to_dict(self) -> dict[str, object]:
        return {
            "marker": self.marker.to_dict(),
            "source_exists": self.source_exists,
            "source_verified": self.source_verified,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validate_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffMarkerError("marker written_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise HandoffMarkerError("marker written_at must include a timezone")
    return value


def _render_marker(source_id: str, written_at: str) -> str:
    return (
        "---\n"
        f'archeos_handoff_marker: "{MARKER_SCHEMA_VERSION}"\n'
        f"source_id: {source_id}\n"
        f"written_at: {written_at}\n"
        "---\n\n"
        "# 向阳经营系统归档说明\n\n"
        "此文件已经归档到向阳经营系统。\n"
        "请不要在此位置继续修改。\n\n"
        "如需查看或更新，请使用：\n"
        f"Source ID：{source_id}\n"
        f"命令：python -m archeos source show {source_id}\n\n"
        "外部文件的修改不会自动同步。\n"
        "此处旧文件可由用户自行保留或删除。\n"
    )


def _parse_marker(marker_path: Path) -> HandoffMarker:
    _validate_marker_for_read(marker_path)
    try:
        content = marker_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HandoffMarkerError("Handoff Marker could not be read") from exc
    lines = content.splitlines()
    if len(lines) < 6 or lines[0] != "---" or lines[4] != "---":
        raise HandoffMarkerError("marker is not a supported ArcheOS Handoff Marker")
    expected_keys = (
        "archeos_handoff_marker",
        "source_id",
        "written_at",
    )
    values: dict[str, str] = {}
    for expected_key, line in zip(expected_keys, lines[1:4], strict=True):
        key, separator, value = line.partition(": ")
        if key != expected_key or not separator or not value:
            raise HandoffMarkerError("marker schema is invalid")
        values[key] = value
    if values["archeos_handoff_marker"] != f'"{MARKER_SCHEMA_VERSION}"':
        raise HandoffMarkerError("marker schema version is unsupported")
    try:
        source_id = require_managed_source_id(values["source_id"])
    except ValueError as exc:
        raise HandoffMarkerError("marker source_id is invalid") from exc
    written_at = _validate_timestamp(values["written_at"])
    if content != _render_marker(source_id, written_at):
        raise HandoffMarkerError("marker content is not a supported ArcheOS Handoff Marker")
    return HandoffMarker(source_id=source_id, marker_path=marker_path, written_at=written_at)


def _location_to_local_path(location: str) -> Path:
    parsed = urlparse(location)
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise HandoffMarkerError("ingested_from is not a local file location")
        return Path(unquote(parsed.path))
    if parsed.scheme:
        raise HandoffMarkerError("ingested_from is not a local file location")
    return Path(location)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _validate_no_symlink_parents(path: Path) -> None:
    """Reject a supplied path whose parent chain would redirect marker I/O."""

    absolute_path = path.absolute()
    parent = absolute_path.parent
    while parent != Path(parent.anchor):
        try:
            if parent.is_symlink():
                raise HandoffMarkerError("marker path must not contain symlink parent directories")
        except OSError as exc:
            raise HandoffMarkerError("marker path parent could not be safely inspected") from exc
        parent = parent.parent


def _validate_marker_for_read(marker_path: Path) -> None:
    _validate_no_symlink_parents(marker_path)
    try:
        marker_path.lstat()
    except OSError as exc:
        raise HandoffMarkerError("Handoff Marker could not be read") from exc
    if marker_path.is_symlink() or not marker_path.is_file():
        raise HandoffMarkerError("Handoff Marker must be a regular file, not a symlink")


class HandoffMarkerService:
    """Write and inspect explicit external-directory Handoff Markers."""

    def __init__(
        self,
        repository: ManagedSourceRepository,
        managed_root: Path | str,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.repository = repository
        self.managed_root = Path(managed_root)
        self.clock = clock

    def write(
        self, source_id: str, *, target_file: Path | str | None = None
    ) -> HandoffWriteResult:
        try:
            source = self.repository.get(source_id)
            verification = self.repository.verify(source.source_id)
        except SourceError as exc:
            raise HandoffMarkerError(
                "Managed Source was not found or could not be verified"
            ) from exc
        if not verification.verified:
            raise HandoffMarkerError("Managed Source verification failed; marker was not written")
        target = self._target_file(source.ingested_from.location if source.ingested_from else None, target_file)
        marker_path = target.with_name(f"{target.name}.archeos.md")
        if _is_under(marker_path, self.managed_root):
            raise HandoffMarkerError("marker target must be outside the Managed Source root")
        self._validate_target(target, marker_path)
        if os.path.lexists(marker_path):
            return self._existing(marker_path, source.source_id)

        written_at = self.clock()
        _validate_timestamp(written_at)
        staging_path = self._stage(marker_path, _render_marker(source.source_id, written_at))
        try:
            try:
                publish_file_no_replace(staging_path, marker_path)
            except FileExistsError:
                return self._existing(marker_path, source.source_id)
            return HandoffWriteResult(
                marker=HandoffMarker(source.source_id, marker_path, written_at),
                status="written",
            )
        except HandoffMarkerError:
            raise
        except OSError as exc:
            raise HandoffMarkerError("Handoff Marker could not be published") from exc
        finally:
            try:
                staging_path.unlink()
            except FileNotFoundError:
                pass

    def show(self, marker_path: Path | str) -> HandoffShowResult:
        marker = _parse_marker(Path(marker_path))
        try:
            self.repository.get(marker.source_id)
        except SourceError:
            return HandoffShowResult(marker, source_exists=False, source_verified=None)
        verification = self.repository.verify(marker.source_id)
        return HandoffShowResult(
            marker,
            source_exists=True,
            source_verified=verification.verified,
        )

    def _target_file(self, historical_location: str | None, target_file: Path | str | None) -> Path:
        if target_file is not None:
            return Path(target_file)
        if historical_location is None:
            raise HandoffMarkerError("ingested_from is unavailable; provide --target-file")
        return _location_to_local_path(historical_location)

    @staticmethod
    def _validate_target(target: Path, marker_path: Path) -> None:
        _validate_no_symlink_parents(target)
        _validate_no_symlink_parents(marker_path)
        try:
            target.lstat()
        except OSError as exc:
            raise HandoffMarkerError("external target file does not exist") from exc
        if target.is_symlink() or not target.is_file():
            raise HandoffMarkerError("external target must be a regular file")
        if not marker_path.parent.is_dir():
            raise HandoffMarkerError("marker target directory does not exist")
        if not os.access(marker_path.parent, os.W_OK):
            raise HandoffMarkerError("marker target directory is not writable")

    @staticmethod
    def _stage(marker_path: Path, content: str) -> Path:
        try:
            descriptor, filename = tempfile.mkstemp(
                prefix=f".{marker_path.name}.",
                suffix=".staging",
                dir=str(marker_path.parent),
                text=True,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return Path(filename)
        except OSError as exc:
            raise HandoffMarkerError("Handoff Marker staging failed") from exc

    @staticmethod
    def _existing(marker_path: Path, source_id: str) -> HandoffWriteResult:
        if marker_path.is_symlink() or not marker_path.is_file():
            raise HandoffMarkerConflictError("marker target already exists and will not be replaced")
        marker = _parse_marker(marker_path)
        if marker.source_id != source_id:
            raise HandoffMarkerConflictError("marker already refers to a different Source")
        return HandoffWriteResult(marker=marker, status="existing")
