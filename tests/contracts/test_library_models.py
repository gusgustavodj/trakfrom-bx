from rekordbox_usb.library_models import (
    LibraryPlaylist,
    LibraryTrack,
    TrackAnalysis,
    UnifiedLibrary,
)
from rekordbox_usb.library_service import summarize


def test_library_summary_counts_metadata():
    tracks = [
        LibraryTrack(
            track_id=1,
            title="Track",
            artist="Artist",
            album=None,
            genre=None,
            label=None,
            bpm=128.0,
            rekordbox_key="8A",
            duration_seconds=180,
            track_number=1,
            rating=0,
            comment=None,
            rekordbox_path="/Contents/Track.mp3",
            source_path="E:/Contents/Track.mp3",
            file_name="Track.mp3",
            file_size=100,
            analysis=TrackAnalysis(beat_count=100),
        )
    ]
    playlists = [
        LibraryPlaylist(1, "Folder", None, 0, True, []),
        LibraryPlaylist(2, "Playlist", 1, 0, False, [1]),
    ]
    library = UnifiedLibrary(
        schema_version=1,
        generated_at="now",
        session_id="session",
        usb_root="E:/",
        source="one_library",
        database_path="E:/PIONEER/rekordbox/exportLibrary.db",
        tracks=tracks,
        playlists=playlists,
    )

    summary = summarize(library)

    assert summary.tracks == 1
    assert summary.playlist_folders == 1
    assert summary.playlists == 1
    assert summary.playlist_entries == 1
    assert summary.tracks_with_bpm == 1
    assert summary.tracks_with_key == 1
    assert summary.tracks_with_beatgrid == 1
