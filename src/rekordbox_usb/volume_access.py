"""Pure contracts for volume-backed track origin and delivery access."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path, PurePosixPath, PureWindowsPath


def _require_non_empty_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class VolumeIdentity:
    """Opaque, stable logical identity for a source volume."""

    value: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.value, "volume identity")


@dataclass(frozen=True, slots=True)
class VirtualVolumePath:
    """Canonical logical path relative to a volume root, without filesystem access."""

    value: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.value, "virtual volume path")
        if "\x00" in self.value:
            raise ValueError("virtual volume path must not contain a NUL byte")

        normalized = self.value.replace("\\", "/")
        windows_path = PureWindowsPath(self.value)
        if normalized.startswith("/") or windows_path.drive or windows_path.root:
            raise ValueError("virtual volume path must be relative to the volume root")

        canonical = PurePosixPath(normalized)
        if canonical == PurePosixPath(".") or ".." in canonical.parts:
            raise ValueError("virtual volume path must not contain parent traversal")

        object.__setattr__(self, "value", canonical.as_posix())


@dataclass(frozen=True, slots=True)
class TrackOrigin:
    """Logical source reference that remains independent from local delivery paths."""

    provider_id: str
    volume_identity: VolumeIdentity
    virtual_path: VirtualVolumePath
    native_library_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_text(self.provider_id, "provider_id")
        if not isinstance(self.volume_identity, VolumeIdentity):
            raise TypeError("volume_identity must be a VolumeIdentity")
        if not isinstance(self.virtual_path, VirtualVolumePath):
            raise TypeError("virtual_path must be a VirtualVolumePath")
        if self.native_library_id is not None:
            _require_non_empty_text(self.native_library_id, "native_library_id")


class TrackAccessCapability(StrEnum):
    """Explicit strategies for delivering a logical track origin to Traktor."""

    DIRECT_PATH = "direct_path"
    REQUIRES_MATERIALIZATION = "requires_materialization"


class MountedVolumeResolutionReason(StrEnum):
    """Stable reason codes for mounted-volume direct-path resolution failures."""

    VOLUME_IDENTITY_MISMATCH = "volume_identity_mismatch"
    MOUNTED_ROOT_INVALID = "mounted_root_invalid"
    PATH_OUTSIDE_MOUNTED_ROOT = "path_outside_mounted_root"
    TRACK_NOT_FOUND = "track_not_found"
    TRACK_NOT_REGULAR = "track_not_regular"
    TRACK_NOT_READABLE = "track_not_readable"


class MountedVolumeResolutionError(ValueError):
    """Raised when a mounted volume cannot provide a direct track path safely."""

    reason: MountedVolumeResolutionReason

    def __init__(self, reason: MountedVolumeResolutionReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class TrackOriginAccess:
    """Access capability and optional direct local path for a track origin."""

    origin: TrackOrigin
    capability: TrackAccessCapability
    direct_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.origin, TrackOrigin):
            raise TypeError("origin must be a TrackOrigin")
        if not isinstance(self.capability, TrackAccessCapability):
            raise TypeError("capability must be a TrackAccessCapability")
        if self.direct_path is not None and not isinstance(self.direct_path, Path):
            raise TypeError("direct_path must be a Path or None")
        if self.capability is TrackAccessCapability.DIRECT_PATH and self.direct_path is None:
            raise ValueError("DIRECT_PATH requires a direct_path")
        if (
            self.capability is TrackAccessCapability.REQUIRES_MATERIALIZATION
            and self.direct_path is not None
        ):
            raise ValueError("REQUIRES_MATERIALIZATION must not include a direct_path")


@dataclass(frozen=True, slots=True)
class ResolvedTrackLocation:
    """Local delivery path selected explicitly without replacing track origin."""

    origin: TrackOrigin
    capability: TrackAccessCapability
    local_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.origin, TrackOrigin):
            raise TypeError("origin must be a TrackOrigin")
        if not isinstance(self.capability, TrackAccessCapability):
            raise TypeError("capability must be a TrackAccessCapability")
        if not isinstance(self.local_path, Path):
            raise TypeError("local_path must be a Path")

    @property
    def is_materialized(self) -> bool:
        """Return whether this local delivery path is materialized."""
        return self.capability is TrackAccessCapability.REQUIRES_MATERIALIZATION


def resolve_mounted_track_origin(
    origin: TrackOrigin,
    mounted_volume_identity: VolumeIdentity,
    mounted_root: Path,
) -> ResolvedTrackLocation:
    """Resolve one logical origin to a readable direct path below a mounted root."""
    if not isinstance(origin, TrackOrigin):
        raise TypeError("origin must be a TrackOrigin")
    if not isinstance(mounted_volume_identity, VolumeIdentity):
        raise TypeError("mounted_volume_identity must be a VolumeIdentity")
    if not isinstance(mounted_root, Path):
        raise TypeError("mounted_root must be a Path")
    if mounted_volume_identity != origin.volume_identity:
        raise MountedVolumeResolutionError(
            MountedVolumeResolutionReason.VOLUME_IDENTITY_MISMATCH
        )
    if not mounted_root.is_absolute() or not mounted_root.exists() or not mounted_root.is_dir():
        raise MountedVolumeResolutionError(MountedVolumeResolutionReason.MOUNTED_ROOT_INVALID)
    if not os.access(mounted_root, os.R_OK):
        raise MountedVolumeResolutionError(MountedVolumeResolutionReason.MOUNTED_ROOT_INVALID)

    resolved_root = mounted_root.resolve(strict=True)
    candidate = resolved_root.joinpath(*origin.virtual_path.value.split("/"))
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise MountedVolumeResolutionError(
            MountedVolumeResolutionReason.PATH_OUTSIDE_MOUNTED_ROOT
        ) from exc

    if not resolved_candidate.exists():
        raise MountedVolumeResolutionError(MountedVolumeResolutionReason.TRACK_NOT_FOUND)
    if not resolved_candidate.is_file():
        raise MountedVolumeResolutionError(MountedVolumeResolutionReason.TRACK_NOT_REGULAR)
    if not os.access(resolved_candidate, os.R_OK):
        raise MountedVolumeResolutionError(MountedVolumeResolutionReason.TRACK_NOT_READABLE)

    return ResolvedTrackLocation(
        origin=origin,
        capability=TrackAccessCapability.DIRECT_PATH,
        local_path=resolved_candidate,
    )
