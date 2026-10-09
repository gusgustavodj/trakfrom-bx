"""Authorial byte builder derived only from the neutral Legacy V1 spec."""

from __future__ import annotations

from dataclasses import dataclass
import struct


PAGE_SIZE = 4096
DIRECTORY_COUNT = 20
DIRECTORY_OFFSET = 28
TRACK_FAMILY = 0
NODE_FAMILY = 7
ENTRY_FAMILY = 8
KEY_FAMILY = 5


@dataclass(frozen=True, slots=True)
class SyntheticLegacyImage:
    data: bytes
    family_pages: dict[int, tuple[int, ...]]
    row_addresses: dict[int, tuple[int, ...]]


def encode_short_ascii(value: str) -> bytes:
    payload = value.encode("ascii")
    total = len(payload) + 1
    if total > 127:
        raise ValueError("short ASCII value is too long")
    return bytes(((total << 1) | 1,)) + payload


def encode_long_ascii(value: str) -> bytes:
    payload = value.encode("ascii")
    total = len(payload) + 4
    return b"\x40" + struct.pack("<H", total) + b"\x00" + payload


def encode_long_utf16(value: str) -> bytes:
    payload = value.encode("utf-16-le")
    total = len(payload) + 4
    return b"\x90" + struct.pack("<H", total) + b"\x00" + payload


def _encode(value: str, mode: str) -> bytes:
    if mode == "short_ascii":
        return encode_short_ascii(value)
    if mode == "long_ascii":
        return encode_long_ascii(value)
    if mode == "utf16":
        return encode_long_utf16(value)
    if mode != "auto":
        raise ValueError(f"unknown synthetic string mode: {mode}")
    try:
        return encode_short_ascii(value)
    except (UnicodeEncodeError, ValueError):
        try:
            return encode_long_ascii(value)
        except UnicodeEncodeError:
            return encode_long_utf16(value)


class SyntheticLegacyPdbBuilder:
    """Build minimal valid or deliberately incoherent V1 images in memory."""

    def __init__(self) -> None:
        self._tracks: list[bytes] = []
        self._nodes: list[bytes] = []
        self._entries: list[bytes] = []
        self._keys: list[bytes] = []

    def add_track(
        self,
        track_id: int,
        title: str,
        path: str,
        *,
        file_name: str | None = None,
        title_mode: str = "auto",
        file_name_mode: str = "auto",
        path_mode: str = "auto",
        offset_overrides: dict[int, int] | None = None,
        bpmx100: int = 0,
        key_id: int = 0,
    ) -> SyntheticLegacyPdbBuilder:
        file_name = file_name if file_name is not None else path.replace("\\", "/").split("/")[-1]
        fixed = bytearray(136)
        struct.pack_into("<I", fixed, 72, track_id)
        struct.pack_into("<I", fixed, 56, bpmx100)
        struct.pack_into("<I", fixed, 32, key_id)
        encoded_values = (
            (17, _encode(title, title_mode)),
            (19, _encode(file_name, file_name_mode)),
            (20, _encode(path, path_mode)),
        )
        cursor = len(fixed)
        offsets = [len(fixed)] * 21
        payload = bytearray()
        for index, encoded in encoded_values:
            offsets[index] = cursor + len(payload)
            payload.extend(encoded)
        for index, offset in (offset_overrides or {}).items():
            if not 0 <= index < len(offsets):
                raise ValueError("synthetic track offset index is outside 0..20")
            offsets[index] = offset
        for index, offset in enumerate(offsets):
            struct.pack_into("<H", fixed, 94 + index * 2, offset)
        self._tracks.append(bytes(fixed + payload))
        return self

    def add_key(self, key_id: int, value: str) -> SyntheticLegacyPdbBuilder:
        self._keys.append(struct.pack("<II", key_id, 0) + _encode(value, "auto"))
        return self

    def add_node(
        self,
        node_id: int,
        name: str,
        *,
        parent_id: int = 0,
        sibling_position: int = 0,
        is_folder: bool = False,
        name_mode: str = "auto",
    ) -> SyntheticLegacyPdbBuilder:
        row = bytearray(20)
        struct.pack_into("<I", row, 0, parent_id)
        struct.pack_into("<I", row, 8, sibling_position)
        struct.pack_into("<I", row, 12, node_id)
        struct.pack_into("<I", row, 16, int(is_folder))
        row.extend(_encode(name, name_mode))
        self._nodes.append(bytes(row))
        return self

    def add_entry(
        self,
        playlist_id: int,
        track_id: int,
        entry_position: int,
    ) -> SyntheticLegacyPdbBuilder:
        self._entries.append(struct.pack("<III", entry_position, track_id, playlist_id))
        return self

    def add_playlist_entries(
        self,
        playlist_id: int,
        track_ids: tuple[int, ...] | list[int],
        *,
        position_base: int = 0,
    ) -> SyntheticLegacyPdbBuilder:
        """Add ordered entries with a caller-selected local position base."""

        for offset, track_id in enumerate(track_ids):
            self.add_entry(playlist_id, track_id, position_base + offset)
        return self

    def build(self) -> bytes:
        return self.build_image().data

    def build_image(self) -> SyntheticLegacyImage:
        rows_by_family = {
            TRACK_FAMILY: tuple(self._tracks),
            NODE_FAMILY: tuple(self._nodes),
            ENTRY_FAMILY: tuple(self._entries),
        }
        if self._keys:
            rows_by_family[KEY_FAMILY] = tuple(self._keys)
        chunks_by_family = {
            family: self._split_rows(rows) for family, rows in rows_by_family.items()
        }
        family_pages: dict[int, tuple[int, ...]] = {}
        next_page_index = 1
        for family in rows_by_family:
            count = len(chunks_by_family[family])
            family_pages[family] = tuple(range(next_page_index, next_page_index + count))
            next_page_index += count

        pages = [bytearray(PAGE_SIZE) for _ in range(next_page_index)]
        header = pages[0]
        struct.pack_into("<I", header, 4, PAGE_SIZE)
        struct.pack_into("<I", header, 8, DIRECTORY_COUNT)
        struct.pack_into("<I", header, 12, next_page_index)
        for family in range(DIRECTORY_COUNT):
            offset = DIRECTORY_OFFSET + family * 16
            struct.pack_into("<I", header, offset, family)
            if family in family_pages:
                indices = family_pages[family]
                struct.pack_into("<I", header, offset + 8, indices[0])
                struct.pack_into("<I", header, offset + 12, indices[-1])

        row_addresses: dict[int, list[int]] = {family: [] for family in rows_by_family}
        for family, chunks in chunks_by_family.items():
            indices = family_pages[family]
            for position, (page_index, rows) in enumerate(zip(indices, chunks, strict=True)):
                next_index = indices[position + 1] if position + 1 < len(indices) else next_page_index
                page, starts = self._build_page(page_index, family, next_index, rows)
                pages[page_index] = page
                row_addresses[family].extend(page_index * PAGE_SIZE + start for start in starts)

        return SyntheticLegacyImage(
            data=b"".join(pages),
            family_pages=family_pages,
            row_addresses={family: tuple(items) for family, items in row_addresses.items()},
        )

    @staticmethod
    def _split_rows(rows: tuple[bytes, ...]) -> tuple[tuple[bytes, ...], ...]:
        if not rows:
            return ((),)
        chunks: list[tuple[bytes, ...]] = []
        current: list[bytes] = []
        current_size = 0
        for row in rows:
            candidate_count = len(current) + 1
            groups = (candidate_count + 15) // 16
            required = 40 + current_size + len(row) + 36 * groups
            if required > PAGE_SIZE and current:
                chunks.append(tuple(current))
                current = []
                current_size = 0
                candidate_count = 1
                groups = 1
                required = 40 + len(row) + 36 * groups
            if required > PAGE_SIZE:
                raise ValueError("synthetic row does not fit in one page")
            current.append(row)
            current_size += len(row)
        chunks.append(tuple(current))
        return tuple(chunks)

    @staticmethod
    def _build_page(
        page_index: int,
        family: int,
        next_page: int,
        rows: tuple[bytes, ...],
    ) -> tuple[bytearray, tuple[int, ...]]:
        page = bytearray(PAGE_SIZE)
        struct.pack_into("<I", page, 4, page_index)
        struct.pack_into("<I", page, 8, family)
        struct.pack_into("<I", page, 12, next_page)
        locator_count = len(rows)
        packed = locator_count | (locator_count << 13)
        page[24:27] = packed.to_bytes(3, "little")
        group_count = (locator_count + 15) // 16
        index_start = PAGE_SIZE - 36 * group_count
        cursor = 40
        starts: list[int] = []
        masks = [0] * group_count
        for logical_index, row in enumerate(rows):
            if cursor + len(row) > index_start:
                raise ValueError("synthetic page overflow")
            page[cursor : cursor + len(row)] = row
            starts.append(cursor)
            group = logical_index // 16
            item = logical_index % 16
            base = PAGE_SIZE - group * 36
            struct.pack_into("<H", page, base - (6 + 2 * item), cursor - 40)
            masks[group] |= 1 << item
            cursor += len(row)
        for group, mask in enumerate(masks):
            base = PAGE_SIZE - group * 36
            struct.pack_into("<H", page, base - 4, mask)
        struct.pack_into("<H", page, 28, index_start - cursor)
        struct.pack_into("<H", page, 30, cursor - 40)
        return page, tuple(starts)
