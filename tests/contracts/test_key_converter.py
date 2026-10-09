import pytest

from rekordbox_usb.key_converter import KeyConverter, convert_rekordbox_key
from rekordbox_usb.library_models import ActiveKeySource, LibraryTrack


@pytest.mark.parametrize(
    ("rekordbox_key", "traktor_key"),
    [
        ("1A", "6m"),
        ("2A", "7m"),
        ("3A", "8m"),
        ("4A", "9m"),
        ("5A", "10m"),
        ("6A", "11m"),
        ("7A", "12m"),
        ("8A", "1m"),
        ("9A", "2m"),
        ("10A", "3m"),
        ("11A", "4m"),
        ("12A", "5m"),
        ("1B", "6d"),
        ("2B", "7d"),
        ("3B", "8d"),
        ("4B", "9d"),
        ("5B", "10d"),
        ("6B", "11d"),
        ("7B", "12d"),
        ("8B", "1d"),
        ("9B", "2d"),
        ("10B", "3d"),
        ("11B", "4d"),
        ("12B", "5d"),
    ],
)
def test_converts_all_camelot_keys_to_traktor_open_key(rekordbox_key: str, traktor_key: str):
    assert convert_rekordbox_key(rekordbox_key) == traktor_key
    assert KeyConverter.convert_rekordbox_key(rekordbox_key) == traktor_key


@pytest.mark.parametrize("value", [None, "", "None", "13A", "0B", "C#m", "random key"])
def test_returns_none_for_empty_or_invalid_rekordbox_keys(value: str | None):
    assert convert_rekordbox_key(value) is None


def _track(rekordbox_key: str) -> LibraryTrack:
    return LibraryTrack(
        track_id=1,
        title="Key Test",
        artist=None,
        album=None,
        genre=None,
        label=None,
        bpm=None,
        rekordbox_key=rekordbox_key,
        duration_seconds=None,
        track_number=None,
        rating=None,
        comment=None,
        rekordbox_path="/Contents/test.aiff",
        source_path=r"D:\Contents\test.aiff",
        file_name="test.aiff",
        file_size=None,
    )


def test_traktor_analysis_key_has_priority_over_rekordbox_preview():
    track = _track("4A")

    assert track.traktor_preview_key == "9m"
    assert track.active_key == "9m"
    assert track.active_key_source is ActiveKeySource.REKORDBOX_IMPORTED

    track.set_traktor_analysis_key("11d")

    assert track.active_key == "11d"
    assert track.active_key_source is ActiveKeySource.TRAKTOR_ANALYZED


def test_conversion_preserves_the_original_rekordbox_key():
    track = _track("8B")

    assert track.rekordbox_key == "8B"
    assert track.rekordbox_key_original == "8B"
    assert track.traktor_preview_key == "1d"
