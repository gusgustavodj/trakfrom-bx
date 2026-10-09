from rekordbox_usb.library_models import LibraryTrack
from rekordbox_usb.metadata_normalization import normalize_bpm, normalize_key


def test_key_normalization_accepts_camelot_open_key_and_musical_key():
    for value in ("8A", "1m", "Am"):
        model = normalize_key(value)
        assert model.is_valid is True
        assert model.camelot == "8A"
        assert model.open_key == "1m"
        assert model.mode == "minor"


def test_enharmonic_inputs_share_harmonic_identity():
    f_minor = normalize_key("4A")
    assert f_minor.open_key == "9m"
    assert f_minor.canonical_flat_name == "F minor"
    assert f_minor.harmonic_id == "9m"

    ab_major = normalize_key("4B")
    g_sharp_major = normalize_key("G# major")
    assert ab_major.open_key == "9d"
    assert g_sharp_major.open_key == "9d"
    assert ab_major.harmonic_id == g_sharp_major.harmonic_id
    assert ab_major.pitch_class == g_sharp_major.pitch_class


def test_key_model_exposes_flat_and_sharp_spellings():
    model = normalize_key("1A")
    assert model.open_key == "6m"
    assert model.canonical_flat_name == "A-flat minor"
    assert model.canonical_sharp_name == "G-sharp minor"
    assert model.short_flat == "Abm"
    assert model.short_sharp == "G#m"


def test_traktor_musical_key_value_confirms_textual_key():
    assert normalize_key("8A", 21).key_conflict is False
    assert normalize_key("8A", 21).key_source_confidence == "high"
    assert normalize_key("12m", 14).open_key == "12m"
    assert normalize_key("12m", 14).key_conflict is False
    assert normalize_key("9d", 8).key_conflict is False


def test_traktor_musical_key_value_marks_conflict():
    model = normalize_key("9d", 5)
    assert model.open_key == "9d"
    assert model.traktor_musical_key_value == 5
    assert model.key_conflict is True
    assert model.key_source_confidence == "low"


def test_key_normalization_rejects_unknown_key():
    assert normalize_key("not-a-key").is_valid is False


def test_bpm_range_adjusts_octaves_and_preserves_raw_value():
    assert normalize_bpm(64) == (128.0, True)
    assert normalize_bpm(160) == (80.0, True)
    assert normalize_bpm(120) == (120.0, False)


def test_track_metadata_prefers_traktor_after_analysis():
    track = LibraryTrack(
        1, "Track", None, None, None, None, 64.0, "8A", None, None, None, None,
        "/track", r"D:\\track", "track", None,
    )
    assert track.raw_bpm == 64.0
    assert track.normalized_bpm == 128.0
    assert track.bpm_source == "rekordbox"
    assert track.preferred_display_key == "1m"
    track.set_traktor_analysis_bpm(130.0)
    track.set_traktor_analysis_key("2d")
    assert track.normalized_bpm == 130.0
    assert track.bpm_source == "traktor"
    assert track.preferred_display_key == "2d"
