"""Public conversion from a validated library to projection-builder input."""

from __future__ import annotations

from .library_models import UnifiedLibrary
from .virtual_projection_builder import (
    ProjectionBuildInput,
    ProjectionPlaylistInput,
    ProjectionTrackInput,
)
from .volume_access import TrackOrigin, VirtualVolumePath, VolumeIdentity


class InvalidLibraryProjectionInputError(ValueError):
    """Raised when a neutral library cannot be projected without data loss."""


def build_projection_input_from_library(
    library: UnifiedLibrary,
    *,
    volume_identity: VolumeIdentity,
    source_id: str,
    display_name: str,
    snapshot_id: str,
) -> ProjectionBuildInput:
    """Convert one fully validated neutral library without filesystem access."""

    if not isinstance(library, UnifiedLibrary):
        raise TypeError("library must be a UnifiedLibrary")
    tracks_by_id = {}
    for track in library.tracks:
        if track.track_id in tracks_by_id:
            raise InvalidLibraryProjectionInputError("track ids must be unique")
        tracks_by_id[track.track_id] = track

    playlists_by_id = {}
    for playlist in library.playlists:
        if playlist.playlist_id in playlists_by_id:
            raise InvalidLibraryProjectionInputError("playlist ids must be unique")
        playlists_by_id[playlist.playlist_id] = playlist

    playlist_inputs: list[ProjectionPlaylistInput] = []
    track_inputs: list[ProjectionTrackInput] = []
    for playlist in library.playlists:
        if playlist.parent_id is not None and playlist.parent_id not in playlists_by_id:
            raise InvalidLibraryProjectionInputError("playlist parent must exist")
        if playlist.is_folder and playlist.track_ids:
            raise InvalidLibraryProjectionInputError("folder cannot contain tracks")
        playlist_inputs.append(
            ProjectionPlaylistInput(
                playlist_id=str(playlist.playlist_id),
                source_id=source_id,
                display_name=playlist.name,
                is_folder=playlist.is_folder,
                track_count=0 if playlist.is_folder else len(playlist.track_ids),
                parent_playlist_id=(
                    str(playlist.parent_id) if playlist.parent_id is not None else None
                ),
            )
        )
        if playlist.is_folder:
            continue
        for track_id in playlist.track_ids:
            track = tracks_by_id.get(track_id)
            if track is None:
                raise InvalidLibraryProjectionInputError("playlist track must exist")
            track_inputs.append(
                ProjectionTrackInput(
                    track_id=str(track.track_id),
                    source_id=source_id,
                    playlist_id=str(playlist.playlist_id),
                    origin=TrackOrigin(
                        provider_id=library.source,
                        volume_identity=volume_identity,
                        virtual_path=VirtualVolumePath(track.source_path),
                        native_library_id=str(track.track_id),
                    ),
                    display_name=track.title,
                    size_bytes=track.file_size,
                )
            )

    return ProjectionBuildInput(
        volume_identity=volume_identity,
        source_id=source_id,
        display_name=display_name,
        snapshot_id=snapshot_id,
        playlists=tuple(playlist_inputs),
        tracks=tuple(track_inputs),
        read_only=True,
    )
