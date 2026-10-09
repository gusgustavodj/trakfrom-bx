"""Fresh projection tests from ADR-0006 and CONTRACT_V2_TO_SERVICE_LIBRARY_V1.

Synthetic IDs and paths identify relationships only; they are not format constants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rekordbox_usb.library_models import LibraryPlaylist, LibraryTrack, UnifiedLibrary
from rekordbox_usb.library_projection import (
    InvalidLibraryProjectionInputError,
    build_projection_input_from_library,
)
from rekordbox_usb.virtual_projection_builder import build_projection_snapshot
from rekordbox_usb.volume_access import VolumeIdentity


def _track(track_id: int = 11) -> LibraryTrack:
    relative_path = "Contents/synthetic.wav"
    return LibraryTrack(
        track_id=track_id,
        title="Synthetic Track",
        artist=None,
        album=None,
        genre=None,
        label=None,
        bpm=None,
        rekordbox_key=None,
        duration_seconds=None,
        track_number=None,
        rating=None,
        comment=None,
        rekordbox_path=relative_path,
        source_path=relative_path,
        file_name="synthetic.wav",
        file_size=None,
    )


def _library(root: Path, *, tracks: list[LibraryTrack], playlists: list[LibraryPlaylist]) -> UnifiedLibrary:
    return UnifiedLibrary(
        schema_version=1,
        generated_at="2026-01-01T00:00:00+00:00",
        session_id="synthetic-session",
        usb_root=str(root),
        source="synthetic-provider",
        database_path=str(root / "PIONEER" / "rekordbox" / "export.pdb"),
        tracks=tracks,
        playlists=playlists,
    )


def _project(library: UnifiedLibrary):
    return build_projection_input_from_library(
        library,
        volume_identity=VolumeIdentity("synthetic-volume"),
        source_id="synthetic-source",
        display_name="Synthetic Source",
        snapshot_id="synthetic-snapshot",
    )


def test_projection_keeps_source_identity_and_read_only_boundary(tmp_path: Path) -> None:
    # ADR-0006: platform-neutral projection, source identity and read-only access.
    source_root = tmp_path / "unmounted-synthetic-source"
    folder = LibraryPlaylist(70, "Folder", None, 0, True, [])
    playlist = LibraryPlaylist(71, "Playlist", 70, 0, False, [11])
    build_input = _project(_library(source_root, tracks=[_track()], playlists=[folder, playlist]))
    snapshot = build_projection_snapshot(build_input)

    assert build_input.read_only is True
    assert snapshot.source.read_only is True
    assert snapshot.source.volume_identity == VolumeIdentity("synthetic-volume")
    assert {entry.playlist_id for entry in snapshot.playlists} == {"70", "71"}
    assert len(snapshot.tracks) == 1
    assert snapshot.tracks[0].origin.volume_identity == VolumeIdentity("synthetic-volume")
    assert not source_root.exists()


def test_folder_cannot_claim_tracks(tmp_path: Path) -> None:
    # V2-to-service contract: folders have no direct track entries.
    folder = LibraryPlaylist(70, "Folder", None, 0, True, [11])
    library = _library(tmp_path, tracks=[_track()], playlists=[folder])

    with pytest.raises(InvalidLibraryProjectionInputError):
        _project(library)


def test_unknown_playlist_track_rejects_the_entire_projection(tmp_path: Path) -> None:
    # V2-to-service contract: no filtering of invalid track references.
    playlist = LibraryPlaylist(71, "Playlist", None, 0, False, [99])
    library = _library(tmp_path, tracks=[_track()], playlists=[playlist])

    with pytest.raises(InvalidLibraryProjectionInputError):
        _project(library)


def test_duplicate_track_identity_rejects_the_projection(tmp_path: Path) -> None:
    # V2-to-service contract requires unique track IDs.
    playlist = LibraryPlaylist(71, "Playlist", None, 0, False, [11])
    library = _library(tmp_path, tracks=[_track(), _track()], playlists=[playlist])

    with pytest.raises(InvalidLibraryProjectionInputError):
        _project(library)


def test_missing_parent_folder_rejects_the_projection(tmp_path: Path) -> None:
    # V2-to-service contract requires a valid parent folder reference.
    playlist = LibraryPlaylist(71, "Playlist", 99, 0, False, [11])
    library = _library(tmp_path, tracks=[_track()], playlists=[playlist])

    with pytest.raises(InvalidLibraryProjectionInputError):
        _project(library)
