"""Authorial synthetic metadata; no proprietary metadata layout is assumed."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from rekordbox_usb.library_models import LibraryPlaylist, LibraryTrack, UnifiedLibrary
from rekordbox_usb.legacy_reader import read_legacy
from rekordbox_usb.metadata_normalization import normalize_key
from rekordbox_usb.nml_writer import render_playlist_nml_content
from rekordbox_usb.public_runtime import (
    _LegacyReader, _LibraryMetadataResolver, _ProjectionBuilder,
)
from rekordbox_usb.virtual_nml_catalog import render_nml_catalog
from rekordbox_usb.volume_access import VolumeIdentity
from tests.contracts.test_public_executable import _Backend, _fixture, _runtime
from tests.fixtures.synthetic_legacy_pdb import SyntheticLegacyPdbBuilder


def _track(ident, bpm, key):
    return LibraryTrack(
        track_id=ident, title=f'Synthetic <{ident}> & "quoted"', artist="A & B",
        album=None, genre=None, label=None, bpm=bpm, rekordbox_key=key,
        duration_seconds=None, track_number=None, rating=None, comment=None,
        rekordbox_path=f"Contents/{ident}.mp3", source_path=f"Contents/{ident}.mp3",
        file_name=f"{ident}.mp3", file_size=None,
    )


def _library(tracks):
    return UnifiedLibrary(
        schema_version=1, generated_at="2000-01-01T00:00:00Z", session_id="synthetic",
        usb_root="X:\\", source="synthetic-public-reader", database_path="synthetic",
        tracks=tracks, playlists=[LibraryPlaylist(10, "List & <keys>", None, 0, False,
                                                 [t.track_id for t in tracks])],
    )


def _assert_metadata(entry, bpm, key):
    tempo, info, musical = entry.find("TEMPO"), entry.find("INFO"), entry.find("MUSICAL_KEY")
    if bpm is None:
        assert tempo is None
    else:
        assert tempo is not None
        assert float(tempo.get("BPM")) == bpm
    if key is None:
        assert "KEY" not in info.attrib
        assert musical is None
    else:
        assert musical is not None
        expected = normalize_key(key)
        actual = normalize_key(info.get("KEY"), int(musical.get("VALUE")))
        assert actual.is_valid and not actual.key_conflict
        assert (actual.mode, actual.pitch_class) == (expected.mode, expected.pitch_class)


KEYS = [
    "C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B",
    "Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "Abm", "Am", "Bbm", "Bm",
]


@pytest.mark.parametrize("key", KEYS + ["1m", "12d", "8A", "8B"])
def test_writer_key_values_round_trip_musically(key):
    track = _track(1, 128.0, key)
    content, warnings = render_playlist_nml_content(
        [track], _library([track]).playlists[0], session_id="synthetic",
        resolved_locations={1: "X:\\Contents\\1.mp3"}, now=datetime(2000, 1, 1),
    )
    assert not warnings
    entry = ET.fromstring(content).find("./COLLECTION/ENTRY")
    _assert_metadata(entry, 128.0, key)
    assert entry.get("TITLE") == track.title
    assert entry.get("ARTIST") == track.artist


@pytest.mark.parametrize("bpm,key", [(128.0, "C"), (128.5, "Am"), (None, None),
                                     (64.25, "F#m"), (175.5, "Bb"),
                                     (128.01, None), (None, "Dm")])
def test_public_catalog_preserves_authoritative_bpm_and_key(bpm, key):
    track = _track(1, bpm, key)
    library = _library([track])
    snapshot = _ProjectionBuilder().build(
        library, volume_identity=VolumeIdentity("synthetic"), source_id="source",
        display_name="Synthetic", snapshot_id="revision",
    )

    class Paths:
        def resolve(self, origin):
            return "X:\\" + origin.virtual_path.value.replace("/", "\\")

    catalog = render_nml_catalog(snapshot, Paths(), metadata_resolver=_LibraryMetadataResolver(library))
    _assert_metadata(ET.fromstring(catalog.entries[0].content).find("./COLLECTION/ENTRY"), bpm, key)
    # The old shared model can adjust tempo; public transport must retain the raw source value.
    assert track.raw_bpm == bpm


def test_metadata_resolver_requires_exact_origin():
    library = _library([_track(1, 128.5, "Am")])
    snapshot = _ProjectionBuilder().build(
        library, volume_identity=VolumeIdentity("synthetic"), source_id="source",
        display_name="Synthetic", snapshot_id="revision",
    )
    resolver, track = _LibraryMetadataResolver(library), snapshot.tracks[0]
    for changes in ({"provider_id": "other"}, {"native_library_id": "2"},
                    {"virtual_path": replace(track.origin.virtual_path, value="Contents/other.mp3")}):
        assert resolver.resolve(replace(track, origin=replace(track.origin, **changes))) is None
    assert resolver.resolve(track).bpm == 128.5


def test_existing_traktor_analysis_precedence_is_preserved():
    track = _track(1, 128.5, "Am")
    track.set_traktor_analysis_key("2d")
    track.set_traktor_analysis_bpm(130.0)
    library = _library([track])
    snapshot = _ProjectionBuilder().build(
        library, volume_identity=VolumeIdentity("synthetic"), source_id="source",
        display_name="Synthetic", snapshot_id="revision",
    )
    metadata = _LibraryMetadataResolver(library).resolve(snapshot.tracks[0])
    assert metadata.bpm == 130.0
    assert normalize_key(metadata.key).open_key == "2d"


def test_public_runtime_materializes_distinct_values_and_is_idempotent(tmp_path, monkeypatch):
    source, mount = tmp_path / "usb", tmp_path / "mount"
    _fixture(source)
    backend = _Backend(mount)
    runtime = _runtime(source, backend)
    original_read = _LegacyReader.read
    expected = {1: (128.0, "C"), 2: (128.5, "Am"), 3: (64.25, "F#m"), 4: (None, None)}

    def known_metadata(reader, session):
        # The approved Legacy fixture has no BPM/key layout. Inject only neutral metadata
        # at the reader model boundary; this is not proof of physical source extraction.
        library = original_read(reader, session)
        library.tracks = [replace(t, bpm=expected[t.track_id][0],
                                  raw_bpm=expected[t.track_id][0],
                                  normalized_bpm=expected[t.track_id][0],
                                  bpm_source="rekordbox" if expected[t.track_id][0] else None,
                                  rekordbox_key=expected[t.track_id][1],
                                  rekordbox_key_original=expected[t.track_id][1],
                                  raw_key=expected[t.track_id][1], normalized_key=None,
                                  traktor_preview_key=None)
                          for t in library.tracks]
        return library

    monkeypatch.setattr(_LegacyReader, "read", known_metadata)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    result = runtime.publish(source, tmp_path / "data")
    assert result.changed
    entries = [e for p in mount.rglob("*.nml")
               for e in ET.parse(p).findall("./COLLECTION/ENTRY")]
    assert len(entries) == 4
    for entry in entries:
        ident = {"One": 1, "Two": 2, "Three": 3, "Four": 4}[entry.get("TITLE")]
        _assert_metadata(entry, *expected[ident])
    assert not runtime.publish(source, tmp_path / "data").changed
    assert backend.calls == ["create", "attach", "initialize", "format", "mount"]
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_known_missing_legacy_metadata_stays_absent(tmp_path):
    source, mount = tmp_path / "usb", tmp_path / "mount"
    _fixture(source)
    runtime = _runtime(source, _Backend(mount))
    runtime.publish(source, tmp_path / "data")
    for path in mount.rglob("*.nml"):
        for entry in ET.parse(path).findall("./COLLECTION/ENTRY"):
            _assert_metadata(entry, None, None)


def _metadata_fixture(source, values):
    builder = SyntheticLegacyPdbBuilder().add_node(10, "Synthetic & metadata")
    for ident, (bpmx100, key) in enumerate(values, start=1):
        builder.add_track(ident, f"Synthetic {ident}", f"Contents/{ident}.mp3",
                          bpmx100=bpmx100, key_id=ident if key else 0)
        if key:
            builder.add_key(ident, key)
        builder.add_entry(10, ident, ident - 1)
        audio = source / f"Contents/{ident}.mp3"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"authorial synthetic placeholder")
    database = source / "PIONEER/rekordbox/export.pdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(builder.build())


def test_synthetic_source_to_public_materialization_all_keys_and_decimals(tmp_path):
    values = [(12800 + ident, key) for ident, key in enumerate(KEYS)]
    values += [(12850, "Am"), (6425, "C"), (17550, "Dm"), (0, None)]
    source, mount = tmp_path / "usb", tmp_path / "mount"
    _metadata_fixture(source, values)
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    raw = read_legacy(source)
    assert raw["status"] == "SUCCESS"
    for raw_track, (bpmx100, key) in zip(raw["library"]["tracks"], values, strict=True):
        assert raw_track.get("bpm") == (bpmx100 / 100 if bpmx100 else None)
        assert raw_track.get("rekordbox_key") == key
    backend = _Backend(mount)
    runtime = _runtime(source, backend)
    library = _LegacyReader(tmp_path / "data").read(runtime._session(runtime._selected_volume(source)))
    for track, (bpmx100, key) in zip(library.tracks, values, strict=True):
        assert track.raw_bpm == track.bpm == (bpmx100 / 100 if bpmx100 else None)
        assert not track.bpm_was_octave_adjusted
        assert track.raw_key == key
    result = runtime.publish(source, tmp_path / "data")
    assert result.track_count == len(values)
    entries = ET.parse(next(mount.rglob("*.nml"))).findall("./COLLECTION/ENTRY")
    for entry, (bpmx100, key) in zip(entries, values, strict=True):
        _assert_metadata(entry, bpmx100 / 100 if bpmx100 else None, key)
        assert entry.find("INFO").get("KEY") == key
    assert not runtime.publish(source, tmp_path / "data").changed
    assert before == {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}


@pytest.mark.parametrize("invalid", ["dangling", "duplicate", "invalid_key"])
def test_reader_metadata_inconsistency_fails_without_partial_library(tmp_path, invalid):
    builder = (SyntheticLegacyPdbBuilder().add_track(1, "Synthetic", "Contents/1.mp3",
               bpmx100=12850, key_id=1).add_node(10, "Synthetic").add_entry(10, 1, 0))
    if invalid != "dangling":
        builder.add_key(1, "not-a-key" if invalid == "invalid_key" else "Am")
    if invalid == "duplicate":
        builder.add_key(1, "C")
    database = tmp_path / "PIONEER/rekordbox/export.pdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(builder.build())
    result = read_legacy(tmp_path)
    assert result["status"] == "INVALID_LIBRARY"
    assert result["library"] is None


@pytest.mark.parametrize("name,relative,mode", [
    ("Synthetic Artist", 10, "short_ascii"),
    (("Synthetic " * 16).strip(), 12, "long_ascii"),
    ('Artista ç & <duo> "A"', 12, "utf16"),
])
def test_artist_survives_source_reader_conversion_and_public_playlist(tmp_path, monkeypatch, name, relative, mode):
    source, mount = tmp_path / "usb", tmp_path / "mount"
    builder = (SyntheticLegacyPdbBuilder()
               .add_artist(71, name, name_relative=relative, name_mode=mode)
               .add_track(1, "First", "Contents/1.mp3", artist_id=71)
               .add_track(2, "Second", "Contents/2.mp3", artist_id=71)
               .add_track(3, "Unknown", "Contents/3.mp3")
               .add_node(10, "Synthetic List").add_playlist_entries(10, [2, 1, 3]))
    database = source / "PIONEER/rekordbox/export.pdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(builder.build())
    for ident in (1, 2, 3):
        audio = source / f"Contents/{ident}.mp3"
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b"synthetic placeholder, not audio")
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    raw = read_legacy(source)
    assert raw["status"] == "SUCCESS"
    assert [t.get("artist") for t in raw["library"]["tracks"]] == [name, name, None]
    runtime = _runtime(source, _Backend(mount))
    library = _LegacyReader(tmp_path / "data").read(runtime._session(runtime._selected_volume(source)))
    assert [t.artist for t in library.tracks] == [name, name, None]
    # An existing catalog from the previous reader has no artist, while the USB
    # database is identical. Upgrading must refresh that local catalog too.
    original_read = _LegacyReader.read

    def previous_reader(reader, session):
        library = original_read(reader, session)
        library.tracks = [replace(t, artist=None) for t in library.tracks]
        return library

    with monkeypatch.context() as patch:
        patch.setattr(_LegacyReader, "read", previous_reader)
        assert runtime.publish(source, tmp_path / "data").track_count == 3
    old_entries = ET.parse(next(mount.rglob("*.nml"))).findall("./COLLECTION/ENTRY")
    assert all(e.get("ARTIST") == "" for e in old_entries)
    assert runtime.publish(source, tmp_path / "data").changed
    entries = ET.parse(next(mount.rglob("*.nml"))).findall("./COLLECTION/ENTRY")
    assert [(e.get("TITLE"), e.get("ARTIST")) for e in entries] == [
        ("Second", name), ("First", name), ("Unknown", "")]
    assert not runtime.publish(source, tmp_path / "data").changed
    assert before == {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}


@pytest.mark.parametrize("invalid", ["dangling", "duplicate", "empty", "bad_offset"])
def test_artist_inconsistency_refuses_partial_library(tmp_path, invalid):
    builder = (SyntheticLegacyPdbBuilder()
               .add_track(1, "Synthetic", "Contents/1.mp3", artist_id=71)
               .add_node(10, "Synthetic").add_entry(10, 1, 0))
    if invalid != "dangling":
        builder.add_artist(71, "" if invalid == "empty" else "Synthetic Artist")
    if invalid == "duplicate":
        builder.add_artist(71, "Other Artist")
    image = builder.build_image()
    payload = bytearray(image.data)
    if invalid == "bad_offset":
        payload[image.row_addresses[2][0] + 9] = 0
    database = tmp_path / "PIONEER/rekordbox/export.pdb"
    database.parent.mkdir(parents=True)
    database.write_bytes(payload)
    result = read_legacy(tmp_path)
    assert result["status"] == "INVALID_LIBRARY"
    assert result["library"] is None
