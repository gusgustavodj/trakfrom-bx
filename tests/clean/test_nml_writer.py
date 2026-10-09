"""Public NML export checks from ADR-0007 and the read-only USB boundary.

Every input here is synthetic. The assertions concern published behavior, not
undocumented NML schema details or a proprietary reference implementation.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from rekordbox_usb.library_models import LibraryPlaylist, LibraryTrack, UnifiedLibrary
from rekordbox_usb.nml_writer import export_nml, export_playlist_nml
from rekordbox_usb.track_location import TrackLocationError


def _library(usb_root: Path, audio: Path) -> UnifiedLibrary:
    track = LibraryTrack(
        track_id=11,
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
        rekordbox_path="Contents/synthetic.wav",
        source_path=str(audio),
        file_name=audio.name,
        file_size=None,
    )
    return UnifiedLibrary(
        schema_version=1,
        generated_at="2026-01-01T00:00:00+00:00",
        session_id="synthetic-session",
        usb_root=str(usb_root),
        source="synthetic-provider",
        database_path=str(usb_root / "PIONEER" / "rekordbox" / "export.pdb"),
        tracks=[track],
        playlists=[LibraryPlaylist(71, "Synthetic List", None, 0, False, [11])],
    )


def test_export_publishes_parseable_artifact_and_preserves_source(tmp_path: Path) -> None:
    # ADR-0007: output is a generated artifact, audio stays on the USB, and
    # an existing collection.nml is never edited by this export.
    usb_root = tmp_path / "synthetic-usb"
    audio = usb_root / "Contents" / "synthetic.wav"
    audio.parent.mkdir(parents=True)
    audio_bytes = b"synthetic audio fixture"
    audio.write_bytes(audio_bytes)
    collection = usb_root / "collection.nml"
    collection_bytes = b"preexisting user collection sentinel"
    collection.write_bytes(collection_bytes)
    output = tmp_path / "published" / "synthetic.nml"

    result = export_nml(_library(usb_root, audio), output, include_grid=False, include_cues=False)

    assert output.is_file()
    ElementTree.parse(output)
    assert result.tracks == 1
    assert result.playlists == 1
    assert audio.name in output.read_text(encoding="utf-8")
    assert audio.read_bytes() == audio_bytes
    assert collection.read_bytes() == collection_bytes
    assert list(output.parent.iterdir()) == [output]


def test_missing_synthetic_media_cannot_publish_an_artifact(tmp_path: Path) -> None:
    # ADR-0007 requires a direct source reference; no fabricated media copy.
    usb_root = tmp_path / "synthetic-usb"
    missing_audio = usb_root / "Contents" / "missing.wav"
    output = tmp_path / "published" / "synthetic.nml"

    with pytest.raises(TrackLocationError):
        export_nml(_library(usb_root, missing_audio), output, include_grid=False)

    assert not output.exists()
    assert not missing_audio.exists()


def test_folder_is_not_exported_as_a_track_playlist(tmp_path: Path) -> None:
    # CONTRACT_V2_TO_SERVICE_LIBRARY_V1: folders have no direct track entries.
    usb_root = tmp_path / "synthetic-usb"
    audio = usb_root / "Contents" / "synthetic.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"synthetic audio fixture")
    library = _library(usb_root, audio)
    folder = LibraryPlaylist(70, "Folder", None, 0, True, [])
    output = tmp_path / "published" / "folder.nml"

    with pytest.raises(ValueError):
        export_playlist_nml(library, folder, output, include_grid=False)

    assert not output.exists()
