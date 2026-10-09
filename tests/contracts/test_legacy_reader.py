"""Fresh contract tests for the authorial Legacy V1.1 read boundary.

Expectations: CONTRACTS/LEGACY_READER_V1_1.md sections 3, 4, 9, 11-13.
All bytes come from the attested synthetic fixture builder, never from a USB.
"""

from __future__ import annotations

from pathlib import Path
from struct import pack_into

import pytest

from rekordbox_usb.legacy_reader import read_legacy
from tests.fixtures.synthetic_legacy_pdb import SyntheticLegacyPdbBuilder


def _write_image(root: Path, image: bytes) -> Path:
    candidate = root / "PIONEER" / "rekordbox" / "export.pdb"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(image)
    return candidate


def _single_track_image(path: str = "Contents/synthetic.wav") -> bytes:
    return (
        SyntheticLegacyPdbBuilder()
        .add_track(11, "Synthetic Track", path)
        .add_node(7, "Synthetic List")
        .add_entry(7, 11, 0)
        .build()
    )


@pytest.mark.parametrize("position_base", [0, 1])
def test_valid_playlist_position_bases_preserve_logical_order(
    tmp_path: Path, position_base: int
) -> None:
    # Section 11 allows either complete base, independently per playlist.
    image = (
        SyntheticLegacyPdbBuilder()
        .add_track(11, "First", "Contents/first.wav")
        .add_track(12, "Second", "Contents/second.wav")
        .add_node(7, "Synthetic List")
        .add_playlist_entries(7, [12, 11], position_base=position_base)
        .build()
    )
    candidate = _write_image(tmp_path, image)

    result = read_legacy(tmp_path)

    assert result["status"] == "SUCCESS"
    assert result["error"] is None
    assert result["library"]["playlists"] == [
        {"node_id": "7", "track_ids": ["12", "11"]}
    ]
    assert {track["id"]: track["relative_audio_path"] for track in result["library"]["tracks"]} == {
        "11": "Contents/first.wav",
        "12": "Contents/second.wav",
    }
    assert candidate.read_bytes() == image


@pytest.mark.parametrize("unsafe_path", ["../outside.wav", "C:/outside.wav", "//server/song.wav"])
def test_unsafe_source_path_invalidates_entire_library(tmp_path: Path, unsafe_path: str) -> None:
    # Section 9 rejects traversal, drive prefixes and UNC paths.
    _write_image(tmp_path, _single_track_image(unsafe_path))

    result = read_legacy(tmp_path)

    assert result["status"] == "INVALID_LIBRARY"
    assert result["library"] is None


def test_playlist_positions_outside_both_allowed_bases_are_invalid(tmp_path: Path) -> None:
    # Section 11 accepts exactly 0..N-1 or 1..N, not a shifted sequence.
    image = (
        SyntheticLegacyPdbBuilder()
        .add_track(11, "Synthetic Track", "Contents/synthetic.wav")
        .add_node(7, "Synthetic List")
        .add_playlist_entries(7, [11], position_base=2)
        .build()
    )
    _write_image(tmp_path, image)

    result = read_legacy(tmp_path)

    assert result["status"] == "INVALID_LIBRARY"
    assert result["library"] is None


def test_unsupported_page_size_returns_no_partial_library(tmp_path: Path) -> None:
    # Section 4.1 defines 4096 as the sole supported page size in V1.1.
    image = bytearray(_single_track_image())
    pack_into("<I", image, 4, 2048)
    _write_image(tmp_path, bytes(image))

    result = read_legacy(tmp_path)

    assert result["status"] == "UNSUPPORTED"
    assert result["library"] is None


def test_reader_opens_only_the_canonical_database_in_binary_read_mode(tmp_path: Path) -> None:
    # Section 3 fixes the sole candidate and forbids writable opens.
    candidate = _write_image(tmp_path, _single_track_image())
    observed: list[tuple[Path, str]] = []

    def recorded_open(path: Path, mode: str):
        observed.append((path, mode))
        return open(path, mode)

    result = read_legacy(tmp_path, opener=recorded_open)

    assert result["status"] == "SUCCESS"
    assert observed == [(candidate, "rb")]


def test_io_failure_returns_read_error_without_a_library(tmp_path: Path) -> None:
    # Section 13 distinguishes I/O failure from an invalid source library.
    def failing_open(path: Path, mode: str):
        raise OSError("synthetic I/O failure")

    result = read_legacy(tmp_path, opener=failing_open)

    assert result["status"] == "READ_ERROR"
    assert result["library"] is None
