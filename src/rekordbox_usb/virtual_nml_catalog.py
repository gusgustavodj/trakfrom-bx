"""Pure, deterministic NML catalog rendering for virtual playlist snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Protocol, runtime_checkable

from .library_models import LibraryPlaylist, LibraryTrack
from .nml_writer import render_playlist_nml_content
from .virtual_projection import ProjectionSnapshot, VirtualPath, VirtualTrack
from .volume_access import TrackOrigin


class NmlCatalogError(ValueError):
    """Base error for virtual NML catalog rendering."""


class NmlCatalogPathResolutionError(NmlCatalogError):
    """Raised when a source origin has no compatible NML location."""


class NmlCatalogVolumeMismatchError(NmlCatalogError):
    """Raised when a track origin does not belong to the catalog source."""


class NmlCatalogIdentityError(NmlCatalogError):
    """Raised when playlist identity cannot produce a catalog entry."""


class NmlCatalogNameCollisionError(NmlCatalogError):
    """Raised when stable input cannot produce a unique catalog path."""


class NmlCatalogRenderError(NmlCatalogError):
    """Raised when the shared NML serializer rejects catalog data."""


@runtime_checkable
class TrackOriginNmlPathResolver(Protocol):
    """Resolve one logical track origin to its already-materialized NML path."""

    def resolve(self, origin: TrackOrigin) -> str:
        """Return the absolute path to serialize for this origin."""


@dataclass(frozen=True, slots=True)
class NmlTrackMetadata:
    """Optional already-known metadata supported by the existing NML writer."""

    title: str | None = None
    artist: str | None = None
    bpm: float | None = None
    key: str | None = None
    duration_seconds: int | None = None
    position: int | None = None
    display_key: str | None = None


@runtime_checkable
class VirtualTrackMetadataResolver(Protocol):
    """Provide optional known metadata without reading media or databases."""

    def resolve(self, track: VirtualTrack) -> NmlTrackMetadata | None:
        """Return metadata already available for a projected track."""


@dataclass(frozen=True, slots=True)
class NmlCatalogEntry:
    """One in-memory NML file destined for a logical virtual path."""

    source_id: str
    playlist_id: str
    logical_filename: str
    display_name: str
    destination_path: VirtualPath
    content: str
    content_sha256: str
    track_count: int
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class NmlCatalog:
    """Immutable deterministic set of NML files for one source revision."""

    source_id: str
    snapshot_id: str
    entries: tuple[NmlCatalogEntry, ...]


_RENDER_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _safe_segment(value: str, stable_id: str) -> str:
    cleaned = value.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    if cleaned in {"", ".", ".."}:
        cleaned = "playlist"
    return f"{cleaned}--{sha256(stable_id.encode('utf-8')).hexdigest()[:12]}"


def _nml_path(playlist_path: VirtualPath, playlist_id: str, display_name: str) -> VirtualPath:
    if not playlist_id:
        raise NmlCatalogIdentityError("playlist_id must be non-empty")
    segment = _safe_segment(display_name, playlist_id)
    return VirtualPath((*playlist_path.segments[:-1], f"{segment}.nml"))


def _resolved_path(resolver: TrackOriginNmlPathResolver, track: VirtualTrack) -> str:
    try:
        resolved = resolver.resolve(track.origin)
    except Exception as exc:
        raise NmlCatalogPathResolutionError(track.track_id) from exc
    if not isinstance(resolved, str) or not resolved.strip():
        raise NmlCatalogPathResolutionError(track.track_id)
    return resolved


def _library_track(track: VirtualTrack, metadata: NmlTrackMetadata | None, index: int) -> LibraryTrack:
    details = metadata or NmlTrackMetadata()
    return LibraryTrack(
        track_id=index,
        title=details.title or track.display_name,
        artist=details.artist,
        album=None,
        genre=None,
        label=None,
        bpm=details.bpm,
        # Catalog metadata is already authoritative; do not infer a half/double tempo.
        normalized_bpm=details.bpm,
        rekordbox_key=details.key,
        preferred_display_key=details.display_key,
        duration_seconds=details.duration_seconds,
        track_number=details.position,
        rating=None,
        comment=None,
        rekordbox_path=track.origin.virtual_path.value,
        source_path=track.origin.virtual_path.value,
        file_name=track.origin.virtual_path.value.rsplit("/", maxsplit=1)[-1] or None,
        file_size=track.size_bytes,
    )


def render_nml_catalog(
    snapshot: ProjectionSnapshot,
    path_resolver: TrackOriginNmlPathResolver,
    *,
    metadata_resolver: VirtualTrackMetadataResolver | None = None,
) -> NmlCatalog:
    """Render one NML per selectable playlist without I/O or platform access."""
    if not isinstance(snapshot, ProjectionSnapshot):
        raise TypeError("snapshot must be a ProjectionSnapshot")
    if not isinstance(path_resolver, TrackOriginNmlPathResolver):
        raise TypeError("path_resolver must implement TrackOriginNmlPathResolver")
    if metadata_resolver is not None and not isinstance(metadata_resolver, VirtualTrackMetadataResolver):
        raise TypeError("metadata_resolver must implement VirtualTrackMetadataResolver")

    tracks_by_playlist: dict[str, list[VirtualTrack]] = {}
    for track in snapshot.tracks:
        if track.origin.volume_identity != snapshot.source.volume_identity:
            raise NmlCatalogVolumeMismatchError(track.track_id)
        tracks_by_playlist.setdefault(track.playlist_id, []).append(track)

    entries: list[NmlCatalogEntry] = []
    destinations: set[VirtualPath] = set()
    for playlist in snapshot.playlists:
        if not playlist.selectable:
            continue
        destination = _nml_path(playlist.path, playlist.playlist_id, playlist.display_name)
        if destination in destinations:
            raise NmlCatalogNameCollisionError(destination.value)
        destinations.add(destination)
        projected_tracks = tracks_by_playlist.get(playlist.playlist_id, [])
        library_tracks: list[LibraryTrack] = []
        resolved_locations: dict[int, str] = {}
        for index, track in enumerate(projected_tracks, start=1):
            metadata = metadata_resolver.resolve(track) if metadata_resolver else None
            library_track = _library_track(track, metadata, index)
            library_tracks.append(library_track)
            resolved_locations[index] = _resolved_path(path_resolver, track)
        library_playlist = LibraryPlaylist(
            playlist_id=int(sha256(playlist.playlist_id.encode("utf-8")).hexdigest()[:12], 16),
            name=playlist.display_name,
            parent_id=None,
            sequence=0,
            is_folder=False,
            track_ids=list(resolved_locations),
        )
        try:
            content, _warnings = render_playlist_nml_content(
                library_tracks,
                library_playlist,
                session_id=snapshot.source.source_id,
                resolved_locations=resolved_locations,
                now=_RENDER_TIME,
            )
        except Exception as exc:
            raise NmlCatalogRenderError(playlist.playlist_id) from exc
        entries.append(
            NmlCatalogEntry(
                source_id=snapshot.source.source_id,
                playlist_id=playlist.playlist_id,
                logical_filename=destination.segments[-1],
                display_name=playlist.display_name,
                destination_path=destination,
                content=content,
                content_sha256=sha256(content.encode("utf-8")).hexdigest(),
                track_count=len(projected_tracks),
                snapshot_id=snapshot.snapshot_id,
            )
        )
    return NmlCatalog(snapshot.source.source_id, snapshot.snapshot_id, tuple(entries))


def rebase_nml_catalog_root(catalog: NmlCatalog, root_segment: str) -> NmlCatalog:
    """Replace only the physical top-level destination of a rendered catalog."""
    if not isinstance(catalog, NmlCatalog):
        raise TypeError("catalog must be an NmlCatalog")
    if (
        not isinstance(root_segment, str)
        or not root_segment
        or root_segment in {".", ".."}
        or any(token in root_segment for token in ("\x00", "/", "\\", ":"))
    ):
        raise NmlCatalogIdentityError("catalog root segment is invalid")
    entries = tuple(
        NmlCatalogEntry(
            entry.source_id,
            entry.playlist_id,
            entry.logical_filename,
            entry.display_name,
            VirtualPath((root_segment, *entry.destination_path.segments[1:])),
            entry.content,
            entry.content_sha256,
            entry.track_count,
            entry.snapshot_id,
        )
        for entry in catalog.entries
    )
    return NmlCatalog(catalog.source_id, catalog.snapshot_id, entries)
