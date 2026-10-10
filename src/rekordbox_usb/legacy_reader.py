"""Bounded, read-only interpretation of the specified Legacy V1.1 subset."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from .metadata_normalization import normalize_key


PAGE_SIZE = 4096


class InvalidLibrary(Exception):
    pass


class Unsupported(Exception):
    pass


class ReadFailure(Exception):
    pass


def _require(condition):
    if not condition:
        raise InvalidLibrary


def _u(data, offset, width, limit=None):
    boundary = len(data) if limit is None else limit
    _require(0 <= offset <= boundary and width <= boundary - offset)
    return int.from_bytes(data[offset:offset + width], "little")


def _string(page, start, end):
    marker = _u(page, start, 1, end)
    if marker in (0x40, 0x90):
        length = _u(page, start + 1, 2, end)
        _require(length >= 4 and length <= end - start)
        payload = page[start + 4:start + length]
        if marker == 0x90:
            _require(len(payload) % 2 == 0)
            encoding = "utf-16le"
        else:
            encoding = "ascii"
    else:
        _require(marker & 1)
        length = marker >> 1
        _require(length >= 1 and length <= end - start)
        payload = page[start + 1:start + length]
        encoding = "ascii"
    try:
        value = payload.decode(encoding, errors="strict").rstrip("\x00")
    except UnicodeError as exc:
        raise InvalidLibrary from exc
    _require("\x00" not in value)
    return value


def _audio_path(value):
    _require(value and not any(unicodedata.category(ch) == "Cc" for ch in value))
    _require("://" not in value)
    path = value.replace("\\", "/")
    _require(not path.startswith("//") and not (len(path) >= 2 and path[1] == ":"))
    if path.startswith("/"):
        path = path[1:]
    _require(path and not path.startswith("/"))
    _require(all(part and part not in (".", "..") and ":" not in part for part in path.split("/")))
    return path


class _Pages:
    def __init__(self, stream):
        self.stream = stream
        try:
            first = os.fstat(stream.fileno())
        except (OSError, ValueError) as exc:
            raise ReadFailure from exc
        self.identity = (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns, first.st_ctime_ns)
        self.size = first.st_size
        if self.size < PAGE_SIZE or self.size % PAGE_SIZE:
            raise Unsupported
        self.count = self.size // PAGE_SIZE

    def get(self, index):
        _require(0 <= index < self.count)
        try:
            self.stream.seek(index * PAGE_SIZE)
            page = self.stream.read(PAGE_SIZE)
            current = os.fstat(self.stream.fileno())
        except (OSError, ValueError) as exc:
            raise ReadFailure from exc
        if len(page) != PAGE_SIZE or self._identity(current) != self.identity:
            raise ReadFailure
        return page

    @staticmethod
    def _identity(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def unchanged(self):
        try:
            current = os.fstat(self.stream.fileno())
        except (OSError, ValueError) as exc:
            raise ReadFailure from exc
        if self._identity(current) != self.identity:
            raise ReadFailure


def _directory(header, count):
    if _u(header, 0, 4) != 0 or _u(header, 24, 4) != 0:
        raise Unsupported
    if _u(header, 4, 4) != PAGE_SIZE or _u(header, 8, 4) != 20:
        raise Unsupported
    families = {}
    for slot in range(20):
        at = 28 + 16 * slot
        family = _u(header, at, 4)
        if family in (0, 2, 5, 7, 8):
            _require(family not in families)
            first, last = _u(header, at + 8, 4), _u(header, at + 12, 4)
            if family in (2, 5) and first == 0 and last == 0:
                continue
            _require(first < count and last < count)
            families[family] = (first, last)
    _require({0, 7, 8}.issubset(families))
    return families


def _rows(pages, family, first, last):
    seen = set()
    index = first
    while True:
        _require(index not in seen and len(seen) < pages.count)
        seen.add(index)
        page = pages.get(index)
        data_page = (page[27] & 0x40) == 0
        if data_page:
            _require(_u(page, 4, 4) == index and _u(page, 8, 4) == family)
            packed = _u(page, 24, 3)
            locators, active = packed & 0x1fff, (packed >> 13) & 0x7ff
            groups = (locators + 15) // 16
            end = PAGE_SIZE - 36 * groups
            _require(end >= 40 and locators <= (PAGE_SIZE - 40) // 2)
            processed = 0
            for group in range(groups):
                base = PAGE_SIZE - 36 * group
                _require(base - 36 >= end)
                mask = _u(page, base - 4, 2)
                for position in range(16):
                    logical = group * 16 + position
                    if logical >= locators or not (mask & (1 << position)):
                        continue
                    locator = _u(page, base - (6 + 2 * position), 2)
                    start = 40 + locator
                    _require(40 <= start < end)
                    processed += 1
                    yield page, start, end
            _require(processed == active)
        if index == last:
            break
        index = _u(page, 12, 4)
        _require(index < pages.count)


def _decode_track(page, start, end, keys=None, artists=None):
    _require(136 <= end - start)
    ident = _u(page, start + 72, 4, end)
    _require(ident != 0)
    offsets = []
    for slot in range(21):
        relative = _u(page, start + 94 + 2 * slot, 2, end)
        _require(136 <= relative < end - start)
        offsets.append(relative)
    strings = []
    for slot in (17, 19, 20):
        strings.append(_string(page, start + offsets[slot], end))
    title, filename, path = strings
    title = title.strip() or filename.strip()
    _require(bool(title))
    track = {"id": str(ident), "title": title, "relative_audio_path": _audio_path(path)}
    artist_id = _u(page, start + 68, 4, end)
    if artist_id:
        _require(artists is not None and artist_id in artists)
        track["artist"] = artists[artist_id]
    # Local experimental metadata profile, measured independently on the source.
    bpmx100 = _u(page, start + 56, 4, end)
    if bpmx100:
        track["bpm"] = bpmx100 / 100
    key_id = _u(page, start + 32, 4, end)
    if key_id:
        _require(keys is not None and key_id in keys)
        track["rekordbox_key"] = keys[key_id]
    return ident, track


def _decode_artist(page, start, end):
    # Authorial local profile measured on the maintainer's read-only source.
    _require(10 <= end - start)
    ident = _u(page, start + 4, 4, end)
    relative = _u(page, start + 9, 1, end)
    _require(ident != 0 and 10 <= relative < end - start)
    value = _string(page, start + relative, end).strip()
    _require(bool(value))
    return ident, value


def _decode_key(page, start, end):
    _require(9 <= end - start)
    ident = _u(page, start, 4, end)
    value = _string(page, start + 8, end).strip()
    _require(ident != 0 and bool(value))
    _require(normalize_key(value).is_valid)
    return ident, value


def _decode_node(page, start, end):
    _require(20 <= end - start)
    parent = _u(page, start, 4, end)
    sequence = _u(page, start + 8, 4, end)
    ident = _u(page, start + 12, 4, end)
    folder = _u(page, start + 16, 4, end) != 0
    name = _string(page, start + 20, end).strip()
    _require(ident != 0 and bool(name))
    return ident, {"id": str(ident), "name": name, "parent_id": str(parent) if parent else None,
                   "sequence": sequence, "kind": "folder" if folder else "playlist"}


def _decode_edge(page, start, end):
    _require(12 <= end - start)
    return (_u(page, start, 4, end), _u(page, start + 4, 4, end),
            _u(page, start + 8, 4, end))


def _assemble(tracks, nodes, edges):
    children = {}
    for ident, node in nodes.items():
        parent = int(node["parent_id"]) if node["parent_id"] else 0
        _require(parent == 0 or (parent in nodes and nodes[parent]["kind"] == "folder"))
        children.setdefault(parent, []).append(ident)
    for siblings in children.values():
        positions = [nodes[ident]["sequence"] for ident in siblings]
        _require(sorted(positions) == list(range(len(siblings))))
        siblings.sort(key=lambda ident: nodes[ident]["sequence"])
    ordered = []
    visited = set()

    pending = list(reversed(children.get(0, ())))
    while pending:
        ident = pending.pop()
        _require(ident not in visited)
        visited.add(ident)
        ordered.append(nodes[ident])
        pending.extend(reversed(children.get(ident, ())))
    _require(len(visited) == len(nodes))
    associations = {}
    for position, track, playlist in edges:
        _require(track in tracks and playlist in nodes and nodes[playlist]["kind"] == "playlist")
        associations.setdefault(playlist, []).append((position, track))
    playlists = []
    for node in ordered:
        if node["kind"] != "playlist":
            continue
        items = sorted(associations.get(int(node["id"]), ()))
        positions = [position for position, _ in items]
        _require(positions == list(range(len(items))) or positions == list(range(1, len(items) + 1)))
        playlists.append({"node_id": node["id"], "track_ids": [str(track) for _, track in items]})
    return {"tracks": [tracks[key] for key in sorted(tracks)], "nodes": ordered, "playlists": playlists}


def _parse(stream):
    pages = _Pages(stream)
    families = _directory(pages.get(0), pages.count)
    tracks, nodes, edges = {}, {}, []
    artists = {}
    if 2 in families:
        for page, start, end in _rows(pages, 2, *families[2]):
            ident, value = _decode_artist(page, start, end)
            _require(ident not in artists)
            artists[ident] = value
    keys = {}
    if 5 in families:
        for page, start, end in _rows(pages, 5, *families[5]):
            ident, value = _decode_key(page, start, end)
            _require(ident not in keys)
            keys[ident] = value
    for family, destination, decoder in ((0, tracks, _decode_track), (7, nodes, _decode_node)):
        for page, start, end in _rows(pages, family, *families[family]):
            ident, value = decoder(page, start, end, keys, artists) if family == 0 else decoder(page, start, end)
            _require(ident not in destination)
            destination[ident] = value
    for page, start, end in _rows(pages, 8, *families[8]):
        edges.append(_decode_edge(page, start, end))
    library = _assemble(tracks, nodes, edges)
    pages.unchanged()
    return library


def read_legacy(root, opener=open):
    """Return the neutral result; opener is an injectable binary I/O boundary."""
    candidate = Path(root) / "PIONEER" / "rekordbox" / "export.pdb"
    try:
        with opener(candidate, "rb") as stream:
            library = _parse(stream)
    except FileNotFoundError:
        status = "UNSUPPORTED"
    except Unsupported:
        status = "UNSUPPORTED"
    except InvalidLibrary:
        status = "INVALID_LIBRARY"
    except (ReadFailure, OSError):
        status = "READ_ERROR"
    else:
        return {"status": "SUCCESS", "error": None, "library": library}
    return {"status": status, "error": status, "library": None}
