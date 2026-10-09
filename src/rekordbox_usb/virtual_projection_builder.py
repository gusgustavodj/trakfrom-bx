"""Pure, platform-neutral construction of virtual playlist projections."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .virtual_projection import (
    ProjectionSnapshot,
    VirtualPath,
    VirtualPlaylist,
    VirtualSource,
    VirtualTrack,
)
from .volume_access import TrackOrigin, VolumeIdentity


class ProjectionBuildError(ValueError):
    """Base error for incoherent projection-builder input."""


class InvalidProjectionBuildInputError(ProjectionBuildError):
    """Raised when source, playlist, or track input is incoherent."""


class UnknownProjectionPlaylistError(ProjectionBuildError):
    """Raised when a track or parent refers to a missing playlist."""


class MissingTrackOriginError(ProjectionBuildError):
    """Raised when a projected track does not provide a logical origin."""


class UnresolvableVirtualPathCollisionError(ProjectionBuildError):
    """Raised when stable input cannot produce unique virtual paths."""


class InvalidProjectionHierarchyError(ProjectionBuildError):
    """Raised when playlist parent relationships are cyclic or invalid."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidProjectionBuildInputError(f"{field_name} must be a non-empty string")
    return value


def _stable_segment(display_name: str, stable_id: str, *, fallback: str) -> str:
    """Return a safe logical segment without mutating the display name."""
    _require_text(display_name, "display_name")
    _require_text(stable_id, "stable_id")
    sanitized = display_name.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    if sanitized in {"", ".", ".."}:
        sanitized = fallback
    digest = sha256(stable_id.encode("utf-8")).hexdigest()[:12]
    return f"{sanitized}--{digest}"


@dataclass(frozen=True, slots=True)
class ProjectionPlaylistInput:
    """One already-listed folder or playlist, identified by a stable core id."""

    playlist_id: str
    source_id: str
    display_name: str
    is_folder: bool
    track_count: int
    parent_playlist_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionTrackInput:
    """One already-resolved track occurrence in one playlist."""

    track_id: str
    source_id: str
    playlist_id: str
    origin: TrackOrigin | None
    display_name: str
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectionBuildInput:
    """Read-only core data required to build exactly one source snapshot."""

    volume_identity: VolumeIdentity
    source_id: str
    display_name: str
    snapshot_id: str
    playlists: tuple[ProjectionPlaylistInput, ...]
    tracks: tuple[ProjectionTrackInput, ...]
    read_only: bool = True


def build_projection_snapshot(build_input: ProjectionBuildInput) -> ProjectionSnapshot:
    """Build a deterministic snapshot without filesystem or media access."""
    if not isinstance(build_input, ProjectionBuildInput):
        raise TypeError("build_input must be a ProjectionBuildInput")
    if not isinstance(build_input.volume_identity, VolumeIdentity):
        raise InvalidProjectionBuildInputError("volume_identity must be a VolumeIdentity")
    source_id = _require_text(build_input.source_id, "source_id")
    display_name = _require_text(build_input.display_name, "display_name")
    snapshot_id = _require_text(build_input.snapshot_id, "snapshot_id")
    if build_input.read_only is not True:
        raise InvalidProjectionBuildInputError("projection sources must be read-only")
    if not isinstance(build_input.playlists, tuple) or not isinstance(build_input.tracks, tuple):
        raise InvalidProjectionBuildInputError("playlists and tracks must be tuples")

    nodes_by_id: dict[str, ProjectionPlaylistInput] = {}
    for node in build_input.playlists:
        if not isinstance(node, ProjectionPlaylistInput):
            raise InvalidProjectionBuildInputError("playlists must contain ProjectionPlaylistInput")
        _require_text(node.playlist_id, "playlist_id")
        _require_text(node.display_name, "playlist display_name")
        if node.source_id != source_id:
            raise InvalidProjectionBuildInputError("playlist source_id does not match source")
        if not isinstance(node.is_folder, bool):
            raise InvalidProjectionBuildInputError("is_folder must be a bool")
        if not isinstance(node.track_count, int) or isinstance(node.track_count, bool) or node.track_count < 0:
            raise InvalidProjectionBuildInputError("track_count must be a non-negative integer")
        if node.parent_playlist_id is not None:
            _require_text(node.parent_playlist_id, "parent_playlist_id")
        if node.playlist_id in nodes_by_id:
            raise InvalidProjectionBuildInputError("playlist_id must be unique")
        nodes_by_id[node.playlist_id] = node

    def path_for(playlist_id: str, visiting: set[str]) -> VirtualPath:
        node = nodes_by_id[playlist_id]
        if playlist_id in visiting:
            raise InvalidProjectionHierarchyError("playlist hierarchy must not contain a cycle")
        segment = _stable_segment(node.display_name, node.playlist_id, fallback="playlist")
        if node.parent_playlist_id is None:
            return VirtualPath(("playlists", segment))
        if node.parent_playlist_id not in nodes_by_id:
            raise InvalidProjectionHierarchyError("playlist parent does not exist")
        return VirtualPath(
            (*path_for(node.parent_playlist_id, {*visiting, playlist_id}).segments, segment)
        )

    playlist_paths = {playlist_id: path_for(playlist_id, set()) for playlist_id in nodes_by_id}
    if len(set(playlist_paths.values())) != len(playlist_paths):
        raise UnresolvableVirtualPathCollisionError("playlist paths must be unique")

    virtual_playlists = tuple(
        VirtualPlaylist(
            playlist_id=node.playlist_id,
            source_id=source_id,
            path=playlist_paths[node.playlist_id],
            display_name=node.display_name,
            selectable=not node.is_folder,
            track_count=node.track_count,
            parent_playlist_id=node.parent_playlist_id,
        )
        for node in build_input.playlists
    )

    published_counts = dict.fromkeys(nodes_by_id, 0)
    virtual_tracks: list[VirtualTrack] = []
    track_paths: set[VirtualPath] = set()
    for track in build_input.tracks:
        if not isinstance(track, ProjectionTrackInput):
            raise InvalidProjectionBuildInputError("tracks must contain ProjectionTrackInput")
        _require_text(track.track_id, "track_id")
        _require_text(track.display_name, "track display_name")
        if track.source_id != source_id:
            raise InvalidProjectionBuildInputError("track source_id does not match source")
        node = nodes_by_id.get(track.playlist_id)
        if node is None:
            raise UnknownProjectionPlaylistError("track playlist_id does not exist")
        if node.is_folder:
            raise InvalidProjectionBuildInputError("tracks cannot belong to folder nodes")
        if track.origin is None:
            raise MissingTrackOriginError("track origin is required")
        if not isinstance(track.origin, TrackOrigin):
            raise InvalidProjectionBuildInputError("track origin must be a TrackOrigin")
        if track.origin.volume_identity != build_input.volume_identity:
            raise InvalidProjectionBuildInputError("track origin volume_identity does not match source")
        path = VirtualPath(
            (*playlist_paths[track.playlist_id].segments, _stable_segment(
                track.display_name,
                f"{track.playlist_id}:{track.track_id}",
                fallback="track",
            ))
        )
        if path in track_paths:
            raise UnresolvableVirtualPathCollisionError("track paths must be unique")
        track_paths.add(path)
        published_counts[track.playlist_id] += 1
        virtual_tracks.append(
            VirtualTrack(
                track_id=track.track_id,
                source_id=source_id,
                playlist_id=track.playlist_id,
                path=path,
                origin=track.origin,
                display_name=track.display_name,
                size_bytes=track.size_bytes,
                read_only=True,
            )
        )

    for node in build_input.playlists:
        if node.track_count != published_counts[node.playlist_id]:
            raise InvalidProjectionBuildInputError("playlist track_count does not match track input")

    return ProjectionSnapshot(
        source=VirtualSource(
            source_id=source_id,
            volume_identity=build_input.volume_identity,
            display_name=display_name,
            snapshot_id=snapshot_id,
            read_only=True,
        ),
        playlists=virtual_playlists,
        tracks=tuple(virtual_tracks),
        snapshot_id=snapshot_id,
    )
