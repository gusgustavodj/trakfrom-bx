"""Platform-neutral contracts for read-only virtual playlist projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .volume_access import TrackOrigin, VolumeIdentity


class VirtualProjectionError(ValueError):
    """Base error for virtual playlist projection contracts."""


class InvalidVirtualPathError(VirtualProjectionError):
    """Raised when a virtual path contains an unsafe logical segment."""


class InvalidProjectionSnapshotError(VirtualProjectionError):
    """Raised when a snapshot contains inconsistent source references."""


class ProjectionAdapterError(VirtualProjectionError):
    """Base error for projection adapter lifecycle state."""


class SourceAlreadyPublishedError(ProjectionAdapterError):
    """Raised when an adapter receives a duplicate source publication."""


class SourceNotPublishedError(ProjectionAdapterError):
    """Raised when an adapter receives an operation for an unknown source."""


class ProjectionInvalidatedError(ProjectionAdapterError):
    """Raised when an adapter cannot refresh an invalidated projection."""


class UnsupportedProjectionCapabilityError(ProjectionAdapterError):
    """Raised when an adapter cannot provide a required projection capability."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class VirtualPath:
    """A deterministic logical path represented only by safe segments."""

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segments, tuple):
            raise InvalidVirtualPathError("virtual path segments must be a tuple")
        for segment in self.segments:
            if not isinstance(segment, str) or not segment:
                raise InvalidVirtualPathError("virtual path segments must be non-empty strings")
            if segment in {".", ".."}:
                raise InvalidVirtualPathError("virtual path segments must not traverse")
            if "\x00" in segment or "/" in segment or "\\" in segment:
                raise InvalidVirtualPathError("virtual path segments must not contain separators")
            if segment.startswith("/") or segment.startswith("\\") or ":" in segment:
                raise InvalidVirtualPathError("virtual path segments must not be absolute")

    @property
    def value(self) -> str:
        """Return the deterministic, separator-neutral logical representation."""
        return "/".join(self.segments)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class VirtualSource:
    """Stable read-only source metadata for one projected USB."""

    source_id: str
    volume_identity: VolumeIdentity
    display_name: str
    snapshot_id: str
    read_only: bool

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.display_name, "display_name")
        _require_text(self.snapshot_id, "snapshot_id")
        if not isinstance(self.volume_identity, VolumeIdentity):
            raise TypeError("volume_identity must be a VolumeIdentity")
        if self.read_only is not True:
            raise InvalidProjectionSnapshotError("virtual sources must be read-only")


@dataclass(frozen=True, slots=True)
class VirtualPlaylist:
    """One published playlist folder without an implied track order."""

    playlist_id: str
    source_id: str
    path: VirtualPath
    display_name: str
    selectable: bool
    track_count: int
    parent_playlist_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.playlist_id, "playlist_id")
        _require_text(self.source_id, "source_id")
        _require_text(self.display_name, "display_name")
        if not isinstance(self.path, VirtualPath):
            raise TypeError("path must be a VirtualPath")
        if not isinstance(self.selectable, bool):
            raise TypeError("selectable must be a bool")
        if not isinstance(self.track_count, int) or isinstance(self.track_count, bool) or self.track_count < 0:
            raise ValueError("track_count must be a non-negative integer")
        if self.parent_playlist_id is not None:
            _require_text(self.parent_playlist_id, "parent_playlist_id")


@dataclass(frozen=True, slots=True)
class VirtualTrack:
    """One read-only published media entry backed by a logical track origin."""

    track_id: str
    source_id: str
    playlist_id: str
    path: VirtualPath
    origin: TrackOrigin
    display_name: str
    size_bytes: int | None
    read_only: bool

    def __post_init__(self) -> None:
        _require_text(self.track_id, "track_id")
        _require_text(self.source_id, "source_id")
        _require_text(self.playlist_id, "playlist_id")
        _require_text(self.display_name, "display_name")
        if not isinstance(self.path, VirtualPath):
            raise TypeError("path must be a VirtualPath")
        if not isinstance(self.origin, TrackOrigin):
            raise TypeError("origin must be a TrackOrigin")
        if self.size_bytes is not None and (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer or None")
        if self.read_only is not True:
            raise InvalidProjectionSnapshotError("virtual tracks must be read-only")


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    """Immutable coherent tree for one read-only virtual source revision."""

    source: VirtualSource
    playlists: tuple[VirtualPlaylist, ...]
    tracks: tuple[VirtualTrack, ...]
    snapshot_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, VirtualSource):
            raise TypeError("source must be a VirtualSource")
        _require_text(self.snapshot_id, "snapshot_id")
        if self.snapshot_id != self.source.snapshot_id:
            raise InvalidProjectionSnapshotError("snapshot_id must match source snapshot_id")
        if not isinstance(self.playlists, tuple) or not isinstance(self.tracks, tuple):
            raise TypeError("playlists and tracks must be tuples")

        playlist_ids: set[str] = set()
        for playlist in self.playlists:
            if not isinstance(playlist, VirtualPlaylist):
                raise TypeError("playlists must contain VirtualPlaylist values")
            if playlist.source_id != self.source.source_id:
                raise InvalidProjectionSnapshotError("playlist source_id does not match source")
            if playlist.playlist_id in playlist_ids:
                raise InvalidProjectionSnapshotError("playlist_id must be unique within a snapshot")
            playlist_ids.add(playlist.playlist_id)

        for playlist in self.playlists:
            if playlist.parent_playlist_id is not None and playlist.parent_playlist_id not in playlist_ids:
                raise InvalidProjectionSnapshotError("playlist parent must exist in the snapshot")

        published_track_counts = dict.fromkeys(playlist_ids, 0)
        for track in self.tracks:
            if not isinstance(track, VirtualTrack):
                raise TypeError("tracks must contain VirtualTrack values")
            if track.source_id != self.source.source_id:
                raise InvalidProjectionSnapshotError("track source_id does not match source")
            if track.playlist_id not in playlist_ids:
                raise InvalidProjectionSnapshotError("track playlist_id must exist in the snapshot")
            if track.origin.volume_identity != self.source.volume_identity:
                raise InvalidProjectionSnapshotError("track origin volume_identity does not match source")
            published_track_counts[track.playlist_id] += 1

        for playlist in self.playlists:
            if playlist.track_count != published_track_counts[playlist.playlist_id]:
                raise InvalidProjectionSnapshotError(
                    "playlist track_count must match published tracks"
                )


@dataclass(frozen=True, slots=True)
class ProjectionCapabilities:
    """Explicit adapter capabilities with no optimistic defaults."""

    read_only: bool
    random_access: bool
    dynamic_refresh: bool
    invalidation: bool
    stable_mount_name: bool
    multiple_sources: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("read_only", self.read_only),
            ("random_access", self.random_access),
            ("dynamic_refresh", self.dynamic_refresh),
            ("invalidation", self.invalidation),
            ("stable_mount_name", self.stable_mount_name),
            ("multiple_sources", self.multiple_sources),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")


@runtime_checkable
class VirtualProjectionAdapter(Protocol):
    """Platform adapter lifecycle over neutral projection snapshots."""

    def publish(self, snapshot: ProjectionSnapshot) -> None:
        """Publish one source snapshot."""

    def refresh(self, snapshot: ProjectionSnapshot) -> None:
        """Refresh one already published, non-invalidated source snapshot."""

    def invalidate(self, source_id: str) -> None:
        """Invalidate one published source without redirecting its paths."""

    def unpublish(self, source_id: str) -> None:
        """Remove one published source projection."""

    def capabilities(self) -> ProjectionCapabilities:
        """Return only the capabilities explicitly provided by this adapter."""
