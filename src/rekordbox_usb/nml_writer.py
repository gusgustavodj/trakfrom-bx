from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Literal
from xml.etree import ElementTree as ET

from .library_models import BeatGridPoint, CuePoint, LibraryPlaylist, LibraryTrack, UnifiedLibrary
from .track_location import TrackLocationResolver
from .source_write_guard import SourceWriteBoundary, WriteIntent, atomic_write_bytes

KeyPolicy = Literal["preview_only", "import", "omit"]
_VALID_KEY_POLICIES = {"preview_only", "import", "omit"}


@dataclass(slots=True)
class NmlExportResult:
    output_path: str
    tracks: int
    playlists: int
    folders: int
    grid_markers: int
    hot_cues: int
    memory_cues: int
    saved_loops: int
    key_policy: str
    preview_keys: int
    musical_keys: int
    warnings: list[str]


_CAMELOT_MINOR = {
    1: 20,
    2: 15,
    3: 22,
    4: 17,
    5: 12,
    6: 19,
    7: 14,
    8: 21,
    9: 16,
    10: 23,
    11: 18,
    12: 13,
}

_CAMELOT_MAJOR = {
    1: 11,
    2: 6,
    3: 1,
    4: 8,
    5: 3,
    6: 10,
    7: 5,
    8: 0,
    9: 7,
    10: 2,
    11: 9,
    12: 4,
}

# Traktor Open Key notation: 1m=A minor, 1d=C major.
_OPEN_MINOR = {
    1: 21,
    2: 16,
    3: 23,
    4: 18,
    5: 13,
    6: 20,
    7: 15,
    8: 22,
    9: 17,
    10: 12,
    11: 19,
    12: 14,
}
_OPEN_MAJOR = {
    1: 0,
    2: 7,
    3: 2,
    4: 9,
    5: 4,
    6: 11,
    7: 6,
    8: 1,
    9: 8,
    10: 3,
    11: 10,
    12: 5,
}


def normalize_key_policy(value: str) -> KeyPolicy:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in _VALID_KEY_POLICIES:
        allowed = ", ".join(sorted(_VALID_KEY_POLICIES))
        raise ValueError(f"Invalid key policy {value!r}. Expected one of: {allowed}")
    return normalized  # type: ignore[return-value]


def traktor_key_value(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    match = re.fullmatch(r"(1[0-2]|[1-9])([ABab])", text)
    if match:
        number = int(match.group(1))
        return (_CAMELOT_MINOR if match.group(2).upper() == "A" else _CAMELOT_MAJOR)[number]

    match = re.fullmatch(r"(1[0-2]|[1-9])([mMdD])", text)
    if match:
        number = int(match.group(1))
        return (_OPEN_MINOR if match.group(2).lower() == "m" else _OPEN_MAJOR)[number]
    return None


def _nml_location(source_path: str) -> tuple[str, str, str, str]:
    path = PureWindowsPath(source_path)
    if not path.drive or not path.name:
        raise ValueError(f"Track path is not an absolute Windows path: {source_path}")
    folders = path.parts[1:-1]
    directory = "".join(f"/:{part}" for part in folders) + "/:"
    volume = path.drive
    primary_key = f"{volume}{directory}{path.name}"
    return volume, directory, path.name, primary_key


def _date_attrs(now: datetime) -> tuple[str, str]:
    return f"{now.year}/{now.month}/{now.day}", str(
        now.hour * 3600 + now.minute * 60 + now.second
    )


def _first_downbeat(points: list[BeatGridPoint]) -> BeatGridPoint | None:
    if not points:
        return None
    return next((point for point in points if point.beat == 1), points[0])


def _add_cue(entry: ET.Element, cue: CuePoint, *, default_name: str, hotcue: int) -> None:
    attrs = {
        "NAME": cue.name or default_name,
        "DISPL_ORDER": "0",
        "TYPE": "5" if cue.kind == "loop" else "0",
        "START": f"{cue.time_seconds * 1000.0:.6f}",
        "LEN": f"{(cue.loop_length_seconds or 0.0) * 1000.0:.6f}",
        "REPEATS": "-1",
        "HOTCUE": str(hotcue),
    }
    if cue.color_rgb:
        attrs["COLOR"] = cue.color_rgb
    ET.SubElement(entry, "CUE_V2", attrs)


def _add_track_entry(
    collection: ET.Element,
    track: LibraryTrack,
    now: datetime,
    include_grid: bool,
    include_cues: bool,
    key_policy: KeyPolicy,
    warnings: list[str],
    *,
    resolved_location: str,
) -> tuple[str, int, int, int, int, int, int]:
    modified_date, modified_time = _date_attrs(now)
    entry = ET.SubElement(
        collection,
        "ENTRY",
        {
            "MODIFIED_DATE": modified_date,
            "MODIFIED_TIME": modified_time,
            "TITLE": track.title or "",
            "ARTIST": track.artist or "",
        },
    )

    try:
        volume, directory, filename, primary_key = _nml_location(resolved_location)
    except ValueError as exc:
        warnings.append(str(exc))
        fallback_path = resolved_location
        volume, directory, filename, primary_key = "", "/:", track.file_name or "", fallback_path

    ET.SubElement(entry, "LOCATION", {"DIR": directory, "FILE": filename, "VOLUME": volume})

    album_attrs: dict[str, str] = {}
    if track.track_number is not None:
        album_attrs["TRACK"] = str(track.track_number)
    if track.album:
        album_attrs["TITLE"] = track.album
    ET.SubElement(entry, "ALBUM", album_attrs)
    ET.SubElement(entry, "MODIFICATION_INFO", {"AUTHOR_TYPE": "user"})

    info_attrs: dict[str, str] = {"IMPORT_DATE": modified_date, "FLAGS": "12"}
    if track.genre:
        info_attrs["GENRE"] = track.genre
    if track.label:
        info_attrs["LABEL"] = track.label
    if key_policy != "omit" and track.active_key:
        # Keep an explicit textual fallback while MUSICAL_KEY carries host notation semantics.
        info_attrs["KEY"] = track.preferred_display_key or track.active_key
    if track.duration_seconds is not None:
        info_attrs["PLAYTIME"] = str(track.duration_seconds)
        info_attrs["PLAYTIME_FLOAT"] = f"{float(track.duration_seconds):.6f}"
    if track.file_size is not None:
        info_attrs["FILESIZE"] = str(max(0, int(track.file_size) // 1024))
    if track.comment:
        info_attrs["COMMENT"] = track.comment
    ET.SubElement(entry, "INFO", info_attrs)

    if track.bpm is not None:
        ET.SubElement(
            entry,
            "TEMPO",
            {"BPM": f"{track.bpm:.6f}", "BPM_QUALITY": "100.000000"},
        )

    preview_key_count = int(bool(track.active_key and key_policy != "omit"))
    musical_key_count = 0
    if key_policy != "omit":
        key_value = traktor_key_value(track.active_key)
        if key_value is not None:
            ET.SubElement(entry, "MUSICAL_KEY", {"VALUE": str(key_value)})
            musical_key_count = 1

    grid_count = 0
    if include_grid and track.bpm is not None:
        marker = _first_downbeat(track.analysis.beat_grid)
        marker_time = marker.time_seconds if marker else track.analysis.first_beat_seconds
        marker_bpm = marker.bpm if marker else track.bpm
        if marker_time is not None:
            grid = ET.SubElement(
                entry,
                "CUE_V2",
                {
                    "NAME": "AutoGrid",
                    "DISPL_ORDER": "0",
                    "TYPE": "4",
                    "START": f"{marker_time * 1000.0:.6f}",
                    "LEN": "0.000000",
                    "REPEATS": "-1",
                    "HOTCUE": "-1",
                },
            )
            ET.SubElement(grid, "GRID", {"BPM": f"{marker_bpm:.6f}"})
            grid_count = 1
        if track.analysis.variable_bpm:
            warnings.append(
                f"Variable BPM grid simplified to one marker for track {track.track_id}: "
                f"{track.title}"
            )

    hot_count = memory_count = loop_count = 0
    if include_cues:
        for cue in track.analysis.hot_cues:
            index = (cue.hotcue_index - 1) if cue.hotcue_index and cue.hotcue_index > 0 else -1
            _add_cue(entry, cue, default_name="n.n.", hotcue=index)
            hot_count += 1

        for cue in track.analysis.memory_cues:
            _add_cue(entry, cue, default_name="n.n.", hotcue=-1)
            memory_count += 1

        for cue in track.analysis.saved_loops:
            index = (cue.hotcue_index - 1) if cue.hotcue_index and cue.hotcue_index > 0 else -1
            _add_cue(entry, cue, default_name="n.n.", hotcue=index)
            loop_count += 1

    return (
        primary_key,
        grid_count,
        hot_count,
        memory_count,
        loop_count,
        preview_key_count,
        musical_key_count,
    )


def _append_playlist_node(
    parent: ET.Element,
    item: LibraryPlaylist,
    children: dict[int | None, list[LibraryPlaylist]],
    primary_keys: dict[int, str],
    session_id: str,
    preserve_playlist_order: bool,
) -> None:
    if item.is_folder:
        node = ET.SubElement(parent, "NODE", {"TYPE": "FOLDER", "NAME": item.name})
        child_items = children.get(item.playlist_id, [])
        if not preserve_playlist_order:
            child_items = sorted(
                child_items,
                key=lambda value: (value.sequence, value.playlist_id),
            )
        subnodes = ET.SubElement(node, "SUBNODES", {"COUNT": str(len(child_items))})
        for child in child_items:
            _append_playlist_node(
                subnodes,
                child,
                children,
                primary_keys,
                session_id,
                preserve_playlist_order,
            )
        return

    _append_single_playlist(parent, item, primary_keys, session_id)


def _append_single_playlist(
    parent: ET.Element,
    item: LibraryPlaylist,
    primary_keys: dict[int, str],
    session_id: str,
) -> None:
    node = ET.SubElement(parent, "NODE", {"TYPE": "PLAYLIST", "NAME": item.name})
    playlist_uuid = uuid.uuid5(
        uuid.NAMESPACE_URL, f"rekordbox-usb:{session_id}:{item.playlist_id}"
    ).hex
    valid_track_ids = [track_id for track_id in item.track_ids if track_id in primary_keys]
    playlist = ET.SubElement(
        node,
        "PLAYLIST",
        {"ENTRIES": str(len(valid_track_ids)), "TYPE": "LIST", "UUID": playlist_uuid},
    )
    for track_id in valid_track_ids:
        playlist_entry = ET.SubElement(playlist, "ENTRY")
        ET.SubElement(
            playlist_entry,
            "PRIMARYKEY",
            {"TYPE": "TRACK", "KEY": primary_keys[track_id]},
        )


def _new_root() -> ET.Element:
    root = ET.Element("NML", {"VERSION": "20"})
    ET.SubElement(
        root,
        "HEAD",
        {"COMPANY": "www.native-instruments.com", "PROGRAM": "Traktor Pro 4"},
    )
    return root


def _write_nml(root: ET.Element, output_path: Path, source_root: Path) -> None:
    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    content = '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n' + xml_body + "\n"
    atomic_write_bytes(
        SourceWriteBoundary(source_root), output_path, content.encode("utf-8"),
        intent=WriteIntent.NML_EXPORT,
    )


def render_playlist_nml_content(
    tracks: list[LibraryTrack],
    playlist: LibraryPlaylist,
    *,
    session_id: str,
    resolved_locations: dict[int, str],
    now: datetime,
    key_policy: str = "preview_only",
) -> tuple[str, tuple[str, ...]]:
    """Render one playlist NML deterministically without filesystem access."""
    if playlist.is_folder:
        raise ValueError("A folder cannot be rendered as a playlist NML")
    normalized_policy = normalize_key_policy(key_policy)
    root = _new_root()
    collection = ET.SubElement(root, "COLLECTION", {"ENTRIES": str(len(tracks))})
    warnings: list[str] = []
    primary_keys: dict[int, str] = {}
    for track in tracks:
        try:
            resolved_location = resolved_locations[track.track_id]
        except KeyError as exc:
            raise ValueError(f"Missing resolved location for track {track.track_id}") from exc
        _nml_location(resolved_location)
        primary_key, *_ = _add_track_entry(
            collection,
            track,
            now,
            False,
            False,
            normalized_policy,
            warnings,
            resolved_location=resolved_location,
        )
        primary_keys[track.track_id] = primary_key

    ET.SubElement(root, "SETS", {"ENTRIES": "0"})
    playlists_root = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists_root, "NODE", {"TYPE": "FOLDER", "NAME": "$ROOT"})
    root_subnodes = ET.SubElement(root_node, "SUBNODES", {"COUNT": "1"})
    bridge_node = ET.SubElement(root_subnodes, "NODE", {"TYPE": "FOLDER", "NAME": "rekordbox USB"})
    bridge_subnodes = ET.SubElement(bridge_node, "SUBNODES", {"COUNT": "1"})
    _append_single_playlist(bridge_subnodes, playlist, primary_keys, session_id)
    ET.SubElement(root, "INDEXING")
    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n' + xml_body + "\n",
        tuple(warnings),
    )


def _ordered_unique_tracks(
    track_ids: list[int], tracks_by_id: dict[int, LibraryTrack]
) -> list[LibraryTrack]:
    seen: set[int] = set()
    result: list[LibraryTrack] = []
    for track_id in track_ids:
        if track_id in seen:
            continue
        track = tracks_by_id.get(track_id)
        if track is not None:
            result.append(track)
            seen.add(track_id)
    return result


def export_playlist_nml(
    library: UnifiedLibrary,
    playlist: LibraryPlaylist,
    output_path: Path,
    *,
    include_grid: bool = True,
    key_policy: str = "preview_only",
    location_resolver: TrackLocationResolver | None = None,
) -> NmlExportResult:
    if playlist.is_folder:
        raise ValueError("A folder cannot be exported as a playlist NML")

    normalized_policy = normalize_key_policy(key_policy)
    resolver = location_resolver or TrackLocationResolver()
    now = datetime.now()
    warnings: list[str] = []
    root = _new_root()
    tracks_by_id = {track.track_id: track for track in library.tracks}
    selected_tracks = _ordered_unique_tracks(playlist.track_ids, tracks_by_id)
    collection = ET.SubElement(root, "COLLECTION", {"ENTRIES": str(len(selected_tracks))})

    primary_keys: dict[int, str] = {}
    grid_markers = hot_cues = memory_cues = saved_loops = 0
    preview_keys = musical_keys = 0
    for track in selected_tracks:
        (
            primary_key,
            grids,
            hot,
            memory,
            loops,
            preview,
            musical,
        ) = _add_track_entry(
            collection,
            track,
            now,
            include_grid,
            True,
            normalized_policy,
            warnings,
            resolved_location=resolver.resolve(track),
        )
        primary_keys[track.track_id] = primary_key
        grid_markers += grids
        hot_cues += hot
        memory_cues += memory
        saved_loops += loops
        preview_keys += preview
        musical_keys += musical

    ET.SubElement(root, "SETS", {"ENTRIES": "0"})
    playlists_root = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists_root, "NODE", {"TYPE": "FOLDER", "NAME": "$ROOT"})
    root_subnodes = ET.SubElement(root_node, "SUBNODES", {"COUNT": "1"})
    bridge_node = ET.SubElement(root_subnodes, "NODE", {"TYPE": "FOLDER", "NAME": "rekordbox USB"})
    bridge_subnodes = ET.SubElement(bridge_node, "SUBNODES", {"COUNT": "1"})
    _append_single_playlist(bridge_subnodes, playlist, primary_keys, library.session_id)
    ET.SubElement(root, "INDEXING")
    _write_nml(root, output_path, Path(library.usb_root))

    missing = [track_id for track_id in playlist.track_ids if track_id not in tracks_by_id]
    if missing:
        warnings.append(
            f"Playlist {playlist.name!r} references missing track IDs: "
            + ", ".join(map(str, missing))
        )

    return NmlExportResult(
        output_path=str(output_path),
        tracks=len(selected_tracks),
        playlists=1,
        folders=0,
        grid_markers=grid_markers,
        hot_cues=hot_cues,
        memory_cues=memory_cues,
        saved_loops=saved_loops,
        key_policy=normalized_policy,
        preview_keys=preview_keys,
        musical_keys=musical_keys,
        warnings=warnings,
    )


def export_nml(
    library: UnifiedLibrary,
    output_path: Path,
    *,
    folder_name: str = "rekordbox USB",
    include_grid: bool = True,
    key_policy: str = "import",
    location_resolver: TrackLocationResolver | None = None,
    now: datetime | None = None,
    preserve_playlist_order: bool = False,
    include_cues: bool = True,
) -> NmlExportResult:
    normalized_policy = normalize_key_policy(key_policy)
    resolver = location_resolver or TrackLocationResolver()
    export_time = now or datetime.now()
    warnings: list[str] = []
    root = _new_root()

    collection = ET.SubElement(root, "COLLECTION", {"ENTRIES": str(len(library.tracks))})
    primary_keys: dict[int, str] = {}
    grid_markers = hot_cues = memory_cues = saved_loops = 0
    preview_keys = musical_keys = 0
    for track in library.tracks:
        (
            key,
            grids,
            hot,
            memory,
            loops,
            preview,
            musical,
        ) = _add_track_entry(
            collection,
            track,
            export_time,
            include_grid,
            include_cues,
            normalized_policy,
            warnings,
            resolved_location=resolver.resolve(track),
        )
        primary_keys[track.track_id] = key
        grid_markers += grids
        hot_cues += hot
        memory_cues += memory
        saved_loops += loops
        preview_keys += preview
        musical_keys += musical

    ET.SubElement(root, "SETS", {"ENTRIES": "0"})
    playlists_root = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists_root, "NODE", {"TYPE": "FOLDER", "NAME": "$ROOT"})
    root_subnodes = ET.SubElement(root_node, "SUBNODES", {"COUNT": "1"})
    bridge_node = ET.SubElement(root_subnodes, "NODE", {"TYPE": "FOLDER", "NAME": folder_name})

    children: dict[int | None, list[LibraryPlaylist]] = {}
    for item in library.playlists:
        children.setdefault(item.parent_id, []).append(item)
    top_level = children.get(None, [])
    if not preserve_playlist_order:
        top_level = sorted(top_level, key=lambda value: (value.sequence, value.playlist_id))
    bridge_subnodes = ET.SubElement(bridge_node, "SUBNODES", {"COUNT": str(len(top_level))})
    for item in top_level:
        _append_playlist_node(
            bridge_subnodes,
            item,
            children,
            primary_keys,
            library.session_id,
            preserve_playlist_order,
        )

    ET.SubElement(root, "INDEXING")
    _write_nml(root, output_path, Path(library.usb_root))
    return NmlExportResult(
        output_path=str(output_path),
        tracks=len(library.tracks),
        playlists=sum(not item.is_folder for item in library.playlists),
        folders=sum(item.is_folder for item in library.playlists),
        grid_markers=grid_markers,
        hot_cues=hot_cues,
        memory_cues=memory_cues,
        saved_loops=saved_loops,
        key_policy=normalized_policy,
        preview_keys=preview_keys,
        musical_keys=musical_keys,
        warnings=warnings,
    )
