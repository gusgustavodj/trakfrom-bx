from __future__ import annotations

from dataclasses import dataclass
import inspect
from xml.etree import ElementTree as ET

import pytest

from rekordbox_usb.virtual_nml_catalog import (
    NmlCatalogRenderError,
    NmlCatalogPathResolutionError,
    NmlTrackMetadata,
    TrackOriginNmlPathResolver,
    VirtualTrackMetadataResolver,
    rebase_nml_catalog_root,
    render_nml_catalog,
)
from rekordbox_usb.virtual_projection import (
    ProjectionSnapshot,
    VirtualPath,
    VirtualPlaylist,
    VirtualSource,
    VirtualTrack,
)
from rekordbox_usb.volume_access import TrackOrigin, VirtualVolumePath, VolumeIdentity


VOLUME = VolumeIdentity("volume-1")


@dataclass(frozen=True)
class _WindowsResolver:
    def resolve(self, origin: TrackOrigin) -> str:
        return rf"D:\Music\{origin.virtual_path.value.split('/')[-1]}"


@dataclass(frozen=True)
class _MissingResolver:
    def resolve(self, origin: TrackOrigin) -> str:
        return ""


@dataclass(frozen=True)
class _MacResolver:
    def resolve(self, origin: TrackOrigin) -> str:
        return f"/Volumes/USB/{origin.virtual_path.value}"


@dataclass(frozen=True)
class _Metadata:
    def resolve(self, track: VirtualTrack) -> NmlTrackMetadata:
        return NmlTrackMetadata("Título", "Artista", 123.0, "4A", 321, 2)


def _snapshot(*, second: bool = False, folder: bool = False) -> ProjectionSnapshot:
    source = VirtualSource("source", VOLUME, "USB", "revision", True)
    playlists = [VirtualPlaylist("p1", "source", VirtualPath(("playlists", "Set")), "Set/Ácido", False, 1)]
    if folder:
        playlists.insert(0, VirtualPlaylist("folder", "source", VirtualPath(("playlists", "Pasta")), "Pasta", False, 0))
        playlists[1] = VirtualPlaylist("p1", "source", VirtualPath(("playlists", "Pasta", "Set")), "Set/Ácido", True, 1, "folder")
    else:
        playlists[0] = VirtualPlaylist("p1", "source", VirtualPath(("playlists", "Set")), "Set/Ácido", True, 1)
    tracks = [VirtualTrack("t1", "source", "p1", VirtualPath(("playlists", "Set", "Song")), TrackOrigin("test", VOLUME, VirtualVolumePath("music/song.mp3")), "Song.mp3", 100, True)]
    if second:
        playlists.append(VirtualPlaylist("p2", "source", VirtualPath(("playlists", "Set 2")), "Set/Ácido", True, 0))
    return ProjectionSnapshot(source, tuple(playlists), tuple(tracks), "revision")


def test_empty_snapshot_and_empty_playlist_render_deterministically() -> None:
    empty_source = ProjectionSnapshot(VirtualSource("s", VOLUME, "USB", "r", True), (), (), "r")
    assert render_nml_catalog(empty_source, _WindowsResolver()).entries == ()

    playlist = VirtualPlaylist("empty", "source", VirtualPath(("playlists", "Empty")), "Empty", True, 0)
    snapshot = ProjectionSnapshot(_snapshot().source, (playlist,), (), "revision")
    catalog = render_nml_catalog(snapshot, _WindowsResolver())
    assert catalog.entries[0].track_count == 0
    assert 'ENTRIES="0"' in catalog.entries[0].content


def test_catalog_renders_logical_paths_unique_names_and_metadata() -> None:
    catalog = render_nml_catalog(_snapshot(second=True, folder=True), _WindowsResolver(), metadata_resolver=_Metadata())
    entry = catalog.entries[0]
    root = ET.fromstring(entry.content)
    track = root.find("./COLLECTION/ENTRY")

    assert entry.destination_path.value.startswith("playlists/Pasta/")
    assert entry.logical_filename.endswith(".nml")
    assert "Set_Ácido" in entry.logical_filename
    assert track is not None
    assert track.attrib["TITLE"] == "Título"
    assert track.attrib["ARTIST"] == "Artista"
    assert track.find("ALBUM").attrib["TRACK"] == "2"
    assert track.find("INFO").attrib["KEY"] == "9m"
    assert track.find("INFO").attrib["PLAYTIME"] == "321"
    assert track.find("TEMPO").attrib["BPM"] == "123.000000"
    assert track.find("MUSICAL_KEY").attrib["VALUE"] == "17"
    assert track.find("LOCATION").attrib["VOLUME"] == "D:"
    assert "collection.nml" not in entry.content
    assert ".vhdx" not in entry.content
    assert len(catalog.entries) == 2


def test_catalog_is_deterministic_and_rejects_unresolved_paths() -> None:
    snapshot = _snapshot(second=True)
    first = render_nml_catalog(snapshot, _WindowsResolver())
    second = render_nml_catalog(snapshot, _WindowsResolver())

    assert first == second
    assert len({entry.logical_filename for entry in first.entries}) == 2
    with pytest.raises(NmlCatalogPathResolutionError):
        render_nml_catalog(snapshot, _MissingResolver())
    with pytest.raises(NmlCatalogRenderError):
        render_nml_catalog(snapshot, _MacResolver())


def test_catalog_root_can_be_rebased_without_changing_nml_content() -> None:
    catalog = render_nml_catalog(_snapshot(second=True, folder=True), _WindowsResolver())

    rebased = rebase_nml_catalog_root(catalog, "GUS CULT 25")

    assert [entry.destination_path.value for entry in rebased.entries] == [
        value.replace("playlists/", "GUS CULT 25/", 1)
        for value in [entry.destination_path.value for entry in catalog.entries]
    ]
    assert [entry.content for entry in rebased.entries] == [entry.content for entry in catalog.entries]


def test_shared_track_is_rendered_for_each_selectable_playlist() -> None:
    source = VirtualSource("source", VOLUME, "USB", "revision", True)
    one = VirtualPlaylist("one", "source", VirtualPath(("playlists", "One")), "One", True, 1)
    two = VirtualPlaylist("two", "source", VirtualPath(("playlists", "Two")), "Two", True, 1)
    origin = TrackOrigin("test", VOLUME, VirtualVolumePath("music/shared.mp3"))
    tracks = (
        VirtualTrack("shared", "source", "one", VirtualPath(("playlists", "One", "song")), origin, "Song", None, True),
        VirtualTrack("shared", "source", "two", VirtualPath(("playlists", "Two", "song")), origin, "Song", None, True),
    )
    catalog = render_nml_catalog(ProjectionSnapshot(source, (one, two), tracks, "revision"), _WindowsResolver())

    assert len(catalog.entries) == 2
    assert all(entry.track_count == 1 for entry in catalog.entries)
    assert all('ENTRIES="1"' in entry.content for entry in catalog.entries)


def test_catalog_module_has_no_platform_or_filesystem_dependencies() -> None:
    import rekordbox_usb.virtual_nml_catalog as catalog

    assert isinstance(_WindowsResolver(), TrackOriginNmlPathResolver)
    assert isinstance(_MacResolver(), TrackOriginNmlPathResolver)
    assert isinstance(_Metadata(), VirtualTrackMetadataResolver)
    source = inspect.getsource(catalog)
    assert all(token not in source for token in ("win32", "WinFsp", "ProjFS", "macFUSE", "FSKit", "open("))
